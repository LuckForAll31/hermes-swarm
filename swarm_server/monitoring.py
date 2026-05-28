"""Central monitoring database for events and messages."""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("swarm.monitoring")


class MonitoringDB:
    """Central SQLite database for all monitoring events and message history."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp   REAL    NOT NULL,
        agent_name  TEXT    NOT NULL,
        event_type  TEXT    NOT NULL,
        from_agent  TEXT,
        to_agent    TEXT,
        task_id     TEXT,
        data        TEXT
    );

    CREATE TABLE IF NOT EXISTS messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp   REAL    NOT NULL,
        agent_name  TEXT    NOT NULL,
        role        TEXT    NOT NULL,
        content     TEXT    NOT NULL,
        task_id     TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_events_agent     ON events(agent_name);
    CREATE INDEX IF NOT EXISTS idx_events_time      ON events(timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_events_type      ON events(event_type);
    CREATE INDEX IF NOT EXISTS idx_messages_agent   ON messages(agent_name);
    CREATE INDEX IF NOT EXISTS idx_messages_time    ON messages(timestamp DESC);
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        return sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(self.SCHEMA)
            conn.commit()

    def log_event(
        self,
        agent_name: str,
        event_type: str,
        from_agent: str = None,
        to_agent: str = None,
        task_id: str = None,
        data: dict = None,
    ):
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO events (timestamp, agent_name, event_type, from_agent, to_agent, task_id, data)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        time.time(),
                        agent_name,
                        event_type,
                        from_agent,
                        to_agent,
                        task_id,
                        json.dumps(data) if data else None,
                    ),
                )
                conn.commit()
        except Exception as e:
            log.warning("[MonitorDB] Failed to log event: %s", e)

    def log_message(self, agent_name: str, role: str, content: str, task_id: str = None):
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO messages (timestamp, agent_name, role, content, task_id) VALUES (?, ?, ?, ?, ?)",
                    (time.time(), agent_name, role, content, task_id),
                )
                conn.commit()
        except Exception as e:
            log.warning("[MonitorDB] Failed to log message: %s", e)

    def get_events(self, agent_name: str = None, limit: int = 100, offset: int = 0) -> List[dict]:
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                if agent_name:
                    rows = conn.execute(
                        "SELECT * FROM events WHERE agent_name = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                        (agent_name, limit, offset),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM events ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                        (limit, offset),
                    ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            log.warning("[MonitorDB] Failed to get events: %s", e)
            return []

    def get_messages(self, agent_name: str, limit: int = 100, offset: int = 0) -> List[dict]:
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM messages WHERE agent_name = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                    (agent_name, limit, offset),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            log.warning("[MonitorDB] Failed to get messages: %s", e)
            return []

    def get_agent_stats(self) -> Dict[str, dict]:
        stats = {}
        try:
            with self._conn() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT agent_name, event_type, COUNT(*) as count FROM events GROUP BY agent_name, event_type"
                ).fetchall()
                for r in rows:
                    aname = r["agent_name"]
                    if aname not in stats:
                        stats[aname] = {"events": {}, "last_active": None, "total_messages": 0}
                    stats[aname]["events"][r["event_type"]] = r["count"]

                rows = conn.execute(
                    "SELECT agent_name, MAX(timestamp) as last_ts FROM events GROUP BY agent_name"
                ).fetchall()
                for r in rows:
                    if r["agent_name"] in stats:
                        stats[r["agent_name"]]["last_active"] = r["last_ts"]

                rows = conn.execute(
                    "SELECT agent_name, COUNT(*) as count FROM messages GROUP BY agent_name"
                ).fetchall()
                for r in rows:
                    if r["agent_name"] in stats:
                        stats[r["agent_name"]]["total_messages"] = r["count"]
        except Exception as e:
            log.warning("[MonitorDB] Failed to get stats: %s", e)
        return stats


# Global singleton instance
from swarm_server.config import MONITORING_DB  # noqa: E402

monitor_db = MonitoringDB(MONITORING_DB)
