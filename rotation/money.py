"""Splitting, balances and the fewest payments to square everyone up. All amounts in cents."""

from __future__ import annotations


def split(cents: int, people: list[tuple[int, str]]) -> list[tuple[int, str, int]]:
    """Even split; leftover cents go to the first people so the total always matches."""
    n = len(people)
    base, extra = divmod(cents, n)
    return [(uid, name, base + (1 if i < extra else 0)) for i, (uid, name) in enumerate(people)]


def balances(expenses) -> dict[int, tuple[str, int]]:
    """user id -> (name, net cents). Positive = the group owes them. Negative = they owe."""
    net: dict[int, int] = {}
    names: dict[int, str] = {}
    for e in expenses:
        names[e.payer_id] = e.payer_name
        net[e.payer_id] = net.get(e.payer_id, 0) + e.cents
        for uid, name, c in e.shares:
            names[uid] = name
            net[uid] = net.get(uid, 0) - c
    return {uid: (names[uid], v) for uid, v in net.items()}


def payments(bal: dict[int, tuple[str, int]]) -> list[tuple[int, str, int, str, int]]:
    """Fewest payments to settle: list of (from id, from name, to id, to name, cents)."""
    debtors = sorted([[uid, n, -v] for uid, (n, v) in bal.items() if v < 0], key=lambda x: -x[2])
    creditors = sorted([[uid, n, v] for uid, (n, v) in bal.items() if v > 0], key=lambda x: -x[2])
    out = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        pay = min(debtors[i][2], creditors[j][2])
        if pay > 0:
            out.append((debtors[i][0], debtors[i][1], creditors[j][0], creditors[j][1], pay))
        debtors[i][2] -= pay
        creditors[j][2] -= pay
        if debtors[i][2] == 0:
            i += 1
        if creditors[j][2] == 0:
            j += 1
    return out


def owed_between(bal_payments, debtor_id: int, creditor_id: int) -> int:
    return sum(c for f, _, t, _, c in bal_payments if f == debtor_id and t == creditor_id)


def owes(bal: dict[int, tuple[str, int]]) -> dict[int, int]:
    """user id -> cents they owe overall (0 if they're even or owed money)."""
    return {uid: max(0, -v) for uid, (_, v) in bal.items()}


def fmt(cents: int) -> str:
    return f"${cents / 100:,.2f}"
