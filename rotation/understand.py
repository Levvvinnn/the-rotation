"""Turn a chat message into one of:
  did     the sender did a task, maybe with an amount ("I paid $48 for dinner")
  ask     someone asks whose turn it is ("whose turn to drive?")
  settle  the sender paid someone back ("paid you back @Dev", "venmo'd jake $20")
or nothing.

Built-in phrase matching always works. If ANTHROPIC_API_KEY is set, an AI model also reads messages that look
relevant, so any phrasing works ("bro I carried the whole Costco run, 63 bucks").
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import aiohttp

# canonical task -> words that mean it
TASKS = {
    "drive": ["drive", "drove", "driving", "driver", "drove us", "driven"],
    "uber": ["uber", "lyft", "ride"],
    "host": ["host", "hosted", "hosting", "my place", "at mine"],
    "pay": ["paid", "pay", "paying", "bill", "tab", "check", "covered"],
    "food": ["food", "dinner", "lunch", "pizza", "takeout", "doordash"],
    "snacks": ["snacks", "snack", "drinks"],
    "groceries": ["groceries", "grocery", "costco", "trader joe"],
    "dishes": ["dishes", "dish"],
    "trash": ["trash", "garbage"],
    "aux": ["aux", "music", "playlist"],
}
MONEY_TASKS = {"pay", "food", "uber", "groceries", "snacks"}

DID_VERBS = r"(drove|hosted|paid|got|bought|grabbed|covered|took out|did|picked up|ordered|took)"
_did_re = re.compile(rf"\bi\s+(?:just\s+|already\s+)?{DID_VERBS}\b(.*)", re.I)
_did_short = re.compile(r"\bi\s+(?:just\s+|already\s+)?(drove|hosted)\b", re.I)
_ask_re = re.compile(
    r"\b(?:whose|who'?s|who is|who\s+should)\s+(?:turn|driving|hosting|paying|getting|grabbing|buying|ordering|"
    r"taking|doing|drive|host|pay|get|grab|buy|order|take|do)\b(.*)",
    re.I,
)
_settle_re = re.compile(
    r"\b(?:paid|sent|venmo'?d|venmoed|zelle'?d|zelled|cash\s?app'?d)\b[^.!?]*\bback\b"
    r"|\b(?:venmo'?d|venmoed|zelle'?d|zelled|cash\s?app'?d)\b"
    r"|\bwe(?:'re| are)\s+(?:even|square|squared up)\b",
    re.I,
)
_amount_re = re.compile(r"\$\s?(\d{1,5}(?:\.\d{1,2})?)|\b(\d{1,5}(?:\.\d{1,2})?)\s?(?:dollars|bucks)\b", re.I)


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
    value = float(m.group(1) or m.group(2))
    return int(round(value * 100)) if value > 0 else None


def _note(text: str) -> str:
    m = re.search(r"\bfor\s+(?:the\s+)?([a-z][a-z' ]{1,24})", text, re.I)
    return m.group(1).strip() if m else ""


def _task_in(text: str, known: list[str]) -> str | None:
    t = text.lower()
    for task in known:  # tasks this server already uses win first
        if re.search(rf"\b{re.escape(task)}\b", t):
            return task
    for task, words in TASKS.items():
        for w in sorted(words, key=len, reverse=True):
            if re.search(rf"\b{re.escape(w)}\b", t):
                return task
    return None


def rules(text: str, known: list[str] | None = None) -> Intent | None:
    known = known or []
    if _settle_re.search(text):
        return Intent("settle", cents=amount_cents(text))
    m = _ask_re.search(text)
    if m:
        task = _task_in(m.group(0), known)
        if task:
            return Intent("ask", task)
    m = _did_re.search(text)
    if m:
        task = _task_in(m.group(0), known)
        if task:
            return Intent("did", task, amount_cents(text), _note(text))
    m = _did_short.search(text)
    if m:
        return Intent("did", "drive" if m.group(1).lower() == "drove" else "host", amount_cents(text))
    return None


def looks_relevant(text: str) -> bool:
    t = text.lower()
    if "turn" in t or "who" in t or _settle_re.search(t) or _amount_re.search(t):
        return True
    return bool(re.search(r"\bi\s+(?:just\s+|already\s+)?\w+", t)) and any(
        w in t for words in TASKS.values() for w in words
    )


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
    if not key:
        return None
    body = {
        "model": os.environ.get("ROTATION_MODEL", "claude-haiku-5-5"),
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
    cents = int(round(float(amount) * 100)) if isinstance(amount, (int, float)) and amount > 0 else None
    if kind == "settle":
        return Intent("settle", cents=cents)
    if kind in ("did", "ask") and out.get("task"):
        task = str(out["task"]).lower().strip()[:20]
        return Intent(kind, task, cents if kind == "did" else None, str(out.get("note") or "")[:40])
    return None


async def understand(text: str, known: list[str] | None = None) -> Intent | None:
    known = known or []
    if not looks_relevant(text):
        return None
    found = await ai(text, known)
    return found or rules(text, known)
