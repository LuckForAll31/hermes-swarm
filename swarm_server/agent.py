"""Agent daemon wrapper around a Hermes AIAgent instance."""

import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from swarm_server.config import LITELLM_API_BASE, SWEEP_INTERVAL_SECONDS, WORKSPACE_ROOT
from swarm_server.monitoring import monitor_db
from swarm_server.queue import TaskQueue
from swarm_server.tools import (
    _ASK_HUMAN_TOOL_SCHEMA,
    _SEND_PEER_MESSAGE_TOOL_SCHEMA,
    _daemon_registry,
    _register_custom_tools,
)
from swarm_server.websocket import _agent_init_lock, _broadcast

log = logging.getLogger("swarm.agent")

AGENT_STATE_IDLE = "idle"
AGENT_STATE_BUSY = "busy"


class AgentDaemon:
    def __init__(self, name: str, cfg: Dict[str, Any]) -> None:
        self.name = name
        self.cfg = cfg
        self.state = AGENT_STATE_IDLE
        self._lock = threading.Lock()

        workspace_dir = WORKSPACE_ROOT / cfg["workspace"]
        workspace_dir.mkdir(parents=True, exist_ok=True)
        db_path = workspace_dir / f"{name}_queue.db"
        self.queue = TaskQueue(db_path)

        # Each agent gets its own isolated Hermes home
        self._hermes_home = workspace_dir / ".hermes"
        self._hermes_home.mkdir(parents=True, exist_ok=True)

        self._ai_agent = None
        self._sweep_task: Optional[asyncio.Task] = None

        self.human_event = threading.Event()
        self.human_response = None
        self.next_sweep_at = 0.0

    def _ensure_agent(self):
        if self._ai_agent is not None:
            return
        with _agent_init_lock:
            if self._ai_agent is not None:
                return
            try:
                sys.path.insert(0, "/Users/pradhyun/.hermes/hermes-agent")
                from run_agent import AIAgent

                os.environ["HERMES_HOME"] = str(self._hermes_home)

                self._ai_agent = AIAgent(
                    base_url=LITELLM_API_BASE,
                    api_key="sk-1234",
                    model="litellm-model",
                    session_id=self.cfg["session_id"],
                    skip_memory=False,
                    skip_context_files=False,
                    quiet_mode=True,
                    ephemeral_system_prompt=self.cfg["soul"],
                )
                _register_custom_tools()

                existing_names = {
                    t.get("function", {}).get("name") for t in (self._ai_agent.tools or [])
                }
                if "send_peer_message" not in existing_names:
                    self._ai_agent.tools = list(self._ai_agent.tools or [])
                    self._ai_agent.tools.append(_SEND_PEER_MESSAGE_TOOL_SCHEMA)
                    self._ai_agent.valid_tool_names.add("send_peer_message")
                if "ask_human" not in existing_names:
                    self._ai_agent.tools = list(self._ai_agent.tools or [])
                    self._ai_agent.tools.append(_ASK_HUMAN_TOOL_SCHEMA)
                    self._ai_agent.valid_tool_names.add("ask_human")

                # Eagerly init session DB while HERMES_HOME is locked to this agent
                try:
                    sd = self._ai_agent._get_session_db_for_recall()
                    if sd is None:
                        log.error("[%s] _get_session_db_for_recall() returned None", self.name)
                    else:
                        log.info("[%s] SessionDB created at %s", self.name, getattr(sd, "_db_path", "?"))
                except Exception as e:
                    log.error("[%s] _get_session_db_for_recall() failed: %s", self.name, e)
                self._ai_agent._ensure_db_session()

                log.info(
                    "[%s] Hermes AIAgent initialised (session=%s, home=%s)",
                    self.name,
                    self.cfg["session_id"],
                    self._hermes_home,
                )
            except Exception as exc:
                log.error("[%s] Failed to init AIAgent: %s", self.name, exc)
                raise

    def _load_session_from_db(self) -> List[Dict[str, Any]]:
        """Load conversation history from agent's own isolated Hermes session DB."""
        if self._ai_agent is None:
            log.debug("[%s] _load_session_from_db: _ai_agent is None", self.name)
            return []
        session_db = getattr(self._ai_agent, "_session_db", None)
        if session_db is None:
            log.warning("[%s] _load_session_from_db: _session_db is None", self.name)
            return []
        try:
            current_sid = getattr(self._ai_agent, "session_id", None) or self.cfg["session_id"]
            msgs = session_db.get_messages_as_conversation(current_sid, include_ancestors=True)
            log.debug("[%s] Loaded %d messages from session %s", self.name, len(msgs), current_sid)
            return msgs
        except Exception as e:
            log.warning("[%s] Failed to load session from DB: %s", self.name, e)
            return []

    def ingest_task(self, from_agent: str, payload: str) -> str:
        task_id = self.queue.enqueue(from_agent, payload)
        log.info("[%s] Task queued from '%s': %s", self.name, from_agent, payload[:80])
        monitor_db.log_event(
            self.name,
            "task_enqueued",
            from_agent=from_agent,
            task_id=task_id,
            data={"payload_preview": payload[:100]},
        )
        _broadcast("queue_updated", {
            "agent_name": self.name,
            "pending_count": self.queue.get_pending_count(),
            "timestamp": time.time(),
        })
        return task_id

    async def sweep_loop(self):
        log.info("[%s] Sweep loop started (interval=%ds)", self.name, SWEEP_INTERVAL_SECONDS)
        while True:
            self.next_sweep_at = time.time() + SWEEP_INTERVAL_SECONDS
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            await self._sweep()

    async def _sweep(self):
        with self._lock:
            if self.state != AGENT_STATE_IDLE:
                return
            old_state = self.state
            self.state = AGENT_STATE_BUSY

        if old_state != AGENT_STATE_BUSY:
            _broadcast("state_change", {
                "agent_name": self.name,
                "state": self.state,
                "timestamp": time.time(),
                "next_sweep_at": self.next_sweep_at,
            })
            monitor_db.log_event(self.name, "state_change", data={"new_state": self.state})

        try:
            tasks = self.queue.drain_pending()
            if not tasks:
                with self._lock:
                    self.state = AGENT_STATE_IDLE
                    self.next_sweep_at = time.time() + SWEEP_INTERVAL_SECONDS
                _broadcast("state_change", {
                    "agent_name": self.name,
                    "state": self.state,
                    "timestamp": time.time(),
                    "next_sweep_at": self.next_sweep_at,
                })
                monitor_db.log_event(self.name, "state_change", data={"new_state": self.state})
                return

            log.info("[%s] Sweep: processing %d task(s) in batch", self.name, len(tasks))
            monitor_db.log_event(self.name, "task_dequeued", data={"count": len(tasks)})
            _broadcast("task_dequeued", {
                "agent_name": self.name,
                "count": len(tasks),
                "timestamp": time.time(),
            })

            await self._process_tasks_batch(tasks)
        finally:
            with self._lock:
                if self.state == AGENT_STATE_BUSY:
                    self.state = AGENT_STATE_IDLE
                    self.next_sweep_at = time.time() + SWEEP_INTERVAL_SECONDS
                    _broadcast("state_change", {
                        "agent_name": self.name,
                        "state": self.state,
                        "timestamp": time.time(),
                        "next_sweep_at": self.next_sweep_at,
                    })
                    monitor_db.log_event(self.name, "state_change", data={"new_state": self.state})

    async def _process_tasks_batch(self, tasks: List[Dict[str, Any]]):
        task_ids = [t["id"] for t in tasks]
        task_preview = ", ".join([t["id"][:8] for t in tasks])
        log.info("[%s] Processing batch: %s", self.name, task_preview)
        _broadcast("conversation_start", {
            "agent_name": self.name,
            "task_count": len(tasks),
            "task_ids": task_ids,
            "timestamp": time.time(),
        })

        combined = f"You have {len(tasks)} new message(s) to process:\n\n"
        for i, task in enumerate(tasks, 1):
            combined += f"--- [{i}] from {task['from_agent']} ---\n{task['payload']}\n\n"

        try:
            self._ensure_agent()
            history = self._load_session_from_db()
            response = await asyncio.to_thread(
                self._ai_agent.run_conversation,
                user_message=combined,
                task_id=f"agent_name:{self.name}",
                conversation_history=history,
            )
            new_messages = response.get("messages", [])
            final = str(response.get("final_response", ""))
            log.info("[%s] Batch complete. Response: %s", self.name, final[:200])

            last_user_idx = -1
            for i, msg in enumerate(new_messages):
                if msg.get("role") == "user":
                    last_user_idx = i
            turn_messages = new_messages[last_user_idx + 1 :] if last_user_idx >= 0 else new_messages

            for msg in turn_messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")

                if role == "assistant" and msg.get("tool_calls"):
                    tcs = msg["tool_calls"]
                    tool_summary = " | ".join([
                        f"{tc.get('function', {}).get('name', '?')}()" for tc in tcs
                    ])
                    content = f"🛠️ Tool Calls: {tool_summary}\n\n{content or ''}"

                if role == "tool":
                    tc_id = msg.get("tool_call_id", "?")
                    content = f"📤 Tool Result [{tc_id}]: {content}"

                monitor_db.log_message(self.name, role, content, ",".join(task_ids))
                _broadcast("message_logged", {
                    "agent_name": self.name,
                    "role": role,
                    "content": content,
                    "task_id": task_preview,
                    "timestamp": time.time(),
                })

            monitor_db.log_event(self.name, "conversation_complete", data={"response_preview": final[:200]})
            _broadcast("conversation_complete", {
                "agent_name": self.name,
                "task_count": len(tasks),
                "response_preview": final[:200],
                "timestamp": time.time(),
            })
            for t in tasks:
                self.queue.mark_done(t["id"])
        except Exception as exc:
            log.error("[%s] Batch failed: %s", self.name, exc)
            monitor_db.log_event(self.name, "error", data={"error": str(exc), "task_ids": task_ids})
            _broadcast("error", {
                "agent_name": self.name,
                "task_ids": task_ids,
                "error": str(exc),
                "timestamp": time.time(),
            })

    def start_sweep(self, loop: asyncio.AbstractEventLoop):
        self._sweep_task = loop.create_task(self.sweep_loop())
