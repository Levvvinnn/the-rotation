"""The Rotation: Discord bot that keeps a friend group's turns and money fair.

Run:  python bot.py   (needs DISCORD_TOKEN in the environment or a .env file next to this script)
"""

from __future__ import annotations

import asyncio
import os
import random
import tempfile
import time
from pathlib import Path

import discord
from discord import app_commands

from rotation import fairness, money, wheel
from rotation.store import Store
from rotation.understand import MONEY_TASKS, TASKS, understand

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
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# channel id -> {user id: (display name, last message time)}
recent: dict[int, dict[int, tuple[str, float]]] = {}


# ---------- who's in ----------

def recent_people(channel_id: int) -> dict[int, str]:
    cutoff = time.time() - RECENT_HOURS * 3600
    return {uid: name for uid, (name, ts) in recent.get(channel_id, {}).items() if ts >= cutoff}


def default_people(channel_id: int, guild_id: int, task: str, base: dict[int, str]) -> dict[int, str]:
    """Nobody mentioned: everyone who has done this task before, plus the asker.
    For a brand new task, everyone who talked in this channel recently."""
    people = dict(base)
    events = store.events(guild_id, task)
    for e in events:
        people.setdefault(e.user_id, e.user_name)
    if not events:
        for uid, name in recent_people(channel_id).items():
            people.setdefault(uid, name)
    return people


def mentioned(message: discord.Message) -> dict[int, str]:
    return {m.id: m.display_name for m in message.mentions if not m.bot and m.id != message.author.id}


# ---------- turns ----------

def standings_embed(title: str, task: str, ranked, highlight: int | None, color: int) -> discord.Embed:
    em = discord.Embed(title=title, color=color)
    lines = []
    for s, pct in zip(ranked, fairness.chances(ranked)):
        name = f"**{s.name}**" if s.user_id == highlight else s.name
        lines.append(f"{name} · {pct}% · {fairness.describe(s)}")
    em.description = "\n".join(lines)
    extra = " (+ what you owe)" if task in MONEY_TASKS else ""
    em.set_footer(text=f"{task} · slice size = how overdue you are{extra}")
    return em


