"""SQLite history per Discord server: who did which task, and who paid for whom."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Event:
    id: int
    guild_id: int
    task: str
    user_id: int
    user_name: str
    ts: float
    source: str
    message_id: int | None


@dataclass
class Expense:
    id: int
    guild_id: int
    payer_id: int
    payer_name: str
    cents: int
    note: str
    kind: str          # "expense" or "settle"
    ts: float
    message_id: int | None
    shares: list[tuple[int, str, int]]  # (user id, name, cents owed to the payer)


class Store:
    def __init__(self, path: str | Path = "rotation.db"):
        self.db = sqlite3.connect(str(path))
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, task TEXT NOT NULL,
                user_id INTEGER NOT NULL, user_name TEXT NOT NULL, ts REAL NOT NULL, source TEXT NOT NULL,
                message_id INTEGER);
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, payer_id INTEGER NOT NULL,
                payer_name TEXT NOT NULL, cents INTEGER NOT NULL, note TEXT NOT NULL, kind TEXT NOT NULL,
                ts REAL NOT NULL, message_id INTEGER);
            CREATE TABLE IF NOT EXISTS shares (
                expense_id INTEGER NOT NULL, user_id INTEGER NOT NULL, user_name TEXT NOT NULL,
                cents INTEGER NOT NULL);
            """
        )
        self.db.commit()

    # ----- turns -----

    def add(self, guild_id: int, task: str, user_id: int, user_name: str,
            source: str = "chat", message_id: int | None = None, ts: float | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO events (guild_id, task, user_id, user_name, ts, source, message_id) VALUES (?,?,?,?,?,?,?)",
            (guild_id, task.lower(), user_id, user_name, time.time() if ts is None else ts, source, message_id),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def remove(self, event_id: int) -> None:
        self.db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        self.db.commit()

    def by_message(self, message_id: int) -> Event | None:
        row = self.db.execute("SELECT * FROM events WHERE message_id = ?", (message_id,)).fetchone()
        return Event(*row) if row else None

    def last(self, guild_id: int) -> Event | None:
        row = self.db.execute("SELECT * FROM events WHERE guild_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
                              (guild_id,)).fetchone()
        return Event(*row) if row else None

    def events(self, guild_id: int, task: str) -> list[Event]:
        rows = self.db.execute("SELECT * FROM events WHERE guild_id = ? AND task = ? ORDER BY ts",
                               (guild_id, task.lower())).fetchall()
        return [Event(*r) for r in rows]

    def tasks(self, guild_id: int) -> list[str]:
        rows = self.db.execute("SELECT DISTINCT task FROM events WHERE guild_id = ? ORDER BY task",
                               (guild_id,)).fetchall()
        return [r[0] for r in rows]

    def clear_task(self, guild_id: int, task: str) -> None:
        self.db.execute("DELETE FROM events WHERE guild_id = ? AND task = ?", (guild_id, task.lower()))
        self.db.commit()

    # ----- money -----

    def add_expense(self, guild_id: int, payer_id: int, payer_name: str, cents: int, note: str,
                    shares: list[tuple[int, str, int]], kind: str = "expense",
                    message_id: int | None = None, ts: float | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO expenses (guild_id, payer_id, payer_name, cents, note, kind, ts, message_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (guild_id, payer_id, payer_name, cents, note, kind, time.time() if ts is None else ts, message_id),
        )
        eid = int(cur.lastrowid)
        self.db.executemany("INSERT INTO shares (expense_id, user_id, user_name, cents) VALUES (?,?,?,?)",
                            [(eid, uid, name, c) for uid, name, c in shares])
        self.db.commit()
        return eid

    def _expense(self, row) -> Expense:
        shares = self.db.execute("SELECT user_id, user_name, cents FROM shares WHERE expense_id = ?",
                                 (row[0],)).fetchall()
        return Expense(*row, shares=[tuple(s) for s in shares])

    def expenses(self, guild_id: int) -> list[Expense]:
        rows = self.db.execute("SELECT * FROM expenses WHERE guild_id = ? ORDER BY ts", (guild_id,)).fetchall()
        return [self._expense(r) for r in rows]

    def expense_by_message(self, message_id: int) -> Expense | None:
        row = self.db.execute("SELECT * FROM expenses WHERE message_id = ?", (message_id,)).fetchone()
        return self._expense(row) if row else None

    def last_expense(self, guild_id: int) -> Expense | None:
        row = self.db.execute("SELECT * FROM expenses WHERE guild_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
                              (guild_id,)).fetchone()
        return self._expense(row) if row else None

    def remove_expense(self, expense_id: int) -> None:
        self.db.execute("DELETE FROM shares WHERE expense_id = ?", (expense_id,))
        self.db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        self.db.commit()

    def clear_money(self, guild_id: int) -> None:
        ids = [r[0] for r in self.db.execute("SELECT id FROM expenses WHERE guild_id = ?", (guild_id,))]
        for eid in ids:
            self.remove_expense(eid)
