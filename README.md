# The Rotation 🔁

A Discord bot that keeps a friend group's turns and money fair.

Who's driving, getting the Uber, hosting, paying? Who still owes who for dinner? The Rotation lives in your group
chat, picks up "I drove last night" and "I paid $48 for dinner" on its own, tells you who's up next and why, and
when it's too close to call, spins a wheel where each friend's slice is as big as how overdue they are.

Track 3 (Group Chat Assistant) entry for the Cubbie Hackathon.

## What it does

**Turns**
- **Logs from the chat.** "I drove last night", "I got the uber home", "I hosted friday" → the bot reacts 📝.
  Wrong? The person who said it reacts ❌ to undo.
- **"Whose turn to drive?"** Mention friends to choose who's in, or leave it and it uses everyone who's done it
  before. If someone's clearly overdue: "🔁 Up in the rotation: Ben (drive)" with everyone's odds and why.
- **Too close? It spins.** A wheel GIF where slice size = how overdue each person is. Then: ✅ Done, log it ·
  🙅 Can't, spin without me (only the picked person can pass).

**Money**
- **Logs from the chat.** "I paid $48 for dinner @Ben @Cal" → "💸 Ana paid $48.00 for dinner · split 3 ways: Ben
  $16.00 · Cal $16.00". No mentions? It splits with whoever's been talking in the channel recently.
- **`/owe`** nets everything into the fewest payments ("Cal → Ana $28.00"), with a 🤝 button to mark each paid.
- **Settle up from the chat.** "paid you back @Ana" or "venmo'd you $20" (or reply to their message) → 🤝.
- **Turns and money talk to each other.** For money turns (pay, food, uber, groceries, snacks), whoever owes the
  group more gets a bigger slice. Owing money makes you more likely to be up next to pay.

**Commands:** `/spin`, `/tally`, `/log`, `/expense`, `/owe`, `/settle`, `/undo`, `/demo-seed`.

How "overdue" works: weight = (days since you last did it + 1) ÷ (times in the last 60 days + 1); never done it
counts as 30 days. On money turns, every $25 you owe adds 1× to your weight. If the top person's weight is at least
double the next one's, it's a clear answer; otherwise the wheel spins with those weights.

## Set up (about 10 minutes)

1. **Make the bot.** https://discord.com/developers/applications → New Application → "Bot" tab → Reset Token and
   copy it. On the same page, turn on **Message Content Intent**.
2. **Invite it.** "OAuth2" → "URL Generator" → scopes `bot` and `applications.commands` → permissions: Send
   Messages, Embed Links, Attach Files, Add Reactions, Read Message History → open the URL and pick your server.
3. **Run it** (Python 3.10+):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env               # then paste your token into .env
   python bot.py
   ```
   When it prints "The Rotation is online", the slash commands are ready in your server.
4. **Optional:** add an Anthropic API key to `.env` as `ANTHROPIC_API_KEY` so it understands any phrasing
   ("bro I carried the whole Costco run, 63 bucks"). Without it, built-in phrase matching handles the common ones.

Keep `.env` private. It's in `.gitignore` so it never gets pushed.

## Demo in 90 seconds

Three people in a test server: Ana, Ben, Cal.

1. `/demo-seed friend1:@Ana friend2:@Ben friend3:@Cal` (made-up history: Ana drives a lot, Cal barely; Ana
   covered a $60 dinner, Ben a $24 uber).
2. Ana: **"whose turn to drive to the game?"** → too close to call → the wheel spins (Cal has the biggest slice).
3. The picked person taps **✅ Done, log it**. Ana asks again → a clear answer this time, with everyone's odds.
4. Ben: **"whose turn to pay?"** → Cal, clearly: "owes $28.00". Money and turns working together.
5. `/owe` → "Cal → Ana $28.00 · Ben → Ana $4.00". Cal taps 🤝 → updated card.
6. Ben: **"I paid $36 for pizza @Ana @Cal"** → 💸 split card. `/owe` again.

## Tests

```bash
python -m unittest discover tests
```

They cover the fairness math (including the demo flow), splitting and settling money, money-aware turns, that the
wheel always stops on the picked person, the GIF size, and the phrase matching.

## Files

```text
bot.py                    Discord bot: reads chat, buttons, slash commands
rotation/fairness.py      who's overdue, odds, weighted pick
rotation/money.py         splits, balances, fewest payments
rotation/wheel.py         draws the spinning wheel GIF
rotation/understand.py    "I drove" / "I paid $48" / "whose turn?" / "paid you back" from chat
rotation/store.py         SQLite history per server
tests/                    unit tests
```
