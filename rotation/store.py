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
    actor_id: int | None
    created_at: float


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
    actor_id: int | None
    created_at: float
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
        # Additive migration for databases created by the original prototype.
        for table in ("events", "expenses"):
            columns = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if "actor_id" not in columns:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN actor_id INTEGER")
            if "created_at" not in columns:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
                self.db.execute(f"UPDATE {table} SET created_at = ts")
        self.db.executescript("""
            CREATE INDEX IF NOT EXISTS events_task ON events(guild_id, task);
            CREATE INDEX IF NOT EXISTS events_message ON events(message_id);
            CREATE INDEX IF NOT EXISTS expenses_guild ON expenses(guild_id);
            CREATE INDEX IF NOT EXISTS shares_expense ON shares(expense_id);
            CREATE TABLE IF NOT EXISTS task_people (
                guild_id INTEGER NOT NULL, task TEXT NOT NULL, user_id INTEGER NOT NULL, user_name TEXT NOT NULL,
                PRIMARY KEY(guild_id, task, user_id));
            CREATE TABLE IF NOT EXISTS speakers (
                channel_id INTEGER, user_id INTEGER, user_name TEXT, ts REAL,
                PRIMARY KEY(channel_id, user_id));
            CREATE TABLE IF NOT EXISTS processed_messages (message_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS ledger_versions (guild_id INTEGER PRIMARY KEY, version INTEGER NOT NULL);
            CREATE TRIGGER IF NOT EXISTS expense_added AFTER INSERT ON expenses BEGIN
                INSERT INTO ledger_versions VALUES (NEW.guild_id, 1)
                ON CONFLICT(guild_id) DO UPDATE SET version = version + 1;
            END;
            CREATE TRIGGER IF NOT EXISTS expense_removed AFTER DELETE ON expenses BEGIN
                INSERT INTO ledger_versions VALUES (OLD.guild_id, 1)
                ON CONFLICT(guild_id) DO UPDATE SET version = version + 1;
            END;
        """)
        self.db.commit()

    def version(self, guild_id: int) -> int:
        row = self.db.execute("SELECT version FROM ledger_versions WHERE guild_id = ?", (guild_id,)).fetchone()
        return row[0] if row else 0

    def claim_message(self, message_id: int) -> bool:
        with self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO processed_messages VALUES (?)", (message_id,))
        return bool(cur.rowcount)

    def remember(self, channel_id: int, user_id: int, name: str, now: float) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO speakers VALUES (?,?,?,?)", (channel_id, user_id, name, now))
            self.db.execute("DELETE FROM speakers WHERE ts < ?", (now - 12 * 3600,))

    def recent_people(self, channel_id: int, since: float) -> dict[int, str]:
        return dict(self.db.execute("SELECT user_id, user_name FROM speakers WHERE channel_id = ? AND ts >= ?",
                                    (channel_id, since)))

    def task_people(self, guild_id: int, task: str) -> dict[int, str]:
        return dict(self.db.execute("SELECT user_id, user_name FROM task_people WHERE guild_id = ? AND task = ?",
                                    (guild_id, task)))

    def add_task_people(self, guild_id: int, task: str, people: dict[int, str]) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO task_people VALUES (?,?,?,?)",
                                [(guild_id, task, uid, name) for uid, name in people.items()])

    # ----- turns -----

    def add(self, guild_id: int, task: str, user_id: int, user_name: str,
            source: str = "chat", message_id: int | None = None, ts: float | None = None, actor_id: int | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO events (guild_id, task, user_id, user_name, ts, source, message_id, actor_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (guild_id, task.lower(), user_id, user_name, time.time() if ts is None else ts, source, message_id, actor_id or user_id, time.time()),
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
        row = self.db.execute("SELECT * FROM events WHERE guild_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                              (guild_id,)).fetchone()
        return Event(*row) if row else None

    def events(self, guild_id: int, task: str) -> list[Event]:
        rows = self.db.execute("SELECT * FROM events WHERE guild_id = ? AND task = ? ORDER BY ts",
                               (guild_id, task.lower())).fetchall()
        return [Event(*r) for r in rows]

    def tasks(self, guild_id: int) -> list[str]:
        rows = self.db.execute("SELECT task FROM events WHERE guild_id = ? UNION SELECT task FROM task_people WHERE guild_id = ? ORDER BY task",
                               (guild_id, guild_id)).fetchall()
        return [r[0] for r in rows]

    def clear_task(self, guild_id: int, task: str) -> None:
        self.db.execute("DELETE FROM events WHERE guild_id = ? AND task = ?", (guild_id, task.lower()))
        self.db.execute("DELETE FROM task_people WHERE guild_id = ? AND task = ?", (guild_id, task.lower()))
        self.db.commit()

    # ----- money -----

    def add_expense(self, guild_id: int, payer_id: int, payer_name: str, cents: int, note: str,
                    shares: list[tuple[int, str, int]], kind: str = "expense",
                    message_id: int | None = None, ts: float | None = None, actor_id: int | None = None) -> int:
        if cents <= 0 or sum(c for _, _, c in shares) != cents or any(c < 0 for _, _, c in shares):
            raise ValueError("Expense shares must be nonnegative and add up to the amount.")
        with self.db:
            cur = self.db.execute(
                "INSERT INTO expenses (guild_id, payer_id, payer_name, cents, note, kind, ts, message_id, actor_id, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (guild_id, payer_id, payer_name, cents, note, kind, time.time() if ts is None else ts, message_id, actor_id or payer_id, time.time()),
            )
            eid = int(cur.lastrowid)
            self.db.executemany("INSERT INTO shares (expense_id, user_id, user_name, cents) VALUES (?,?,?,?)",
                                [(eid, uid, name, c) for uid, name, c in shares])

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
        row = self.db.execute("SELECT * FROM expenses WHERE guild_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
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
