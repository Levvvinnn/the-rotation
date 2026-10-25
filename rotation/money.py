"""Integer-cent splitting, net balances, and minimum-payment settlements."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache

MAX_CENTS = 100_000_000


def to_cents(amount) -> int:
    """Convert a dollar amount once, without binary-float rounding errors."""
    try:
        value = Decimal(str(amount))
        if not value.is_finite() or value <= 0 or value > Decimal(MAX_CENTS) / 100:
            raise ValueError("Amount must be between $0.01 and $1,000,000.00.")
        cents = int((value * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        if cents <= 0:
            raise ValueError("Amount must be at least $0.01.")
        return cents
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Enter a valid dollar amount.") from exc


def split(cents: int, people: list[tuple[int, str]]) -> list[tuple[int, str, int]]:
    if not people or len({uid for uid, _ in people}) != len(people):
        raise ValueError("Choose at least one person, with no duplicates.")
    if not isinstance(cents, int) or cents <= 0:
        raise ValueError("Amount must be positive integer cents.")
    base, extra = divmod(cents, len(people))
    return [(uid, name, base + (i < extra)) for i, (uid, name) in enumerate(people)]


def balances(expenses) -> dict[int, tuple[str, int]]:
    net, names = {}, {}
    for e in expenses:
        names[e.payer_id] = e.payer_name
        net[e.payer_id] = net.get(e.payer_id, 0) + e.cents
        for uid, name, c in e.shares:
            names[uid] = name
            net[uid] = net.get(uid, 0) - c
    return {uid: (names[uid], v) for uid, v in net.items()}


def payments(bal: dict[int, tuple[str, int]]) -> list[tuple[int, str, int, str, int]]:
    """Exact fewest transfers, directly from net debtors to net creditors.

    Search all ways to exhaust a debtor or creditor. Equal balances can be paired
    immediately. Memoization and the remaining-person lower bound prune the search.
    This is intended for friend groups; exact minimization is exponential in general.
    """
    if sum(v for _, v in bal.values()) != 0:
        raise ValueError("Balances must sum to zero.")
    debtors = sorted([(uid, -v) for uid, (_, v) in bal.items() if v < 0], key=lambda p: (-p[1], p[0]))
    creditors = sorted([(uid, v) for uid, (_, v) in bal.items() if v > 0], key=lambda p: (-p[1], p[0]))

    @lru_cache(maxsize=None)
    def solve(debts, credits):
        if not any(debts):
            return ()
        pairs = [(i, j) for i, d in enumerate(debts) if d for j, c in enumerate(credits) if c]
        exact = next(((i, j) for i, j in pairs if debts[i] == credits[j]), None)
        if exact is not None:
            pairs = [exact]
        best = None
        lower_bound = max(sum(d > 0 for d in debts), sum(c > 0 for c in credits))
        for i, j in pairs:
            amount = min(debts[i], credits[j])
            ds, cs = list(debts), list(credits)
            ds[i] -= amount
            cs[j] -= amount
            candidate = ((i, j, amount),) + solve(tuple(ds), tuple(cs))
            if best is None or len(candidate) < len(best):
                best = candidate
            if len(best) == lower_bound:
                break
        return best

    result = solve(tuple(c for _, c in debtors), tuple(c for _, c in creditors))
    return [(debtors[i][0], bal[debtors[i][0]][0], creditors[j][0], bal[creditors[j][0]][0], c)
            for i, j, c in result]


def owed_between(bal_payments, debtor_id: int, creditor_id: int) -> int:
    return sum(c for f, _, t, _, c in bal_payments if f == debtor_id and t == creditor_id)


def settlement_amount(bal, debtor_id: int, creditor_id: int, requested: int | None = None) -> int:
    if debtor_id == creditor_id:
        raise ValueError("Choose someone other than yourself.")
    # An explicit repayment can go to any net creditor, even if the minimum-payment
    # plan routes this debtor elsewhere. It must not reverse either person's balance.
    debt = max(0, -bal.get(debtor_id, ('', 0))[1])
    credit = max(0, bal.get(creditor_id, ('', 0))[1])
    limit = min(debt, credit)
    if requested is None:
        requested = owed_between(payments(bal), debtor_id, creditor_id)
    if requested <= 0 or requested > limit:
        raise ValueError("No matching debt, or the amount exceeds the outstanding balance. Use /owe to check.")
    return requested


def owes(bal: dict[int, tuple[str, int]]) -> dict[int, int]:
    return {uid: max(0, -v) for uid, (_, v) in bal.items()}


def fmt(cents: int) -> str:
    return f"${cents / 100:,.2f}"
