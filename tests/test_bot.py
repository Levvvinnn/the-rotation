"""Exercise Discord callbacks locally, without a token or network connection."""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

with patch.dict(os.environ, {"ROTATION_DB": ":memory:", "ANTHROPIC_API_KEY": ""}):
    import bot

from rotation import money
from rotation.store import Store


def person(uid, name):
    return NS(id=uid, display_name=name, bot=False)


class BotTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_store = bot.store
        bot.store = Store(Path(self.tmp.name) / 'test.db')
        self.env = patch.dict(os.environ, {'ANTHROPIC_API_KEY': '', 'ROTATION_TIMEZONE': 'America/Phoenix'})
        self.env.start()
        self.ana, self.ben, self.cal = person(1, 'Ana'), person(2, 'Ben'), person(3, 'Cal')
        self.channel = NS(id=100, send=AsyncMock(), fetch_message=AsyncMock())

    async def asyncTearDown(self):
        bot.store.db.close()
        bot.store = self.original_store
        self.env.stop()
        self.tmp.cleanup()

    def message(self, uid, text, mentions=(), mid=500):
        return NS(id=mid, author=uid, content=text, mentions=list(mentions),
                  guild=NS(id=10), channel=self.channel, reference=None,
                  created_at=datetime.now(timezone.utc), reply=AsyncMock(), add_reaction=AsyncMock())

    def interaction(self, uid):
        return NS(id=900, user=uid, guild_id=10, channel_id=100, channel=self.channel,
                  permissions=NS(manage_guild=False), message=NS(embeds=[]),
                  response=NS(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock()),
                  followup=NS(send=AsyncMock()))

    def seed_money(self):
        bot.store.add_expense(10, 1, 'Ana', 6000, 'dinner', money.split(6000, [(1,'Ana'),(2,'Ben'),(3,'Cal')]))

    async def test_required_commands_are_registered_and_server_only(self):
        self.assertEqual({c.name for c in bot.tree.get_commands()},
                         {'spin','tally','log','expense','owe','settle','undo'})
        self.assertTrue(all(c.guild_only for c in bot.tree.get_commands()))
        self.assertIsNone(bot.tree.get_command("demo-seed"))

    async def test_chat_expense_and_author_reaction_undo(self):
        msg = self.message(self.ana, 'I paid $48 for dinner <@2> <@3>', [self.ben, self.cal])
        await bot.on_message(msg)
        self.assertIn('split 3 ways: Ben $16.00 · Cal $16.00', msg.reply.call_args.args[0])
        self.assertEqual(len(bot.store.events(10, 'pay')), 1)
        with patch.object(bot.client, 'get_channel', return_value=None):
            await bot.on_raw_reaction_add(NS(emoji='❌', message_id=500, user_id=2, channel_id=100))
            self.assertEqual(len(bot.store.expenses(10)), 1)
            await bot.on_raw_reaction_add(NS(emoji='❌', message_id=500, user_id=1, channel_id=100))
        self.assertEqual(bot.store.expenses(10), [])
        self.assertEqual(bot.store.events(10, 'pay'), [])

    async def test_chat_redelivery_cannot_double_log(self):
        msg = self.message(self.ana, 'I paid $48 for dinner', [self.ben, self.cal])
        await bot.on_message(msg)
        await bot.on_message(msg)
        self.assertEqual(len(bot.store.expenses(10)), 1)
        self.assertEqual(len(bot.store.events(10, 'pay')), 1)

    async def test_no_mentions_splits_with_recent_channel_speakers(self):
        await bot.on_message(self.message(self.ben, 'hi', mid=501))
        await bot.on_message(self.message(self.cal, 'hello', mid=502))
        await bot.on_message(self.message(self.ana, 'I paid $48 for dinner'))
        self.assertEqual(len(bot.store.expenses(10)[0].shares), 3)

    async def test_explicit_mentions_override_recent_speakers(self):
        await bot.on_message(self.message(self.cal, 'hello', mid=502))
        await bot.on_message(self.message(self.ana, 'I paid $48 for dinner <@2>', [self.ben]))
        self.assertEqual([uid for uid, _, _ in bot.store.expenses(10)[0].shares], [1, 2])

    async def test_done_cannot_be_clicked_twice(self):
        view = bot.TurnView(10, 'drive', {1:'Ana',2:'Ben'}, 2, 'Ben', clear=True)
        await view.done.callback(self.interaction(self.ben))
        await view.done.callback(self.interaction(self.ben))
        self.assertEqual(len(bot.store.events(10, 'drive')), 1)

    async def test_only_picked_person_can_pass(self):
        view = bot.TurnView(10, 'drive', {1:'Ana',2:'Ben',3:'Cal'}, 2, 'Ben', clear=False)
        with patch.object(bot, 'run_turn', new_callable=AsyncMock) as run:
            await view.cant.callback(self.interaction(self.ana))
            run.assert_not_awaited()
            await view.cant.callback(self.interaction(self.ben))
            self.assertEqual(run.call_args.args[3], {1:'Ana',3:'Cal'})
            self.assertTrue(run.call_args.kwargs['force_spin'])
        self.assertEqual(bot.store.events(10, 'drive'), [])

    async def test_single_remaining_person_has_done_button(self):
        await bot.run_turn(self.channel, 10, 'drive', {1:'Ana'}, force_spin=True)
        self.assertIsInstance(self.channel.send.call_args.kwargs['view'], bot.TurnView)

    async def test_default_group_uses_history(self):
        bot.store.add(10, 'drive', 2, 'Ben')
        self.assertEqual(bot.default_people(100,10,'drive',{1:'Ana'}), {2:'Ben'})

    async def test_settlement_card_rejects_stale_and_duplicate_clicks(self):
        self.seed_money()
        pay = money.payments(money.balances(bot.store.expenses(10)))[0]
        old, current = bot.SettleButton(10,pay), bot.SettleButton(10,pay)
        await current.callback(self.interaction(self.ben))
        await current.callback(self.interaction(self.ben))
        await old.callback(self.interaction(self.ben))
        self.assertEqual(sum(e.kind == 'settle' for e in bot.store.expenses(10)), 1)

    async def test_unrelated_person_cannot_confirm_repayment(self):
        self.seed_money()
        button = bot.SettleButton(10, (2,'Ben',1,'Ana',2000))
        await button.callback(self.interaction(self.cal))
        self.assertEqual(len(bot.store.expenses(10)), 1)

    async def test_chat_repayment_infers_only_creditor(self):
        self.seed_money()
        msg = self.message(self.ben, "venmo'd you $20")
        await bot.on_message(msg)
        msg.add_reaction.assert_awaited_once_with('🤝')
        self.assertEqual(money.balances(bot.store.expenses(10))[2][1], 0)

    async def test_reply_repayment_fetches_uncached_message(self):
        self.seed_money()
        msg = self.message(self.ben, 'paid you back')
        msg.reference = NS(resolved=None, message_id=499)
        reference = Mock(spec=bot.discord.Message)
        reference.author = self.ana
        self.channel.fetch_message.return_value = reference
        await bot.on_message(msg)
        self.channel.fetch_message.assert_awaited_once_with(499)
        self.assertEqual(money.balances(bot.store.expenses(10))[2][1], 0)

    async def test_undo_rejects_other_users(self):
        bot.store.add(10,'drive',1,'Ana')
        await bot.undo.callback(self.interaction(self.ben))
        self.assertEqual(len(bot.store.events(10,'drive')), 1)

    async def test_undo_expense_also_removes_linked_turn(self):
        await bot.on_message(self.message(self.ana, 'I paid $48 for dinner', [self.ben]))
        await bot.undo.callback(self.interaction(self.ana))
        self.assertEqual(bot.store.expenses(10), [])
        self.assertEqual(bot.store.events(10,'pay'), [])

    async def test_payment_plan_paginates_every_button(self):
        pays = [(i+10,f'Friend{i}',1,'Ana',100) for i in range(27)]
        first = bot.OweView(10,pays)
        second = bot.OweView(10,pays,1)
        self.assertEqual(sum(isinstance(b,bot.SettleButton) for b in first.children),20)
        self.assertEqual(sum(isinstance(b,bot.SettleButton) for b in second.children),7)
        self.assertTrue(any(isinstance(b,bot.OwePageButton) for b in first.children))

    async def test_ambiguous_recipient_asks_without_mutating_money(self):
        bot.store.add_expense(10,1,'Ana',3000,'food',[(2,'Ben',3000)])
        bot.store.add_expense(10,3,'Cal',2000,'uber',[(2,'Ben',2000)])
        msg = self.message(self.ben, "venmo'd you $20")
        await bot.on_message(msg)
        self.assertEqual(len(bot.store.expenses(10)),2)
        self.assertIn('Who did you pay back?',msg.reply.call_args.args[0])

    async def test_slash_settle_rejects_negative_amount(self):
        self.seed_money()
        with self.assertRaises(ValueError):
            await bot.settle.callback(self.interaction(self.ben), self.ana, -20)
        self.assertEqual(len(bot.store.expenses(10)), 1)

    async def test_slash_expense_allows_recent_friends(self):
        bot.store.remember(100,2,'Ben',bot.time.time())
        await bot.expense.callback(self.interaction(self.ana),48,'dinner')
        self.assertEqual(len(bot.store.expenses(10)[0].shares),2)
        self.assertEqual(len(bot.store.events(10,'pay')),1)
        await bot.undo.callback(self.interaction(self.ana))
        self.assertEqual(bot.store.events(10,'pay'),[])
        self.assertEqual(bot.store.expenses(10),[])

    async def test_plain_chat_logs_chores_and_replies_with_note(self):
        for i, (phrase, task) in enumerate([('I drove','drive'), ('I cooked dinner','cooking'),
                                           ('I did the dishes','dishes'), ('I did laundry','laundry')]):
            msg = self.message(self.ana,phrase,mid=600+i)
            await bot.on_message(msg)
            self.assertEqual(len(bot.store.events(10,task)),1)
            msg.add_reaction.assert_awaited_once_with('📝')
            self.assertIn('📝 Logged: Ana did **'+task+'**',msg.reply.call_args.args[0])

    async def test_first_chore_remembers_recent_group_including_never_done(self):
        await bot.on_message(self.message(self.ben,'hello',mid=601))
        await bot.on_message(self.message(self.ana,'I cooked dinner'))
        self.assertEqual(bot.store.task_people(10,'cooking'),{1:'Ana',2:'Ben'})
        ranked=bot.task_standings(10,'cooking')
        self.assertEqual(ranked[0].name,'Ben')
        self.assertIsNone(ranked[0].days_since)
        self.assertIn('Up next for **cooking**: **Ben**',bot.log_confirmation(10,'cooking','Ana'))

    async def test_tally_without_task_shows_independent_next_people(self):
        now=bot.time.time()
        for task, uid, name, days in [('cooking',1,'Ana',2),('cooking',2,'Ben',7),
                                      ('uber',1,'Ana',10),('uber',2,'Ben',1)]:
            bot.store.add(10,task,uid,name,ts=now-days*86400)
        interaction=self.interaction(self.ana)
        await bot.tally.callback(interaction)
        embed=interaction.response.send_message.call_args.kwargs['embed']
        fields={field.name:field.value for field in embed.fields}
        self.assertIn('Up next for **cooking**: **Ben**',fields['cooking'])
        self.assertIn('Up next for **uber**: **Ana**',fields['uber'])
        self.assertIn('Last done:',fields['cooking'])
        self.assertFalse(bot.tally.parameters[0].required)

    async def test_tally_one_task_shows_next_last_and_odds(self):
        now=bot.time.time()
        bot.store.add(10,'cooking',1,'Ana',ts=now-2*86400)
        bot.store.add(10,'cooking',2,'Ben',ts=now-7*86400)
        interaction=self.interaction(self.ana)
        await bot.tally.callback(interaction,'cooking')
        embed=interaction.response.send_message.call_args.kwargs['embed']
        self.assertIn('Up next for **cooking**: **Ben**',embed.description)
        self.assertIn('Last done: **Ana**',embed.description)
        self.assertIn('%',embed.description)

    async def test_close_tally_explains_that_next_requires_a_spin(self):
        now=bot.time.time()
        bot.store.add(10,'uber',1,'Ana',ts=now-5*86400)
        bot.store.add(10,'uber',2,'Ben',ts=now-5*86400)
        interaction=self.interaction(self.ana)
        await bot.tally.callback(interaction,'uber')
        text=interaction.response.send_message.call_args.kwargs['embed'].description
        self.assertIn('spin needed',text)
        self.assertIn('/spin task:uber',text)

    async def test_cook_alias_preserves_previously_logged_history(self):
        bot.store.add(10,'cook',1,'Ana',ts=bot.time.time()-86400)
        await bot.on_message(self.message(self.ben,'I cooked dinner'))
        self.assertEqual(len(bot.task_events(10,'cooking')),2)
        self.assertEqual(bot.tracked_tasks(10),['cooking'])
        self.assertEqual(bot.normalize_task('cook'),'cooking')

    async def test_chat_plan_is_not_logged(self):
        msg=self.message(self.ana,'I will cook dinner tomorrow')
        await bot.on_message(msg)
        self.assertEqual(bot.store.tasks(10),[])
        msg.reply.assert_not_awaited()

    async def test_empty_tally_explains_how_to_start(self):
        interaction=self.interaction(self.ana)
        await bot.tally.callback(interaction)
        text=interaction.response.send_message.call_args.kwargs['embed'].description
        self.assertIn('I cooked dinner',text)

    async def test_known_custom_chore_is_logged_without_ai(self):
        bot.store.add(10,'litter box',2,'Ben',ts=bot.time.time()-86400)
        msg=self.message(self.ana,'I did the litter box')
        await bot.on_message(msg)
        self.assertEqual(len(bot.store.events(10,'litter box')),2)
        self.assertIn('litter box',msg.reply.call_args.args[0])

    async def test_spin_uses_all_channel_humans_without_numbered_fields(self):
        self.channel.members=[self.ana,self.ben,self.cal,NS(id=99,display_name='Rotater',bot=True)]
        bot.store.add(10,'drive',1,'Old Ana')
        interaction=self.interaction(self.ana)
        with patch.object(bot,'run_turn',new_callable=AsyncMock) as run:
            await bot.spin.callback(interaction,'drive')
            self.assertEqual(run.call_args.args[3],{1:'Ana',2:'Ben',3:'Cal'})
        for command in bot.tree.get_commands():
            self.assertFalse(any(param.name.startswith('friend') for param in command.parameters))

    async def test_expense_uses_current_channel_names(self):
        self.channel.members=[self.ana,self.ben,self.cal,NS(id=99,display_name='Rotater',bot=True)]
        interaction=self.interaction(self.ana)
        await bot.expense.callback(interaction,48,'dinner')
        expense=bot.store.expenses(10)[0]
        self.assertEqual(expense.shares,[(1,'Ana',1600),(2,'Ben',1600),(3,'Cal',1600)])
        self.assertIn('Ben $16.00',interaction.followup.send.call_args.args[0])

    async def test_spin_picker_shows_names_and_uses_selected_people(self):
        self.channel.members=[self.ana,self.ben,self.cal,NS(id=99,display_name='Rotater',bot=True)]
        interaction=self.interaction(self.ana)
        with patch.object(bot,'run_turn',new_callable=AsyncMock) as run:
            await bot.spin.callback(interaction,'cooking',True)
            view=interaction.followup.send.call_args.kwargs['view']
            self.assertIsInstance(view,bot.ParticipantPicker)
            select=view.children[0]
            self.assertEqual({option.label for option in select.options},{'Ana','Ben','Cal'})
            select._values=['2','3']
            await select.callback(interaction)
            await view.confirm.callback(interaction)
            self.assertEqual(run.call_args.args[3],{2:'Ben',3:'Cal'})
            await view.confirm.callback(interaction)
            run.assert_awaited_once()

    async def test_picker_rejects_other_users_and_empty_selection(self):
        action=AsyncMock()
        view=bot.ParticipantPicker(1,{1:'Ana',2:'Ben'},action)
        await view.confirm.callback(self.interaction(self.ben))
        action.assert_not_awaited()
        self.assertFalse(view.submitted)
        view.selected=set()
        await view.confirm.callback(self.interaction(self.ana))
        self.assertFalse(view.submitted)
        action.assert_not_awaited()

    async def test_expense_picker_requires_someone_besides_payer(self):
        self.channel.members=[self.ana,self.ben]
        interaction=self.interaction(self.ana)
        await bot.expense.callback(interaction,48,'dinner',True)
        view=interaction.followup.send.call_args.kwargs['view']
        view.selected={1}
        await view.confirm.callback(interaction)
        self.assertFalse(view.submitted)
        self.assertEqual(bot.store.expenses(10),[])
        view.selected={2}
        await view.confirm.callback(interaction)
        self.assertEqual(bot.store.expenses(10)[0].shares,[(1,'Ana',2400),(2,'Ben',2400)])

    async def test_picker_paginates_names_and_keeps_selection(self):
        people={i:f'Person {i:02}' for i in range(1,31)}
        action=AsyncMock()
        view=bot.ParticipantPicker(1,people,action)
        first=view.children[0]
        self.assertEqual(len(first.options),25)
        first._values=['1']
        await first.callback(self.interaction(self.ana))
        await view.next_page.callback(self.interaction(self.ana))
        second=view.children[0]
        self.assertEqual(len(second.options),5)
        second._values=['30']
        await second.callback(self.interaction(self.ana))
        await view.confirm.callback(self.interaction(self.ana))
        self.assertEqual(action.call_args.args[1],{1:'Person 01',30:'Person 30'})

    async def test_picker_cancel_does_not_run_action(self):
        action=AsyncMock()
        view=bot.ParticipantPicker(1,{1:'Ana',2:'Ben'},action)
        await view.cancel.callback(self.interaction(self.ana))
        await view.confirm.callback(self.interaction(self.ana))
        action.assert_not_awaited()

    async def test_members_include_offline_people_and_refresh_incomplete_cache(self):
        self.ben.status='offline'
        self.channel.members=[self.ana,self.ben]
        self.channel.guild=NS(chunked=False,chunk=AsyncMock())
        people=await bot.load_channel_people(self.channel,self.ana)
        self.assertEqual(people,{1:'Ana',2:'Ben'})
        self.channel.guild.chunk.assert_awaited_once_with(cache=True)

    async def test_thread_people_come_from_fetched_members_not_partial_cache(self):
        thread=Mock(spec=bot.discord.Thread)
        thread.id=100
        thread.guild=NS(chunked=True,get_member=lambda uid:{1:self.ana,2:self.ben,3:self.cal}[uid])
        thread.members=[]
        thread.fetch_members=AsyncMock(return_value=[NS(id=1),NS(id=3)])
        self.assertEqual(await bot.load_channel_people(thread,self.ana),{1:'Ana',3:'Cal'})

    async def test_default_chat_group_uses_current_channel_members(self):
        self.channel.members=[self.ana,self.ben,self.cal,NS(id=99,display_name='Rotater',bot=True)]
        bot.store.add(10,'drive',1,'Old Ana')
        with patch.object(bot.client,'get_channel',return_value=self.channel):
            self.assertEqual(bot.default_people(100,10,'drive',{1:'Ana'}),{1:'Ana',2:'Ben',3:'Cal'})

    async def test_chat_records_explicit_past_time(self):
        msg = self.message(self.ana, 'I drove last night')
        await bot.on_message(msg)
        self.assertAlmostEqual(bot.store.events(10,'drive')[0].ts, msg.created_at.timestamp() - 86400, delta=1)


def tearDownModule():
    bot.store.db.close()
