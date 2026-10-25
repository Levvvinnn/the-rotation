"""Who's most overdue for a task, and how sure we are about it.

Each person gets a weight: the longer since they last did it and the fewer times they did it recently,
the bigger their weight. Weights become slice sizes on the wheel and chances when it spins.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

DAY = 86400.0
WINDOW_DAYS = 60       # only count recent history
NEVER_DAYS = 30        # someone who has never done it counts as 30 days overdue
CLEAR_RATIO = 2.0      # top weight at least this many times the runner-up = no spin needed


@dataclass
class Standing:
    user_id: int
    name: str
    count: int            # times in the window
    days_since: float | None  # None = never
    weight: float
    owes_cents: int = 0


OWE_STEP_CENTS = 2500  # every $25 someone owes the group adds 1x to their weight on money tasks


def standings(people: dict[int, str], events, now: float | None = None,
              owes_cents: dict[int, int] | None = None) -> list[Standing]:
    """people: {user_id: display name}. events: Store events for one task. owes_cents: for money tasks,
    what each person owes the group. Sorted most overdue first."""
    owes_cents = owes_cents or {}
    now = time.time() if now is None else now
    out = []
    for uid, name in people.items():
        mine = [e for e in events if e.user_id == uid]
        recent = [e for e in mine if now - e.ts <= WINDOW_DAYS * DAY]
        last = max((e.ts for e in mine), default=None)
        days = None if last is None else max(0.0, (now - last) / DAY)
        overdue = NEVER_DAYS if days is None else min(days, NEVER_DAYS)
        weight = (overdue + 1.0) / (len(recent) + 1.0)
        owed = owes_cents.get(uid, 0)
        weight *= 1.0 + owed / OWE_STEP_CENTS
        out.append(Standing(uid, name, len(recent), days, weight, owed))
    out.sort(key=lambda s: s.weight, reverse=True)
    return out


def is_clear(ranked: list[Standing]) -> bool:
    if len(ranked) < 2:
        return True
    return ranked[0].weight >= CLEAR_RATIO * ranked[1].weight


def pick(ranked: list[Standing], rng: random.Random | None = None) -> int:
    """Weighted random index into ranked."""
    rng = rng or random.Random()
    total = sum(s.weight for s in ranked)
    x = rng.uniform(0, total)
    for i, s in enumerate(ranked):
        x -= s.weight
        if x <= 0:
            return i
    return len(ranked) - 1


def describe(s: Standing) -> str:
    owe = f", owes ${s.owes_cents / 100:,.2f}" if s.owes_cents else ""
    return _describe_turns(s) + owe


def _describe_turns(s: Standing) -> str:
    if s.days_since is None:
        return "never done it"
    if s.days_since < 1:
        when = "today"
    elif s.days_since < 2:
        when = "yesterday"
    else:
        when = f"{int(s.days_since)} days ago"
    times = "once" if s.count == 1 else f"{s.count} times"
    return f"last {when}, {times} recently"


def chances(ranked: list[Standing]) -> list[int]:
    total = sum(s.weight for s in ranked) or 1.0
    return [round(100 * s.weight / total) for s in ranked]
