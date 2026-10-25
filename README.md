# The Rotation 🔁

A Discord bot for a friend group's turns and shared expenses. It runs on Python 3.10+, stores history in SQLite, and works without an AI API key.

## Start it

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications). In **Bot**, create/copy its token and enable **Message Content Intent** and **Server Members Intent**. The bot needs that intent to read ordinary chat messages ([Discord documentation](https://docs.discord.com/developers/events/gateway#message-content-intent)).
2. In **OAuth2 → URL Generator**, select `bot` and `applications.commands`. Give it **View Channels, Send Messages, Embed Links, Attach Files, Add Reactions, Read Message History**, and **Send Messages in Threads** if you use threads. Open the generated link to invite it to your server.
3. In this folder:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```

   Put the bot token in `.env` as `DISCORD_TOKEN=...`, then run:

   ```bash
   python bot.py
   ```

   On Windows, activate with `.venv\Scripts\activate` instead.

The bot registers its slash commands in each server and prints `The Rotation is online`. Keep the process running for it to respond. The token stays in your local `.env`, which is ignored by Git.

## Turns

- Say **“I drove last night”**, **“I got the uber home”**, **“I cooked dinner”**, **“I did the dishes”**, or **“I did laundry”**. The bot records the matching task, reacts 📝, and replies **“📝 Logged: Ana did cooking”** with the next-turn status. The author can react ❌ on their original message to remove that log and its associated expense.
- Present-tense messages such as **“im cooking today”**, **“I'm driving tonight”**, and **“I'm doing the dishes”** also get 📝 and a confirmation. They count as taking the turn now and show as **Current turn**. Negated statements, questions, and explicit future plans such as “I'm cooking tomorrow” are skipped. Saying “I'm paying $48” records a pay turn, but an expense is only recorded once you say you paid.
- Cooking, dishes, laundry, trash, vacuuming, mopping, bathroom/kitchen cleaning, plants, dog care, and other built-in tasks are recognized without an AI key. A custom task taught with `/log` is recognized in later completion statements such as “I did the litter box”. `cook`, `cooked`, and `cooking` all use the same cooking history.
- Run **`/tally` without a task** for an overview showing the next-turn status and last completion for each task. **`/tally task:cooking`** adds that task's participant odds and reasons. A close rotation explicitly says **spin needed** and names the most overdue person; a clear rotation names who is up next.
- Ask **“Whose turn to drive?”**. Mention friends to include yourself and those friends. Without mentions, the bot uses the real people who can access this channel, including offline members; bots are excluded. Threads use their joined members. Everyone is displayed by their Discord display name. Those participants are remembered for task history, including friends who have not completed the task yet.
- A clear leader gets **🔁 Up in the rotation: Ben (drive)**, with everyone's odds and a reason. Otherwise, the bot sends a weighted wheel GIF and the result.
- **✅ Done, log it** records the chosen person's turn. Anyone included in that rotation can confirm. **🙅 Can't, spin without me** is available only to the picked person and excludes them from that spin and its subsequent passes. A clear result also has **Spin anyway**.
- “Last night”, “yesterday”, weekday names, “last week”, and “N days ago” backdate a log. Other logs use the message time. `ROTATION_TIMEZONE` controls the calendar; the example uses `America/Phoenix`.

Weights follow exactly:

```text
base weight = (days since last time + 1) / (times in the last 60 days + 1)
never done it: use 30 days
money task weight = base weight × (1 + net amount owed / $25)
```

Elapsed days are fractional and have no maximum. Money tasks are `pay`, `food`, `uber`, `groceries`, and `snacks`. A leader whose weight is at least twice the next person's gets a clear answer. Otherwise, the probability and wheel slice are proportional to weight. Displayed percentages total 100%.

## Money

**“I paid $48 for dinner @Ben @Cal”** logs a turn and an expense:

> 💸 Ana paid $48.00 for dinner · split 3 ways: Ben $16.00 · Cal $16.00

The payer is included in the split. Without mentions, participants are the people the bot observed talking in that channel during the last 12 hours. Recent speakers survive restarts. If no other participants are known, the bot asks you to use `/expense` with friends instead of inventing a split. Amounts use integer cents; leftover pennies go to the first participants, starting with the payer.

`/owe` nets all expenses and settlements **within this server** into the fewest direct payments from net debtors to net creditors. Each payment has a 🤝 button; only its sender or recipient can confirm it. Larger plans have page buttons. Confirmation records a settlement; it does not send real money. Old cards reject confirmation after balances change. Run `/owe` again for a fresh plan.

Say **“paid you back @Ana”**, **“venmo'd you $20 @Ana”**, or reply to Ana's message with the repayment. Without an explicit recipient, the bot can infer the recipient when your current plan has only one. Otherwise it asks who you paid. With no amount, it uses the suggested payment to that person. An explicit amount supports partial repayments to any net creditor, up to the smaller of your outstanding debt and their credit. Successful logs get 🤝 and can be undone by their author with ❌.

## Commands

| Command | Use |
| --- | --- |
| `/add-task name` | Register a new task type for this server, using this chat's humans as participants |
| `/remove-task name` | Creator or server manager can remove a task and its turn history after confirmation |
| `/spin task [choose_people]` | Spin using everyone in this chat; `choose_people:true` opens a picker with their names |
| `/tally [task]` | Show who is next for every task; optionally inspect one task with odds and history |
| `/log task [who]` | Record a turn; defaults to yourself |
| `/expense amount note [choose_people]` | Split with the humans in this chat; `choose_people:true` lets you select names |
| `/owe` | Show the minimum-payment plan and settlement buttons |
| `/settle who [amount]` | Record your full suggested repayment or a partial amount |
| `/undo` | Remove the most recently recorded turn/payment; logger or server manager only |

Add a custom type with `/add-task name:litter box`. It appears in task autocomplete and `/tally` immediately, with no completed turn invented. Use `/spin task:litter box`, `/log task:litter box`, or say “I did the litter box” / “im handling the litter box today”. Task types persist across restarts, are separate for each server, and duplicate names keep existing history intact.

Remove a type with `/remove-task name:litter box`, then confirm **Remove task**. Only its creator or a server manager can remove it; tasks without a registered creator require a server manager. Removal deletes its turn history and participants, hides it from autocomplete and the task overview, and disables automatic task logging until `/add-task` explicitly enables it again. Expense and repayment tracking stays available, with existing money records preserved. Old rotation buttons cannot recreate removed tasks.

There are no numbered friend fields. `/spin task:cooking` uses the group automatically. With `choose_people:true`, the private picker starts with everyone selected, lets you toggle actual names, and has page buttons for groups larger than 25. Only the command caller can use that picker. Expense splits always include the payer.

Commands work in servers. Participant pickers expire after five minutes. Other buttons expire after one hour and after a bot restart; history stays saved, and `/spin` or `/owe` creates fresh buttons. Repeated clicks and redelivered chat messages do not duplicate a log. Rules recognize the common phrases, skip obvious negations/quotes/questions, and support known custom task names. For unusual task wording, `/log` is always available.

## Optional configuration and hosting

- `ROTATION_DB`: SQLite path, default `rotation.db` beside `bot.py`. On a hosted machine, put it on persistent disk. Back up this file to retain history. Run one bot process against it.
- `ROTATION_TIMEZONE`: IANA timezone for past-day phrases, default UTC.
- `ANTHROPIC_API_KEY` **and** `ROTATION_MODEL`: optional API key and an available Anthropic model ID. Both must be set to enable the fallback. Relevant messages that built-in rules cannot parse are sent to Anthropic; leave both blank to keep parsing local. API errors fall back to the local rules.

Exact minimum-payment search can take longer for large groups because the general problem is exponential. This bot is designed for friend groups.

## Verify locally

```bash
.venv/bin/python -m unittest discover tests -v
```

Tests cover the overdue formula, weighted selection, wheel alignment and GIF size, payment minimization, rounding, partial settlements, phrase recognition, past dates, SQLite migration and persistence, server/channel isolation, command registration, reaction undo, button permissions, duplicate events, and stale cards. Discord callbacks are tested using mocks; a real server smoke test requires your bot token.

## Files

```text
bot.py                    Discord events, buttons, slash commands, startup
rotation/fairness.py      overdue math, odds, weighted selection
rotation/money.py         cent-safe amounts, splitting, minimum payments
rotation/wheel.py         weighted wheel GIF renderer
rotation/understand.py    phrase parsing and optional AI fallback
rotation/store.py         SQLite history, task participants, recent speakers, ledger versions
rotation/participants.py  name-based participant picker
tests/                   unit and Discord callback tests
```