class TurnView(discord.ui.View):
    def __init__(self, guild_id: int, task: str, people: dict[int, str], chosen_id: int, chosen_name: str,
                 clear: bool):
        super().__init__(timeout=3600)
        self.guild_id, self.task, self.people = guild_id, task, people
        self.chosen_id, self.chosen_name = chosen_id, chosen_name
        if not clear:
            self.remove_item(self.spin_anyway)

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
        store.add(self.guild_id, self.task, self.chosen_id, self.chosen_name, source="button")
        await self._finish(interaction, f"✅ Logged: {self.chosen_name} did {self.task}")

    @discord.ui.button(label="Can't, spin without me", emoji="🙅", style=discord.ButtonStyle.secondary)
    async def cant(self, interaction: discord.Interaction, _b: discord.ui.Button):
        if interaction.user.id != self.chosen_id:
            await interaction.response.send_message(f"Only {self.chosen_name} can pass on this one 👀",
                                                    ephemeral=True)
            return
        await self._finish(interaction, f"🙅 {self.chosen_name} passed")
        others = {k: v for k, v in self.people.items() if k != self.chosen_id}
        await run_turn(interaction.channel, self.guild_id, self.task, others, force_spin=True)

    @discord.ui.button(label="Spin anyway", emoji="🎡", style=discord.ButtonStyle.primary)
    async def spin_anyway(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await self._finish(interaction, "🎡 Spinning instead…")
        await run_turn(interaction.channel, self.guild_id, self.task, self.people, force_spin=True)


async def run_turn(channel, guild_id: int, task: str, people: dict[int, str], force_spin: bool = False) -> None:
    if len(people) < 2:
        hint = f"Mention who's in: `whose turn to {task} @friend @friend`"
        if people:
            (_, name), = people.items()
            await channel.send(f"Only {name} is in, so it's {name} 😅 {hint}")
        else:
            await channel.send(f"Nobody's in yet. {hint}")
        return

    owes = money.owes(money.balances(store.expenses(guild_id))) if task in MONEY_TASKS else None
    ranked = fairness.standings(people, store.events(guild_id, task), owes_cents=owes)

    if not force_spin and fairness.is_clear(ranked):
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
        await channel.send(f"🎡 Too close to call for **{task}**. Spinning…", file=discord.File(path))
    await asyncio.sleep(3.5)
    em = standings_embed(f"🎡 The wheel says {winner.name} ({task})", task, ranked, winner.user_id, ORANGE)
    await channel.send(embed=em, view=TurnView(guild_id, task, people, winner.user_id, winner.name, clear=False))


# ---------- money ----------

def owe_embed(guild_id: int) -> tuple[discord.Embed, list]:
    pays = money.payments(money.balances(store.expenses(guild_id)))
    em = discord.Embed(title="💸 Who owes who", color=PURPLE)
    if not pays:
        em.description = "Everyone's square ✅"
    else:
        em.description = "\n".join(f"{f} → **{t}** {money.fmt(c)}" for _, f, _, t, c in pays)
        em.set_footer(text="Fewest payments to square everyone up. Tap when it's paid.")
    return em, pays


class SettleButton(discord.ui.Button):
    def __init__(self, guild_id: int, pay):
        self.guild_id, self.pay = guild_id, pay
        f_id, f_name, t_id, t_name, cents = pay
        super().__init__(label=f"{f_name} paid {t_name} {money.fmt(cents)}", emoji="🤝",
                         style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        f_id, f_name, t_id, t_name, cents = self.pay
        if interaction.user.id not in (f_id, t_id):
            await interaction.response.send_message(f"Only {f_name} or {t_name} can mark this paid.", ephemeral=True)
            return
        store.add_expense(self.guild_id, f_id, f_name, cents, "settle up", [(t_id, t_name, cents)], kind="settle")
        em, pays = owe_embed(self.guild_id)
        await interaction.response.edit_message(embed=em, view=OweView(self.guild_id, pays))
        await interaction.followup.send(f"🤝 {f_name} squared up with {t_name} ({money.fmt(cents)})")


class OweView(discord.ui.View):
    def __init__(self, guild_id: int, pays):
        super().__init__(timeout=3600)
        for p in pays[:5]:
            self.add_item(SettleButton(guild_id, p))


def record_expense(guild_id: int, payer, people: dict[int, str], cents: int, note: str,
                   message_id: int | None = None) -> str:
    order = [(payer.id, payer.display_name)] + [(u, n) for u, n in people.items() if u != payer.id]
    shares = money.split(cents, order)
    store.add_expense(guild_id, payer.id, payer.display_name, cents, note or "", shares, message_id=message_id)
    others = " · ".join(f"{n} {money.fmt(c)}" for u, n, c in shares if u != payer.id)
    what = f" for {note}" if note else ""
    return f"💸 {payer.display_name} paid {money.fmt(cents)}{what} · split {len(shares)} ways: {others}"


# ---------- reading the chat ----------

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    recent.setdefault(message.channel.id, {})[message.author.id] = (message.author.display_name, time.time())

    intent = await understand(message.content, store.tasks(message.guild.id))
    if not intent:
        return
    gid, author = message.guild.id, message.author

    if intent.kind == "settle":
        target = next(iter(mentioned(message).items()), None)
        if not target and message.reference and isinstance(message.reference.resolved, discord.Message):
            ref = message.reference.resolved.author
            if not ref.bot and ref.id != author.id:
                target = (ref.id, ref.display_name)
        if not target:
            return
        t_id, t_name = target
        pays = money.payments(money.balances(store.expenses(gid)))
        owed = money.owed_between(pays, author.id, t_id)
        cents = intent.cents or owed
        if not cents:
            await message.reply(f"You don't owe {t_name} anything right now 👍", mention_author=False)
            return
        store.add_expense(gid, author.id, author.display_name, cents, "settle up", [(t_id, t_name, cents)],
                          kind="settle", message_id=message.id)
        await message.add_reaction("🤝")
        return

    if intent.kind == "did":
        store.add(gid, intent.task, author.id, author.display_name, source="chat", message_id=message.id)
        await message.add_reaction("📝")
        if intent.cents:
            people = {author.id: author.display_name, **mentioned(message)}
            if len(people) < 2:
                people = {author.id: author.display_name, **recent_people(message.channel.id)}
            if len(people) < 2:
                await message.reply(f"💸 Got {money.fmt(intent.cents)}. Who was it split with? "
                                    f"`/expense` to split it.", mention_author=False)
                return
            line = record_expense(gid, author, people, intent.cents, intent.note, message_id=message.id)
            await message.reply(f"{line}\n-# not right? react {UNDO_EMOJI} to undo", mention_author=False)
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

async def task_autocomplete(interaction: discord.Interaction, current: str):
    names = sorted(set(store.tasks(interaction.guild_id)) | set(TASKS))
    return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n][:25]


def members_dict(*members) -> dict[int, str]:
    return {m.id: m.display_name for m in members if m is not None and not m.bot}


@tree.command(name="spin", description="Spin the wheel for a task")
@app_commands.describe(task="drive, uber, host, pay…")
@app_commands.autocomplete(task=task_autocomplete)
async def spin(interaction: discord.Interaction, task: str, friend1: discord.Member | None = None,
               friend2: discord.Member | None = None, friend3: discord.Member | None = None,
               friend4: discord.Member | None = None):
    task = task.lower().strip()
    base = {interaction.user.id: interaction.user.display_name}
    picked = members_dict(friend1, friend2, friend3, friend4)
    people = {**base, **picked} if picked else default_people(interaction.channel_id, interaction.guild_id,
                                                               task, base)
    await interaction.response.send_message(f"🎡 Wheel coming up for **{task}**…")
    await run_turn(interaction.channel, interaction.guild_id, task, people, force_spin=True)


@tree.command(name="tally", description="See who's done a task and who's overdue")
@app_commands.autocomplete(task=task_autocomplete)
async def tally(interaction: discord.Interaction, task: str):
    task = task.lower().strip()
    events = store.events(interaction.guild_id, task)
    if not events:
        await interaction.response.send_message(f"No history for **{task}** yet.", ephemeral=True)
        return
    owes = money.owes(money.balances(store.expenses(interaction.guild_id))) if task in MONEY_TASKS else None
    ranked = fairness.standings({e.user_id: e.user_name for e in events}, events, owes_cents=owes)
    em = standings_embed(f"📊 {task}: who's next in the rotation", task, ranked, ranked[0].user_id, BLUE)
    await interaction.response.send_message(embed=em)


@tree.command(name="log", description="Log that someone did a task")
@app_commands.autocomplete(task=task_autocomplete)
async def log(interaction: discord.Interaction, task: str, who: discord.Member | None = None):
    who = who or interaction.user
    store.add(interaction.guild_id, task.lower().strip(), who.id, who.display_name, source="command")
    await interaction.response.send_message(f"📝 Logged: {who.display_name} did **{task.lower().strip()}**")


@tree.command(name="expense", description="You paid for something; split it")
@app_commands.describe(amount="dollars, like 48 or 23.50", note="what it was for")
async def expense(interaction: discord.Interaction, amount: float, note: str, friend1: discord.Member,
                  friend2: discord.Member | None = None, friend3: discord.Member | None = None,
                  friend4: discord.Member | None = None):
    cents = int(round(amount * 100))
    if cents <= 0:
        await interaction.response.send_message("Amount has to be more than $0.", ephemeral=True)
        return
    people = members_dict(friend1, friend2, friend3, friend4)
    line = record_expense(interaction.guild_id, interaction.user, people, cents, note)
    await interaction.response.send_message(line)


@tree.command(name="owe", description="Who owes who, with the fewest payments to square up")
async def owe(interaction: discord.Interaction):
    em, pays = owe_embed(interaction.guild_id)
    await interaction.response.send_message(embed=em, view=OweView(interaction.guild_id, pays))


@tree.command(name="settle", description="You paid someone back")
@app_commands.describe(amount="leave empty to pay back everything you owe them")
async def settle(interaction: discord.Interaction, who: discord.Member, amount: float | None = None):
    gid, me = interaction.guild_id, interaction.user
    pays = money.payments(money.balances(store.expenses(gid)))
    cents = int(round(amount * 100)) if amount else money.owed_between(pays, me.id, who.id)
    if not cents:
        await interaction.response.send_message(f"You don't owe {who.display_name} anything right now 👍",
                                                ephemeral=True)
        return
    store.add_expense(gid, me.id, me.display_name, cents, "settle up", [(who.id, who.display_name, cents)],
                      kind="settle")
    await interaction.response.send_message(f"🤝 {me.display_name} paid {who.display_name} back {money.fmt(cents)}")


@tree.command(name="undo", description="Undo the last turn or payment logged in this server")
async def undo(interaction: discord.Interaction):
    ev, ex = store.last(interaction.guild_id), store.last_expense(interaction.guild_id)
    if not ev and not ex:
        await interaction.response.send_message("Nothing to undo.", ephemeral=True)
        return
    if ex and (not ev or ex.ts >= ev.ts):
        store.remove_expense(ex.id)
        if ev and ex.message_id and ev.message_id == ex.message_id:
            store.remove(ev.id)
        await interaction.response.send_message(f"↩️ Removed: {ex.payer_name} {money.fmt(ex.cents)} ({ex.note})")
    else:
        store.remove(ev.id)
        await interaction.response.send_message(f"↩️ Removed: {ev.user_name} did **{ev.task}**")


@tree.command(name="demo-seed", description="Load made-up history for a demo (replaces this server's history)")
async def demo_seed(interaction: discord.Interaction, friend1: discord.Member, friend2: discord.Member,
                    friend3: discord.Member):
    gid = interaction.guild_id
    for t in store.tasks(gid):
        store.clear_task(gid, t)
    store.clear_money(gid)
    now, day = time.time(), 86400
    # drive: first ask is too close to call (wheel), second ask after logging is a clear answer
    for member, days_ago in [(friend1, [2, 6, 13]), (friend2, [9, 20]), (friend3, [12])]:
        for d in days_ago:
            store.add(gid, "drive", member.id, member.display_name, source="demo",
                      ts=now - d * day - random.randint(0, 3600))
    # pay: everyone has paid once, so money owed decides who's up
    for member, d in [(friend1, 3), (friend2, 5), (friend3, 7)]:
        store.add(gid, "pay", member.id, member.display_name, source="demo", ts=now - d * day)
    trio = [(m.id, m.display_name) for m in (friend1, friend2, friend3)]
    store.add_expense(gid, friend1.id, friend1.display_name, 6000, "dinner", money.split(6000, trio),
                      ts=now - 3 * day)
    store.add_expense(gid, friend2.id, friend2.display_name, 2400, "uber", money.split(2400, trio),
                      ts=now - 2 * day)
    await interaction.response.send_message("🎬 Demo history loaded: drive turns, a $60 dinner and a $24 uber.",
                                            ephemeral=True)


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
    if not token:
        raise SystemExit("Set DISCORD_TOKEN in a .env file next to bot.py (see .env.example).")
    client.run(token)
