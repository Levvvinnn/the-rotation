"""Turn a chat message into one of:
  did     the sender did a task, maybe with an amount ("I paid $48 for dinner")
  ask     someone asks whose turn it is ("whose turn to drive?")
  settle  the sender paid someone back ("paid you back @Dev", "venmo'd jake $20")
or nothing.

Built-in phrase matching always works. If ANTHROPIC_API_KEY and ROTATION_MODEL are set, an AI model also reads messages that look
relevant, so any phrasing works ("bro I carried the whole Costco run, 63 bucks").
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rotation.money import to_cents

import aiohttp

# Specific chores precede broad words such as "food" and "cleaning".
TASKS = {
    "drive": ["drive", "drove", "driving", "driver", "driven"],
    "uber": ["uber", "lyft", "ride", "rideshare", "ubered", "lyfted"],
    "host": ["host", "hosted", "hosting", "my place", "at mine"],
    "cooking": ["cook", "cooked", "cooking", "made dinner", "made lunch", "made breakfast",
                "made food", "prepared dinner", "prepared lunch", "prepared breakfast", "prepared food",
                "meal prep", "meal prepped"],
    "dishes": ["dishes", "dish", "dishwasher", "washing up", "washed up"],
    "laundry": ["laundry", "washing clothes", "washed clothes", "folded clothes", "folding clothes"],
    "trash": ["trash", "garbage", "bins", "recycling"],
    "vacuuming": ["vacuum", "vacuumed", "vacuuming", "hoovered"],
    "mopping": ["mop", "mopped", "mopping", "sweep", "swept", "sweeping"],
    "bathroom": ["bathroom", "toilet"],
    "kitchen": ["kitchen"],
    "plants": ["plants", "watered the plants", "watering plants"],
    "dog": ["walked the dog", "walk the dog", "walking the dog", "fed the dog", "feed the dog", "dog"],
    "cleaning": ["clean", "cleaned", "cleaning", "tidy", "tidied", "tidying"],
    "chores": ["chore", "chores"],
    "pay": ["paid", "pay", "paying", "bill", "tab", "check", "covered"],
    "food": ["food", "dinner", "lunch", "pizza", "takeout", "doordash"],
    "snacks": ["snacks", "snack", "drinks"],
    "groceries": ["groceries", "grocery", "costco", "trader joe"],
    "aux": ["aux", "music", "playlist"],
}
MONEY_TASKS = {"pay", "food", "uber", "groceries", "snacks"}

DID_VERBS = (r"(drove|hosted|paid|got|bought|grabbed|covered|took out|did|picked up|ordered|took|"
             r"ubered|lyfted|cooked|made|prepared|washed|cleaned|vacuumed|mopped|swept|watered|walked|fed|"
             r"finished|handled|emptied|folded|ran|booked|called|tidied|hoovered|meal prepped)")
_did_re = re.compile(rf"\bi(?:'ve|\s+have)?\s+(?:(?:just|already|finally|also)\s+)*{DID_VERBS}\b(.*)", re.I)
_ask_re = re.compile(
    r"\b(?:whose|who'?s|who is|who\s+should)\s+(?:turn|driving|hosting|paying|getting|grabbing|buying|ordering|"
    r"next|up(?:\s+next)?|taking|doing|for|drive|host|pay|get|grab|buy|order|take|do|cooking|cook|cleaning|clean|washing|"
    r"vacuuming|mopping|sweeping|walking|feeding|watering|folding)\b(.*)", re.I,
)
_settle_re = re.compile(
    r"\b(?:paid|sent|venmo'?d|venmoed|zelle'?d|zelled|cash\s?app'?d)\b[^.!?]*\bback\b"
    r"|\b(?:venmo'?d|venmoed|zelle'?d|zelled|cash\s?app'?d)\b"
    r"|\bwe(?:'re| are)\s+(?:even|square|squared up)\b",
    re.I,
)
_amount_re = re.compile(
    r"(?<![\w$.-])\$\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?![\d.])"
    r"|(?<![\w.-])((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)\s*(?:dollars|bucks)\b", re.I)


@dataclass
class Intent:
    kind: str                  # "did", "ask" or "settle"
    task: str = ""
    cents: int | None = None   # amount mentioned, if any
    note: str = ""             # what it was for, if known


def amount_cents(text: str) -> int | None:
    m = _amount_re.search(text)
    if not m:
        return None
    try:
        return to_cents((m.group(1) or m.group(2)).replace(",", ""))
    except ValueError:
        return None


def occurred_at(text: str, now: float, zone: str = "UTC") -> float:
    """Resolve explicit past dates; unspecified times retain the message time."""
    local = datetime.fromtimestamp(now, ZoneInfo(zone))
    text = text.lower()
    days = 0
    if re.search(r"\b(yesterday|last night)\b", text):
        days = 1
    elif match := re.search(r"\b(\d{1,3}) days? ago\b", text):
        days = int(match.group(1))
    elif re.search(r"\blast week\b", text):
        days = 7
    else:
        weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        for i, day in enumerate(weekdays):
            if re.search(rf"\b{day}\b", text):
                days = (local.weekday() - i) % 7
                if days == 0 and f"last {day}" in text:
                    days = 7
                break
    return (local - timedelta(days=days)).timestamp()


def _note(text: str) -> str:
    m = re.search(r"\bfor\s+(?:the\s+)?([a-z][a-z' ]{1,24})", text, re.I)
    return m.group(1).strip() if m else ""


def canonical_task(name: str) -> str:
    name = " ".join(name.lower().split())
    for task, aliases in TASKS.items():
        if name == task or name in aliases:
            return task
    return name


def _task_in(text: str, known: list[str]) -> str | None:
    t = text.lower()
    for task in known:  # custom task names win; built-in aliases stay stable as history grows
        if task in TASKS:
            continue
        if re.search(rf"\b{re.escape(task)}\b", t):
            return task
    for task, words in TASKS.items():
        for w in sorted(words, key=len, reverse=True):
            if re.search(rf"\b{re.escape(w)}\b", t):
                return task
    return None


def rules(text: str, known: list[str] | None = None) -> Intent | None:
    known = known or []
    text = text.replace("’", "'")
    if re.search(r"[?]", text) and not _ask_re.search(text):
        return None
    if re.search(r"\b(?:if|wish|said|says|would|could|didn't|did not|haven't|have not)\b", text, re.I):
        return None
    if text.lstrip().startswith(('>', '"', "'", '`')):
        return None
    if _settle_re.search(text):
        if "$" in text and amount_cents(text) is None:
            return None
        return Intent("settle", cents=amount_cents(text))
    m = _ask_re.search(text)
    if m:
        task = _task_in(m.group(0), known)
        if task:
            return Intent("ask", task)
    m = _did_re.search(text)
    if m:
        verb = m.group(1).lower()
        task = (canonical_task(verb) if verb in {"drove", "hosted", "cooked", "vacuumed", "mopped", "swept",
                                                "ubered", "lyfted"} else _task_in(m.group(0), known))
        if task:
            # Refuse an invalid explicit amount rather than silently logging a turn.
            if "$" in text and amount_cents(text) is None:
                return None
            return Intent("did", task, amount_cents(text), _note(text))

    return None


def looks_relevant(text: str, known: list[str] | None = None) -> bool:
    text = text.replace("’", "'")
    return bool(_ask_re.search(text) or _settle_re.search(text) or _amount_re.search(text)
                or _did_re.search(text))


_PROMPT = """You read one message from a friend group's chat. Decide which of these it is:
- the sender says THEY already did a shared chore/turn (driving, paying, hosting, getting food, groceries,
  dishes, etc.), maybe with an amount of money
