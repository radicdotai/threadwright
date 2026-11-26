import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (thread TEXT PRIMARY KEY, session_id TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    thread TEXT NOT NULL, asker TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL NOT NULL,
    cost_usd REAL NOT NULL, is_error INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS done (event_id TEXT PRIMARY KEY, finished_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pending (status_ts TEXT PRIMARY KEY, channel TEXT NOT NULL, thread_ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY, channel TEXT NOT NULL, thread_ts TEXT NOT NULL, card_ts TEXT NOT NULL,
    actions TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
    decided_by TEXT, decided_at REAL, result TEXT
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def _one(self, sql: str, args: tuple[object, ...]) -> tuple[object, ...] | None:
        with self._lock:
            row: tuple[object, ...] | None = self._db.execute(sql, args).fetchone()
        return row

    def _write(self, sql: str, args: tuple[object, ...]) -> None:
        with self._lock, self._db:
            self._db.execute(sql, args)

    def session(self, thread: str) -> str | None:
        row = self._one("SELECT session_id FROM sessions WHERE thread = ?", (thread,))
        return str(row[0]) if row else None

    def save_session(self, thread: str, session_id: str) -> None:
        self._write(
            "INSERT INTO sessions VALUES (?, ?, ?) ON CONFLICT(thread) "
            "DO UPDATE SET session_id = excluded.session_id, updated_at = excluded.updated_at",
            (thread, session_id, time.time()),
        )

    def log_run(self, thread: str, asker: str, started_at: float, cost_usd: float, is_error: bool) -> None:
        self._write("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                    (thread, asker, started_at, time.time(), cost_usd, int(is_error)))

    def spent_since(self, since: float) -> float:
        row = self._one("SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE started_at >= ?", (since,))
        return float(str(row[0])) if row else 0.0

    def is_done(self, event_id: str) -> bool:
        return self._one("SELECT 1 FROM done WHERE event_id = ?", (event_id,)) is not None

    def mark_done(self, event_id: str) -> None:
        self._write("INSERT OR IGNORE INTO done VALUES (?, ?)", (event_id, time.time()))

    def add_pending(self, status_ts: str, channel: str, thread_ts: str) -> None:
        self._write("INSERT OR REPLACE INTO pending VALUES (?, ?, ?)", (status_ts, channel, thread_ts))

    def pending(self, status_ts: str) -> tuple[str, str] | None:
        row = self._one("SELECT channel, thread_ts FROM pending WHERE status_ts = ?", (status_ts,))
        return (str(row[0]), str(row[1])) if row else None

    def remove_pending(self, status_ts: str) -> None:
        self._write("DELETE FROM pending WHERE status_ts = ?", (status_ts,))

    def add_proposal(self, proposal_id: str, channel: str, thread_ts: str, card_ts: str, actions: str) -> None:
        self._write("INSERT INTO proposals (id, channel, thread_ts, card_ts, actions, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (proposal_id, channel, thread_ts, card_ts, actions, time.time()))

    def decide(self, proposal_id: str, status: str, user: str, max_age_s: float) -> tuple[str, str, str, str] | None:
        with self._lock, self._db:
            claimed = self._db.execute(
                "UPDATE proposals SET status = ?, decided_by = ?, decided_at = ? "
                "WHERE id = ? AND status = 'pending' AND created_at >= ?",
                (status, user, time.time(), proposal_id, time.time() - max_age_s)).rowcount
            if claimed != 1:
                return None
            row = self._db.execute("SELECT channel, thread_ts, card_ts, actions FROM proposals WHERE id = ?",
                                   (proposal_id,)).fetchone()
        return str(row[0]), str(row[1]), str(row[2]), str(row[3])

    def interrupted(self) -> list[tuple[str, str, str]]:
        with self._lock, self._db:
            rows = self._db.execute("SELECT id, channel, thread_ts FROM proposals WHERE status = 'running'").fetchall()
            self._db.execute("UPDATE proposals SET status = 'interrupted' WHERE status = 'running'")
        return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]

    def finish_proposal(self, proposal_id: str, status: str, result: str) -> None:
        self._write("UPDATE proposals SET status = ?, result = ? WHERE id = ?", (status, result, proposal_id))
