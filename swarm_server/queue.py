"""SQLite-backed task queue per agent."""

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("swarm.queue")


class TaskQueue:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS tasks (
        id           TEXT PRIMARY KEY,
        from_agent   TEXT NOT NULL,
        payload      TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'pending',
        created_at   REAL NOT NULL,
        processed_at REAL
    );
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self):
        return sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)

    def _init_db(self):
        with self._conn() as conn:
            conn.execute(self.SCHEMA)
            conn.commit()

    def enqueue(self, from_agent: str, payload: str) -> str:
        task_id = str(uuid.uuid4())
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO tasks (id, from_agent, payload, status, created_at) VALUES (?,?,?,?,?)",
                (task_id, from_agent, payload, "pending", time.time()),
            )
            conn.commit()
        log.info("[Queue] Enqueued task %s from '%s'", task_id[:8], from_agent)
        # Monitoring log is injected by caller to avoid circular import
        return task_id

    def drain_pending(self) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT id, from_agent, payload FROM tasks WHERE status='pending' ORDER BY created_at"
            ).fetchall()
            if rows:
                ids = [r[0] for r in rows]
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE tasks SET status='processing', processed_at=? WHERE id IN ({placeholders})",
                    [time.time()] + ids,
                )
                conn.commit()
        return [{"id": r[0], "from_agent": r[1], "payload": r[2]} for r in rows]

    def mark_done(self, task_id: str):
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
            conn.commit()

    def get_pending_count(self) -> int:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'").fetchone()
            return row[0] if row else 0

    def get_all_tasks(self, limit: int = 50) -> List[dict]:
        with self._lock, self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
