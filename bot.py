"""The Rotation: Discord bot that keeps a friend group's turns and money fair.

Run:  python bot.py   (needs DISCORD_TOKEN in the environment or a .env file next to this script)
"""

from __future__ import annotations

import asyncio
import os
import logging
import tempfile
import time
from pathlib import Path

import discord
from discord import app_commands

from rotation import fairness, money, wheel
from rotation.store import Store
from rotation.participants import ParticipantPicker
from rotation.understand import MONEY_TASKS, TASKS, understand, occurred_at, canonical_task

HERE = Path(__file__).parent
RECENT_HOURS = 12
UNDO_EMOJI = "❌"
GREEN, ORANGE, BLUE, PURPLE = 0x1DD1A1, 0xFF9F43, 0x54A0FF, 0x6C5CE7


def load_env() -> None:
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
store = Store(os.environ.get("ROTATION_DB", HERE / "rotation.db"))
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
tree = app_commands.CommandTree(client)

logger = logging.getLogger("rotation")


# ---------- who's in ----------

def recent_people(channel_id: int) -> dict[int, str]:
    cutoff = time.time() - RECENT_HOURS * 3600
    return store.recent_people(channel_id, cutoff)


def tracked_tasks(guild_id: int) -> list[str]:
    return sorted({canonical_task(task) for task in store.tasks(guild_id)})


def task_events(guild_id: int, task: str):
    names = [name for name in store.tasks(guild_id) if canonical_task(name) == task]
    events = [event for name in names for event in store.events(guild_id, name)]
    return sorted(events, key=lambda event: (event.ts, event.id))


def task_people(guild_id: int, task: str) -> dict[int, str]:
    people = {}
    for name in store.tasks(guild_id):
        if canonical_task(name) == task:
            people.update(store.task_people(guild_id, name))
    for event in task_events(guild_id, task):
        people[event.user_id] = event.user_name
    return people


def channel_people(channel) -> dict[int, str]:
    """Current humans who belong to this channel; channel.members respects visibility."""
    if channel is None:
        return {}
    members = getattr(channel, "members", ())
    if isinstance(channel, discord.Thread):
        members = [channel.guild.get_member(member.id) for member in members]
    return {member.id: member.display_name for member in members if member is not None and not member.bot}


async def load_channel_people(channel, asker) -> dict[int, str]:
    guild = getattr(channel, "guild", None)
    if guild and not guild.chunked:
        await guild.chunk(cache=True)
    if isinstance(channel, discord.Thread):
        joined = await channel.fetch_members()
        members = [channel.guild.get_member(member.id) for member in joined]
        people = {member.id: member.display_name for member in members if member is not None and not member.bot}
    else:
        people = channel_people(channel)
    if not people:
        people = recent_people(channel.id)
    if not asker.bot:
        people[asker.id] = asker.display_name
    # Keep the caller first; other names have a stable order for splits.
    return {uid: people[uid] for uid in sorted(people, key=lambda uid: (uid != asker.id, people[uid].casefold(), uid))}


def default_people(channel_id: int, guild_id: int, task: str, base: dict[int, str]) -> dict[int, str]:
    """Use this channel's humans; saved groups/history are a fallback when uncached."""
    people = channel_people(client.get_channel(channel_id))
    if people:
        people.update(base)
        return people
    people = task_people(guild_id, task)
    if not people:
        people.update(recent_people(channel_id))
        people.update(base)
    return people


def mentioned(message: discord.Message) -> dict[int, str]:
    return {m.id: m.display_name for m in message.mentions if not m.bot and m.id != message.author.id}


# ---------- turns ----------

def standings_embed(title: str, task: str, ranked, highlight: int | None, color: int) -> discord.Embed:
    em = discord.Embed(title=title, color=color)
    lines = []
    for s, pct in zip(ranked, fairness.chances(ranked)):
        safe_name = discord.utils.escape_markdown(s.name)
        name = f"**{safe_name}**" if s.user_id == highlight else safe_name
        lines.append(f"{name} · {pct}% · {fairness.describe(s)}")
    em.description = "\n".join(lines)
    extra = " (+ what you owe)" if task in MONEY_TASKS else ""
    em.set_footer(text=f"{task} · slice size = how overdue you are{extra}")
    return em


def task_standings(guild_id: int, task: str):
    owes = money.owes(money.balances(store.expenses(guild_id))) if task in MONEY_TASKS else None
    return fairness.standings(task_people(guild_id, task), task_events(guild_id, task), owes_cents=owes)


