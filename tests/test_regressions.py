import random
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rotation import fairness, money
from rotation.store import Store
from rotation.understand import amount_cents, occurred_at, rules


class RegressionTest(unittest.TestCase):
    def test_full_days_since_not_capped_at_thirty(self):
        store = Store(':memory:')
        self.addCleanup(store.db.close)
        now = time.time()
        store.add(1,'drive',1,'Ana',ts=now-90*86400)
        ranked = fairness.standings({1:'Ana',2:'Ben'},store.events(1,'drive'),now)
        self.assertEqual(ranked[0].weight,91)
        self.assertEqual(ranked[1].weight,31)
        self.assertTrue(fairness.is_clear(ranked))

    def test_sixty_day_window_and_money_multiplier(self):
        store = Store(':memory:')
        self.addCleanup(store.db.close)
        now = time.time()
        store.add(1,'pay',1,'Ana',ts=now-61*86400)
        ranked = fairness.standings({1:'Ana'},store.events(1,'pay'),now,{1:5000})
        self.assertEqual(ranked[0].count,0)
        self.assertEqual(ranked[0].weight,62*3)

    def test_clear_threshold_is_inclusive(self):
        rows = [fairness.Standing(1,'A',0,None,2),fairness.Standing(2,'B',0,None,1)]
        self.assertTrue(fairness.is_clear(rows))
        rows[0].weight=1.999
        self.assertFalse(fairness.is_clear(rows))

    def test_displayed_odds_always_total_one_hundred(self):
        rng = random.Random(42)
        for n in range(1,40):
            rows = [fairness.Standing(i,str(i),0,None,rng.random()+0.01) for i in range(n)]
            self.assertEqual(sum(fairness.chances(rows)),100)

    def test_minimum_payments_beats_greedy(self):
        values = [-3,-13,-10,16,10]
        bal = {i:(str(i),v) for i,v in enumerate(values)}
        pays = money.payments(bal)
        self.assertEqual(len(pays),3)
        residual = dict(enumerate(values))
        for f,_,t,_,c in pays:
            self.assertGreater(c,0)
            residual[f]+=c
            residual[t]-=c
        self.assertTrue(all(v==0 for v in residual.values()))

    def test_cents_rounding_and_rejected_amounts(self):
        self.assertEqual(money.to_cents('1.005'),101)
        self.assertEqual(amount_cents('I paid $1,234.56'),123456)
        for value in (-20,0,float('nan'),float('inf'),'nonsense'):
            with self.assertRaises(ValueError):
                money.to_cents(value)
        for text in ('I paid $-20','I paid $0','I paid $12.345',"venmo'd $-20"):
            self.assertIsNone(rules(text),text)

    def test_settlement_validation(self):
        bal={1:('Ana',3200),2:('Ben',-400),3:('Cal',-2800)}
        self.assertEqual(money.settlement_amount(bal,3,1),2800)
        self.assertEqual(money.settlement_amount(bal,3,1,2000),2000)
        for debtor,creditor,amount in ((3,1,3000),(1,1,100),(1,3,100),(2,1,-20)):
            with self.assertRaises(ValueError):
                money.settlement_amount(bal,debtor,creditor,amount)

    def test_past_calendar_dates(self):
        now=datetime(2026,10,25,18,tzinfo=timezone.utc).timestamp()
        for text,days in [('I hosted friday',2),('I drove last night',1),('I drove 65 days ago',65),('I drove',0)]:
            self.assertEqual(occurred_at(text,now,'America/Phoenix'),now-days*86400)

    def test_common_chore_phrases(self):
        examples=[('I cooked dinner','cooking'),('I made dinner','cooking'),('I did the cooking','cooking'),
                  ('I did the laundry','laundry'),('I washed the dishes','dishes'),('I vacuumed','vacuuming'),
                  ('I mopped the floor','mopping'),('I cleaned the bathroom','bathroom'),
                  ('I watered the plants','plants'),('I walked the dog','dog'),('I did this chore','chores'),
                  ("I've just cooked dinner",'cooking'),('I finished the laundry','laundry'),
                  ('I ubered last night','uber'),('I cooked dinner after we drove home','cooking')]
        for phrase, task in examples:
            with self.subTest(phrase=phrase):
                result=rules(phrase)
                self.assertIsNotNone(result)
                self.assertEqual((result.kind,result.task),('did',task))

    def test_current_turn_chat_phrases(self):
        examples=[('im cooking today','cooking'),("I'm cooking today",'cooking'),('I’m cooking rn','cooking'),
                  ('I am currently cooking dinner','cooking'),("I'm driving tonight",'drive'),
                  ('im doing the dishes','dishes'),("I'm taking out the trash",'trash'),
                  ('im getting the uber home','uber'),("I'm making dinner",'cooking'),
                  ('I am washing clothes','laundry'),('im cleaning the bathroom','bathroom'),
                  ('im handling the litter box today','litter box')]
        for phrase,task in examples:
            with self.subTest(phrase=phrase):
                result=rules(phrase,['litter box'])
                self.assertIsNotNone(result)
                self.assertEqual((result.kind,result.task),('doing',task))

    def test_current_turn_rejects_negation_questions_and_future_plans(self):
        for phrase in ["I'm not cooking today",'im not driving',"I'm cooking tomorrow",
                       'im cooking next Friday', 'am I cooking today?', "I'm cooking today?",
                       'if im cooking today', '> im cooking today', 'he said im cooking today']:
            self.assertIsNone(rules(phrase),phrase)

    def test_current_turn_does_not_log_an_unpaid_expense(self):
        intent=rules("I'm paying $48 for dinner tonight")
        self.assertEqual((intent.kind,intent.task,intent.cents),('doing','pay',None))

    def test_past_participles_and_continuous_variations_share_task_types(self):
        examples=[('I have driven today','did','drive'),("I've driven us home",'did','drive'),
                  ('I driven yesterday','did','drive'),('ive already driven','did','drive'),
                  ('I had cooked dinner','did','cooking'),("I've done the dishes",'did','dishes'),
                  ('I have taken out the trash','did','trash'),('I had gotten the uber','did','uber'),
                  ('I have mopped the floor','did','mopping'),('I emptied the bins','did','trash'),
                  ("I've been cooking dinner",'doing','cooking'),('I have been driving','doing','drive'),
                  ('I was cooking last night','did','cooking'),("I'd been cleaning the bathroom",'did','bathroom')]
        for text,kind,task in examples:
            with self.subTest(text=text):
                result=rules(text)
                self.assertIsNotNone(result)
                self.assertEqual((result.kind,result.task),(kind,task))

    def test_registered_tasks_match_regular_verb_variations(self):
        for text,task in [('I washed the car','wash car'),('im washing the car','wash car'),
                          ('I walked the dog','walk dog'),('I have cleaned the pool','clean pool')]:
            result=rules(text,[task])
            self.assertEqual(result.task,task)

    def test_chore_questions(self):
        for phrase,task in [("who's cooking?",'cooking'),('whose turn to cook?','cooking'),
                            ("who's next for uber?",'uber'),('who is up next for laundry?','laundry')]:
            self.assertEqual((rules(phrase).kind,rules(phrase).task),('ask',task))

    def test_builtin_aliases_do_not_change_with_history(self):
        for text in ('I paid for the uber', 'I paid for dinner', 'whose turn to buy snacks?'):
            baseline = rules(text)
            with_history = rules(text, ['pay','food','uber','snacks'])
            self.assertEqual(with_history, baseline)

    def test_negations_quotes_questions_are_not_logged(self):
        for text in ["I didn't drive",'Did I drive yesterday?', 'if I drove tomorrow', '> I drove', 'he said I drove']:
            self.assertIsNone(rules(text),text)
        self.assertEqual(rules('who should drive?').kind,'ask')
        self.assertEqual(rules('whose turn for costco', ['costco']).task,'costco')

    def test_recent_speakers_and_history_survive_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.db'
            store=Store(path)
            store.remember(100,1,'Ana',time.time())
            store.remember(200,2,'Ben',time.time())
            store.add(1,'drive',1,'Ana')
            store.add_task_people(1,'drive',{1:'Ana',2:'Ben'})
            store.db.close()
            store=Store(path)
            try:
                self.assertEqual(store.recent_people(100,time.time()-3600),{1:'Ana'})
                self.assertEqual(len(store.events(1,'drive')),1)
                self.assertEqual(store.task_people(1,'drive'),{1:'Ana',2:'Ben'})
                self.assertEqual(store.task_people(2,'drive'),{})
                self.assertEqual(store.events(2,'drive'),[])
            finally:
                store.db.close()

    def test_original_database_migrates_without_losing_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'old.db'
            db=sqlite3.connect(path)
            db.executescript('''CREATE TABLE events(id INTEGER PRIMARY KEY, guild_id INTEGER, task TEXT,
                user_id INTEGER, user_name TEXT, ts REAL, source TEXT, message_id INTEGER);
                INSERT INTO events VALUES(1,10,'drive',1,'Ana',123,'chat',500);''')
            db.close()
            store=Store(path)
            try:
                event=store.events(10,'drive')[0]
                self.assertEqual(event.user_name,'Ana')
                self.assertEqual(event.created_at,123)
            finally:
                store.db.close()

    def test_registered_task_survives_reopen_without_participants_or_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'tasks.db'
            store=Store(path)
            self.assertTrue(store.register_task(1,'Litter Box',123))
            self.assertFalse(store.register_task(1,'litter box',456))
            store.db.close()
            store=Store(path)
            try:
                self.assertEqual(store.tasks(1),['litter box'])
                self.assertEqual(store.tasks(2),[])
                self.assertEqual(store.events(1,'litter box'),[])
                self.assertEqual(store.task_people(1,'litter box'),{})
                store.clear_task(1,'litter box')
                self.assertEqual(store.tasks(1),[])
            finally:
                store.db.close()

    def test_removed_task_persists_and_can_be_explicitly_readded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'remove.db'
            store=Store(path)
            store.register_task(1,'litter box',123)
            store.remove_task_type(1,'litter box',123,['litter box'])
            store.db.close()
            store=Store(path)
            try:
                self.assertTrue(store.is_task_removed(1,'litter box'))
                self.assertFalse(store.is_task_removed(2,'litter box'))
                self.assertEqual(store.tasks(1),[])
                self.assertTrue(store.register_task(1,'litter box',456))
                self.assertFalse(store.is_task_removed(1,'litter box'))
                self.assertEqual(store.task_creator(1,'litter box'),456)
            finally:
                store.db.close()

    def test_ledger_version_changes_on_add_and_undo(self):
        store=Store(':memory:')
        self.addCleanup(store.db.close)
        eid=store.add_expense(1,1,'Ana',100,'food',[(2,'Ben',100)])
        first=store.version(1)
        store.remove_expense(eid)
        self.assertGreater(store.version(1),first)
        self.assertEqual(store.version(2),0)