- someone asks whose turn it is / who should do something
- the sender says they paid someone back / sent them money / they're even
- none of these (plans, jokes, questions about other things)

Known task names in this group: {known}
Reuse a known name when it means the same thing. Otherwise use one short lowercase word.

Reply with JSON only, one of:
{{"kind": "did", "task": "<task>", "amount": <dollars or null>, "note": "<what it was for, few words or empty>"}}
{{"kind": "ask", "task": "<task>"}}
{{"kind": "settle", "amount": <dollars or null>}}
{{"kind": "none"}}

Message: {message}"""


async def ai(text: str, known: list[str]) -> Intent | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    model = os.environ.get("ROTATION_MODEL")
    if not key or not model:
        return None
    body = {
        "model": model,
        "max_tokens": 80,
        "messages": [{"role": "user", "content": _PROMPT.format(known=", ".join(known) or "none yet",
                                                                 message=text[:500])}],
    }
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.post("https://api.anthropic.com/v1/messages", json=body, headers=headers) as r:
                if r.status != 200:
                    return None
                data = await r.json()
        raw = data["content"][0]["text"]
        out = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception:
        return None
    kind = out.get("kind")
    amount = out.get("amount")
    try:
        cents = to_cents(amount) if isinstance(amount, (int, float)) and not isinstance(amount, bool) else None
    except ValueError:
        return None
    if kind == "settle":
        return Intent("settle", cents=cents)
    if kind in ("did", "ask") and out.get("task"):
        task = str(out["task"]).lower().strip()[:20]
        return Intent(kind, task, cents if kind == "did" else None, str(out.get("note") or "")[:40])
    return None


async def understand(text: str, known: list[str] | None = None) -> Intent | None:
    known = known or []
    if not looks_relevant(text, known):
        return None
    found = rules(text, known)
    return found or await ai(text, known)