def next_turn_summary(task: str, ranked) -> str:
    label = discord.utils.escape_markdown(task)
    if not ranked:
        return f"No participants for **{label}** yet."
    name = discord.utils.escape_markdown(ranked[0].name)
    if len(ranked) == 1:
        return (f"🔁 **{label}**: only **{name}** is tracked so far. "
                f"Run `/spin task:{task}` to include the people in this chat.")
    if fairness.is_clear(ranked):
        return f"🔁 Up next for **{label}**: **{name}**."
    return (f"🎡 Next for **{label}**: **spin needed** — **{name}** is most overdue. "
            f"Use `/spin task:{task}` to choose.")


def log_confirmation(guild_id: int, task: str, name: str, *, active: bool = False) -> str:
    action = "is handling" if active else "did"
    return (f"📝 Logged: {discord.utils.escape_markdown(name)} {action} **{discord.utils.escape_markdown(task)}**.\n"
            + next_turn_summary(task, task_standings(guild_id, task)))


def last_done_line(events) -> str:
    if not events:
        return "Last done: no completed turns yet."
    last = max(events, key=lambda event: (event.ts, event.id))
    label = "Current turn" if last.source == "chat-active" else "Last done"
    return f"{label}: **{discord.utils.escape_markdown(last.user_name)}** · <t:{int(last.ts)}:R>"


def task_overview_embeds(guild_id: int) -> list[discord.Embed]:
    tasks = tracked_tasks(guild_id)
    if not tasks:
        return [discord.Embed(title="📋 Task rotations", color=BLUE,
                              description='No tasks logged yet. Say "I drove", "I cooked dinner", or "I did the dishes".')]
    embeds = []
    for offset in range(0, len(tasks), 10):
        em = discord.Embed(title="📋 Who's next for each task?", color=BLUE)
        for task in tasks[offset:offset + 10]:
            em.add_field(name=task, value=next_turn_summary(task, task_standings(guild_id, task))
                         + "\n" + last_done_line(task_events(guild_id, task)), inline=False)
        em.set_footer(text="/tally task:<name> shows everyone's odds and task history")
        embeds.append(em)
    return embeds


class TurnView(discord.ui.View):
    def __init__(self, guild_id: int, task: str, people: dict[int, str], chosen_id: int, chosen_name: str,
                 clear: bool):
        super().__init__(timeout=3600)
        self.guild_id, self.task, self.people = guild_id, task, people
        self.chosen_id, self.chosen_name = chosen_id, chosen_name
        self.claimed = False
        if not clear:
            self.remove_item(self.spin_anyway)

    async def claim(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self.people:
            await interaction.response.send_message("Only people in this rotation can use these buttons.", ephemeral=True)
            return False
        if self.claimed:
            await interaction.response.send_message("This turn was already handled. Ask for a new rotation.", ephemeral=True)
            return False
        self.claimed = True
        return True

    async def _finish(self, interaction: discord.Interaction, note: str) -> None:
        for item in self.children:
            item.disabled = True
        em = interaction.message.embeds[0] if interaction.message.embeds else None
        if em:
            em.set_footer(text=note)
        await interaction.response.edit_message(embed=em, view=self)
        self.stop()

    @discord.ui.button(label="Done, log it", emoji="✅", style=discord.ButtonStyle.success)
    async def done(self, interaction: discord.Interaction, _b: discord.ui.Button):
        if not await self.claim(interaction):
            return
        store.add(self.guild_id, self.task, self.chosen_id, self.chosen_name, source="button",
                  actor_id=interaction.user.id)
        await self._finish(interaction, f"✅ Logged: {self.chosen_name} did {self.task}")

    @discord.ui.button(label="Can't, spin without me", emoji="🙅", style=discord.ButtonStyle.secondary)
    async def cant(self, interaction: discord.Interaction, _b: discord.ui.Button):
        if interaction.user.id != self.chosen_id:
            await interaction.response.send_message(f"Only {self.chosen_name} can pass on this one 👀",
                                                    ephemeral=True)
            return
        if not await self.claim(interaction):
            return
        await self._finish(interaction, f"🙅 {self.chosen_name} passed")
        others = {k: v for k, v in self.people.items() if k != self.chosen_id}
        await run_turn(interaction.channel, self.guild_id, self.task, others, force_spin=True)

    @discord.ui.button(label="Spin anyway", emoji="🎡", style=discord.ButtonStyle.primary)
    async def spin_anyway(self, interaction: discord.Interaction, _b: discord.ui.Button):
        if not await self.claim(interaction):
            return
        await self._finish(interaction, "🎡 Spinning instead…")
        await run_turn(interaction.channel, self.guild_id, self.task, self.people, force_spin=True)


async def run_turn(channel, guild_id: int, task: str, people: dict[int, str], force_spin: bool = False) -> None:
    if not people:
        await channel.send(f"Nobody's in yet. Mention who's in: `whose turn to {task} @friend @friend`")
        return

    store.add_task_people(guild_id, task, people)
    owes = money.owes(money.balances(store.expenses(guild_id))) if task in MONEY_TASKS else None
    ranked = fairness.standings(people, task_events(guild_id, task), owes_cents=owes)

    if len(ranked) == 1 or (not force_spin and fairness.is_clear(ranked)):
        top = ranked[0]
        em = standings_embed(f"🔁 Up in the rotation: {top.name} ({task})", task, ranked, top.user_id, GREEN)
        await channel.send(embed=em, view=TurnView(guild_id, task, people, top.user_id, top.name, clear=True))
        return

    idx = fairness.pick(ranked)
    winner = ranked[idx]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "spin.gif"
        await asyncio.get_running_loop().run_in_executor(
            None, wheel.render, [s.name for s in ranked], [s.weight for s in ranked], idx, path)
        await channel.send(f"🎡 Spinning for **{task}**…" if force_spin else f"🎡 Too close to call for **{task}**. Spinning…", file=discord.File(path))
    await asyncio.sleep(3.5)
    em = standings_embed(f"🎡 The wheel says {winner.name} ({task})", task, ranked, winner.user_id, ORANGE)
    await channel.send(embed=em, view=TurnView(guild_id, task, people, winner.user_id, winner.name, clear=False))


# ---------- money ----------

def owe_embed(guild_id: int, page: int = 0) -> tuple[discord.Embed, list]:
    pays = money.payments(money.balances(store.expenses(guild_id)))
    em = discord.Embed(title="💸 Who owes who", color=PURPLE)
    if not pays:
        em.description = "Everyone's square ✅"
    else:
        page = min(page, (len(pays) - 1) // 20)
        visible = pays[page * 20:(page + 1) * 20]
        em.description = "\n".join(f"{discord.utils.escape_markdown(f)} → **{discord.utils.escape_markdown(t)}** {money.fmt(c)}"
                                   for _, f, _, t, c in visible)
        pages = (len(pays) + 19) // 20
        em.set_footer(text=f"Fewest payments to square everyone up. Tap when it's paid. · {page + 1}/{pages}")
    return em, pays


class SettleButton(discord.ui.Button):
    def __init__(self, guild_id: int, pay):
        self.guild_id, self.pay = guild_id, pay
        self.version = store.version(guild_id)
        self.completed = False
        f_id, f_name, t_id, t_name, cents = pay
        super().__init__(label=f"{f_name} paid {t_name} {money.fmt(cents)}"[:80], emoji="🤝",
                         style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        f_id, f_name, t_id, t_name, cents = self.pay
        if interaction.user.id not in (f_id, t_id):
            await interaction.response.send_message(f"Only {f_name} or {t_name} can mark this paid.", ephemeral=True)
            return
        if self.completed or self.version != store.version(self.guild_id):
            await interaction.response.send_message("Balances changed since this card. Run /owe for the current payments.", ephemeral=True)
            return
        cents = money.settlement_amount(money.balances(store.expenses(self.guild_id)), f_id, t_id, cents)
        self.completed = True
        store.add_expense(self.guild_id, f_id, f_name, cents, "settle up", [(t_id, t_name, cents)],
                          kind="settle", actor_id=interaction.user.id)
        em, pays = owe_embed(self.guild_id)
        await interaction.response.edit_message(embed=em, view=OweView(self.guild_id, pays))
        await interaction.followup.send(f"🤝 {f_name} squared up with {t_name} ({money.fmt(cents)})")


class OwePageButton(discord.ui.Button):
    def __init__(self, guild_id: int, page: int, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.guild_id, self.page = guild_id, page

    async def callback(self, interaction: discord.Interaction):
        em, pays = owe_embed(self.guild_id, self.page)
        page = min(self.page, max(0, (len(pays) - 1) // 20))
        await interaction.response.edit_message(embed=em, view=OweView(self.guild_id, pays, page))


class OweView(discord.ui.View):
    def __init__(self, guild_id: int, pays, page: int = 0):
        super().__init__(timeout=3600)
        for p in pays[page * 20:(page + 1) * 20]:
            self.add_item(SettleButton(guild_id, p))
        if page:
            self.add_item(OwePageButton(guild_id, page - 1, "Previous payments"))
        if (page + 1) * 20 < len(pays):
            self.add_item(OwePageButton(guild_id, page + 1, "More payments"))


def record_expense(guild_id: int, payer, people: dict[int, str], cents: int, note: str,
                   message_id: int | None = None, log_turn: bool = False) -> str:
    order = [(payer.id, payer.display_name)] + [(u, n) for u, n in people.items() if u != payer.id]
    shares = money.split(cents, order)
    if log_turn:
        store.add(guild_id, "pay", payer.id, payer.display_name, source="command", message_id=message_id)
    store.add_expense(guild_id, payer.id, payer.display_name, cents, note or "", shares, message_id=message_id)
    others = " · ".join(f"{n} {money.fmt(c)}" for u, n, c in shares if u != payer.id)
    what = f" for {discord.utils.escape_markdown(note)}" if note else ""
    return f"💸 {payer.display_name} paid {money.fmt(cents)}{what} · split {len(shares)} ways: {others}"


# ---------- reading the chat ----------

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    store.remember(message.channel.id, message.author.id, message.author.display_name, time.time())

    intent = await understand(message.content, store.tasks(message.guild.id))
    if not intent or not store.claim_message(message.id):
        return
    gid, author = message.guild.id, message.author
    if intent.kind in ("did", "doing", "ask"):
        intent.task = normalize_task(intent.task)

    if intent.kind == "settle":
        target = next(iter(mentioned(message).items()), None)
        if not target and message.reference:
            resolved = message.reference.resolved
            if not isinstance(resolved, discord.Message) and message.reference.message_id:
                try:
                    resolved = await message.channel.fetch_message(message.reference.message_id)
                except discord.HTTPException:
                    resolved = None
            ref = resolved.author if isinstance(resolved, discord.Message) else None
            if ref and not ref.bot and ref.id != author.id:
                target = (ref.id, ref.display_name)
        if not target:
            pays = money.payments(money.balances(store.expenses(gid)))
            recipients = {(t_id, t_name) for f_id, _, t_id, t_name, _ in pays if f_id == author.id}
            if len(recipients) == 1:
                target = next(iter(recipients))
        if not target:
            await message.reply("Who did you pay back? Mention them, reply to their message, or use /settle.", mention_author=False)
            return
        t_id, t_name = target
        try:
            cents = money.settlement_amount(money.balances(store.expenses(gid)), author.id, t_id, intent.cents)
        except ValueError as exc:
            await message.reply(str(exc), mention_author=False)
            return
        store.add_expense(gid, author.id, author.display_name, cents, "settle up", [(t_id, t_name, cents)],
                          kind="settle", message_id=message.id)
        await message.add_reaction("🤝")
        return

    if intent.kind in ("did", "doing"):
        active = intent.kind == "doing"
        people = default_people(message.channel.id, gid, intent.task, {author.id: author.display_name})
        people[author.id] = author.display_name
        store.add_task_people(gid, intent.task, people)
        timestamp = message.created_at.timestamp()
        if not active:
            timestamp = occurred_at(message.content, timestamp, os.environ.get("ROTATION_TIMEZONE", "UTC"))
        store.add(gid, intent.task, author.id, author.display_name,
                  source="chat-active" if active else "chat", message_id=message.id, ts=timestamp)
        try:
            await message.add_reaction("📝")
        except discord.HTTPException:
            logger.warning("Task logged, but could not add the note reaction in channel %s", message.channel.id)
        if intent.cents:
            people = {author.id: author.display_name, **mentioned(message)}
            if len(people) < 2:
                people = {author.id: author.display_name, **recent_people(message.channel.id)}
            if len(people) < 2:
                await message.reply(log_confirmation(gid, intent.task, author.display_name, active=active)
                                    + f"\n💸 Got {money.fmt(intent.cents)}. Who was it split with? Use `/expense` with friends.",
                                    mention_author=False)
                return
            line = record_expense(gid, author, people, intent.cents, intent.note, message_id=message.id)
            text = log_confirmation(gid, intent.task, author.display_name, active=active) + "\n" + line
        else:
            text = log_confirmation(gid, intent.task, author.display_name, active=active)
        await message.reply(text + f"\n-# Not right? React {UNDO_EMOJI} on your original message to undo.", mention_author=False)
        return

    base = {author.id: author.display_name}
    picked = mentioned(message)
    people = {**base, **picked} if picked else default_people(message.channel.id, gid, intent.task, base)
    await run_turn(message.channel, gid, intent.task, people)


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != UNDO_EMOJI:
        return
    event = store.by_message(payload.message_id)
    expense = store.expense_by_message(payload.message_id)
    owner = event.user_id if event else expense.payer_id if expense else None
    if owner is None or payload.user_id != owner:
        return
    if event:
        store.remove(event.id)
    if expense:
        store.remove_expense(expense.id)
    channel = client.get_channel(payload.channel_id)
    if channel:
        msg = await channel.fetch_message(payload.message_id)
        for emoji in ("📝", "🤝"):
            try:
                await msg.remove_reaction(emoji, client.user)
            except discord.HTTPException:
                pass
        await msg.add_reaction("↩️")


# ---------- slash commands ----------

def normalize_task(task: str) -> str:
    task = canonical_task(task)
    if not task or len(task) > 40:
        raise ValueError("Task names must contain 1–40 characters.")
    return task


@tree.error
async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if isinstance(original, (ValueError, app_commands.CheckFailure)):
        text = str(original)
    elif isinstance(original, discord.Forbidden):
        text = "I need permission to send messages, attach files, embed links, add reactions, and read history here."
    else:
        logger.error("Command failed", exc_info=(type(original), original, original.__traceback__))
        text = "Something went wrong. Try again; details are in the bot log."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def task_autocomplete(interaction: discord.Interaction, current: str):
    names = sorted(set(tracked_tasks(interaction.guild_id)) | set(TASKS))
    return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n][:25]


@tree.command(name="spin", description="Spin for a task using the people in this chat")
@app_commands.guild_only()
@app_commands.describe(task="drive, uber, cooking, dishes…", choose_people="Optional: pick from people's names instead of using everyone")
@app_commands.autocomplete(task=task_autocomplete)
async def spin(interaction: discord.Interaction, task: str, choose_people: bool = False):
    task = normalize_task(task)
    await interaction.response.defer(ephemeral=choose_people)
    people = await load_channel_people(interaction.channel, interaction.user)
    if choose_people:
        async def start(selected_interaction, selected):
            await run_turn(selected_interaction.channel, selected_interaction.guild_id, task, selected, force_spin=True)
        view = ParticipantPicker(interaction.user.id, people, start, label="Spin with these people")
        await interaction.followup.send(view.description(), view=view, ephemeral=True)
        return
    await interaction.followup.send(f"🎡 Wheel coming up for **{discord.utils.escape_markdown(task)}** with {len(people)} people from this chat…")
    await run_turn(interaction.channel, interaction.guild_id, task, people, force_spin=True)


@tree.command(name="tally", description="See who's next for every task, or the odds for one task")
@app_commands.guild_only()
@app_commands.describe(task="Optional: pick one task, or leave blank to see all task rotations")
@app_commands.autocomplete(task=task_autocomplete)
async def tally(interaction: discord.Interaction, task: str | None = None):
    if task is None:
        embeds = task_overview_embeds(interaction.guild_id)
        await interaction.response.send_message(embed=embeds[0])
        for em in embeds[1:]:
            await interaction.followup.send(embed=em)
        return
    task = normalize_task(task)
    events = task_events(interaction.guild_id, task)
    ranked = task_standings(interaction.guild_id, task)
    if not ranked:
        await interaction.response.send_message(f'No history for **{discord.utils.escape_markdown(task)}** yet. '
                                                'Say what you did in chat to start tracking it.', ephemeral=True)
        return
    em = standings_embed(f"📊 {task} rotation", task, ranked, ranked[0].user_id, BLUE)
    em.description = (next_turn_summary(task, ranked) + "\n" + last_done_line(events)
                      + "\n\n" + em.description)
    await interaction.response.send_message(embed=em)


@tree.command(name="log", description="Log that someone did a task")
@app_commands.guild_only()
@app_commands.autocomplete(task=task_autocomplete)
async def log(interaction: discord.Interaction, task: str, who: discord.Member | None = None):
    who = who or interaction.user
    task = normalize_task(task)
    if who.bot:
        raise ValueError("Choose a person, rather than a bot.")
    people = default_people(interaction.channel_id, interaction.guild_id, task, {who.id: who.display_name})
    people[who.id] = who.display_name
    store.add_task_people(interaction.guild_id, task, people)
    store.add(interaction.guild_id, task, who.id, who.display_name, source="command", actor_id=interaction.user.id)
    await interaction.response.send_message(log_confirmation(interaction.guild_id, task, who.display_name))


@tree.command(name="expense", description="Log your expense and split it with the people in this chat")
@app_commands.guild_only()
@app_commands.describe(amount="dollars, like 48 or 23.50", note="what it was for",
                       choose_people="Optional: choose who shares this expense by name")
async def expense(interaction: discord.Interaction, amount: float, note: str, choose_people: bool = False):
    cents = money.to_cents(amount)
    await interaction.response.defer(ephemeral=choose_people)
    people = await load_channel_people(interaction.channel, interaction.user)
    if not any(uid != interaction.user.id for uid in people):
        raise ValueError("There needs to be at least one other person in this chat to split an expense.")
    if choose_people:
        async def save(selected_interaction, selected):
            if not any(uid != selected_interaction.user.id for uid in selected):
                await selected_interaction.followup.send("Choose at least one other person to split with.", ephemeral=True)
                return
            line = record_expense(selected_interaction.guild_id, selected_interaction.user, selected, cents, note,
                                  message_id=selected_interaction.id, log_turn=True)
            await selected_interaction.followup.send(line, ephemeral=False)
        view = ParticipantPicker(interaction.user.id, people, save, label="Split with these people",
                                 other_than=interaction.user.id)
        await interaction.followup.send(view.description() + " You are always included as the payer.", view=view, ephemeral=True)
        return
    line = record_expense(interaction.guild_id, interaction.user, people, cents, note,
                          message_id=interaction.id, log_turn=True)
    await interaction.followup.send(line)


@tree.command(name="owe", description="Who owes who, with the fewest payments to square up")
@app_commands.guild_only()
async def owe(interaction: discord.Interaction):
    em, pays = owe_embed(interaction.guild_id)
    await interaction.response.send_message(embed=em, view=OweView(interaction.guild_id, pays))


@tree.command(name="settle", description="You paid someone back")
@app_commands.guild_only()
@app_commands.describe(amount="leave empty to pay back everything you owe them")
async def settle(interaction: discord.Interaction, who: discord.Member, amount: float | None = None):
    gid, me = interaction.guild_id, interaction.user
    if who.bot:
        raise ValueError("Choose a person, rather than a bot.")
    cents = money.settlement_amount(money.balances(store.expenses(gid)), me.id, who.id,
                                    money.to_cents(amount) if amount is not None else None)
    store.add_expense(gid, me.id, me.display_name, cents, "settle up", [(who.id, who.display_name, cents)],
                      kind="settle")
    await interaction.response.send_message(f"🤝 {me.display_name} paid {who.display_name} back {money.fmt(cents)}")


@tree.command(name="undo", description="Undo the last turn or payment logged in this server")
@app_commands.guild_only()
async def undo(interaction: discord.Interaction):
    ev, ex = store.last(interaction.guild_id), store.last_expense(interaction.guild_id)
    if not ev and not ex:
        await interaction.response.send_message("Nothing to undo.", ephemeral=True)
        return
    latest = ex if ex and (not ev or ex.created_at >= ev.created_at) else ev
    owner = latest.actor_id or (latest.user_id if hasattr(latest, "user_id") else latest.payer_id)
    if interaction.user.id != owner and not interaction.permissions.manage_guild:
        await interaction.response.send_message("Only the person who logged it or a server manager can undo it.", ephemeral=True)
        return
    if latest is ex:
        store.remove_expense(ex.id)
        linked = store.by_message(ex.message_id) if ex.message_id else None
        if linked:
            store.remove(linked.id)
        await interaction.response.send_message(f"↩️ Removed: {ex.payer_name} {money.fmt(ex.cents)} ({ex.note})")
    else:
        store.remove(ev.id)
        await interaction.response.send_message(f"↩️ Removed: {ev.user_name} did **{ev.task}**")


# ---------- startup ----------

@client.event
async def on_ready():
    for guild in client.guilds:
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
    print(f"The Rotation is online as {client.user} in {len(client.guilds)} server(s).")


@client.event
async def on_guild_join(guild: discord.Guild):
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token or token == "paste-your-bot-token-here":
        raise SystemExit("Set DISCORD_TOKEN in a .env file next to bot.py (see .env.example).")
    client.run(token)
