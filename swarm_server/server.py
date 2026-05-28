"""FastAPI application, REST routes, WebSocket endpoint, and lifecycle management."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from swarm_server.agent import AgentDaemon
from swarm_server.config import (
    AGENTS,
    DASHBOARD_DIR,
    LITELLM_API_BASE,
    MONITORING_DB,
    SERVER_HOST,
    SERVER_PORT,
    WORKSPACE_ROOT,
    load_agents_config,
    save_agent_config,
)
from swarm_server.monitoring import monitor_db
from swarm_server.tools import _daemon_registry
from swarm_server.websocket import _main_event_loop, ws_broadcaster

log = logging.getLogger("swarm.server")

# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(title="Hermes Swarm Server", version="0.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

daemons: Dict[str, AgentDaemon] = {}


# ---------------------------------------------------------------------------
# WebSocket Endpoint
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    import time

    await ws_broadcaster.connect(ws)
    try:
        state_snapshot = {
            "type": "state_snapshot",
            "payload": {
                "agents": {
                    name: {
                        "state": d.state,
                        "pending_count": d.queue.get_pending_count(),
                        "config": d.cfg,
                        "next_sweep_at": d.next_sweep_at,
                    }
                    for name, d in daemons.items()
                },
                "timestamp": time.time(),
            },
        }
        await ws.send_text(json.dumps(state_snapshot))

        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("action") == "ping":
                    await ws.send_text(json.dumps({"type": "pong", "payload": {}}))
            except Exception:
                pass
    except WebSocketDisconnect:
        await ws_broadcaster.disconnect(ws)
    except Exception as e:
        log.warning("[WS] Error: %s", e)
        await ws_broadcaster.disconnect(ws)


# ---------------------------------------------------------------------------
# Core Agent Routes
# ---------------------------------------------------------------------------
@app.post("/agent/{agent_name}/task")
async def agent_ingest(agent_name: str, request: Request):
    body = await request.json()
    from_agent = body.get("from_agent", "unknown")
    payload = body.get("payload", "")
    if not payload:
        return JSONResponse({"error": "empty payload"}, status_code=400)
    daemon = daemons.get(agent_name)
    if daemon is None:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    task_id = daemon.ingest_task(from_agent, payload)
    return JSONResponse({"task_id": task_id, "status": "queued"})


@app.get("/agent/{agent_name}/status")
async def agent_status(agent_name: str):
    daemon = daemons.get(agent_name)
    if daemon is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({
        "agent": agent_name,
        "state": daemon.state,
        "pending_count": daemon.queue.get_pending_count(),
        "session_id": daemon.cfg.get("session_id"),
    })


@app.post("/agent/{agent_name}/human_response")
async def human_response(agent_name: str, request: Request):
    body = await request.json()
    response_text = body.get("response", "")
    if not response_text:
        return JSONResponse({"error": "empty response"}, status_code=400)
    daemon = daemons.get(agent_name)
    if daemon is None:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    if daemon.state != "asking_human":
        return JSONResponse(
            {"error": f"Agent is not asking human (state: {daemon.state})"},
            status_code=400,
        )
    daemon.human_response = response_text
    daemon.human_event.set()
    return {"status": "ok", "message": "Response sent to agent."}


# ---------------------------------------------------------------------------
# Monitoring Routes
# ---------------------------------------------------------------------------
@app.get("/monitoring/agents")
async def monitoring_agents():
    import time

    return JSONResponse({
        "agents": {
            name: {
                "state": d.state,
                "pending_count": d.queue.get_pending_count(),
                "next_sweep_at": d.next_sweep_at,
                "workspace": d.cfg.get("workspace"),
                "session_id": d.cfg.get("session_id"),
                "soul_preview": d.cfg.get("soul", "")[:200],
            }
            for name, d in daemons.items()
        },
        "timestamp": time.time(),
    })


@app.get("/monitoring/agents/{agent_name}/events")
async def monitoring_events(agent_name: str, limit: int = 50):
    events = monitor_db.get_events(agent_name=agent_name, limit=limit)
    return JSONResponse({"agent": agent_name, "events": events})


@app.get("/monitoring/agents/{agent_name}/messages")
async def monitoring_messages(agent_name: str, limit: int = 200, offset: int = 0):
    messages = monitor_db.get_messages(agent_name=agent_name, limit=limit, offset=offset)
    messages.reverse()
    return JSONResponse({"agent": agent_name, "messages": messages})


@app.get("/monitoring/agents/{agent_name}/queue")
async def monitoring_queue(agent_name: str):
    daemon = daemons.get(agent_name)
    if daemon is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    tasks = daemon.queue.get_all_tasks(limit=100)
    return JSONResponse({
        "agent": agent_name,
        "pending_count": daemon.queue.get_pending_count(),
        "tasks": tasks,
    })


@app.get("/monitoring/stats")
async def monitoring_stats():
    import time

    stats = monitor_db.get_agent_stats()
    for name, daemon in daemons.items():
        if name not in stats:
            stats[name] = {"events": {}, "total_messages": 0}
        stats[name]["current_state"] = daemon.state
        stats[name]["pending_count"] = daemon.queue.get_pending_count()
    return JSONResponse({"stats": stats, "timestamp": time.time()})


@app.get("/monitoring/recent_events")
async def monitoring_recent(limit: int = 100):
    events = monitor_db.get_events(limit=limit)
    return JSONResponse({"events": events})


# ---------------------------------------------------------------------------
# Agent Management Routes
# ---------------------------------------------------------------------------
@app.get("/agents")
async def get_agents():
    return JSONResponse(load_agents_config())


@app.post("/agent")
async def add_or_update_agent(request: Request):
    body = await request.json()
    agent_name = body.get("agent_name")
    name = body.get("name")
    session_id = body.get("session_id")
    workspace = body.get("workspace")
    port = body.get("port")
    peer_name = body.get("peer_name")
    peer_port = body.get("peer_port")
    soul = body.get("soul")

    if not agent_name or not name or not session_id or not workspace or not soul:
        return JSONResponse({"error": "Missing required fields"}, status_code=400)

    cfg = {
        "name": name,
        "session_id": session_id,
        "workspace": workspace,
        "port": port,
        "peer_name": peer_name,
        "peer_port": peer_port,
        "soul": soul,
    }
    save_agent_config(agent_name, cfg)
    loop = asyncio.get_running_loop()
    if agent_name in daemons:
        daemon = daemons[agent_name]
        with daemon._lock:
            daemon.cfg = cfg
            daemon._ai_agent = None
    else:
        register_agent_daemon(agent_name, cfg, loop)
    return JSONResponse({"status": "success", "message": f"Agent '{agent_name}' registered/updated."})


@app.post("/agent/{agent_name}/soul")
async def update_agent_soul(agent_name: str, request: Request):
    body = await request.json()
    soul = body.get("soul")
    if not soul:
        return JSONResponse({"error": "Missing 'soul' field"}, status_code=400)

    config = load_agents_config()
    if agent_name not in config:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    cfg = config[agent_name]
    cfg["soul"] = soul
    save_agent_config(agent_name, cfg)

    if agent_name in daemons:
        daemon = daemons[agent_name]
        with daemon._lock:
            daemon.cfg = cfg
            daemon._ai_agent = None
        log.info("[Dynamic Registry] Soul updated for agent '%s'", agent_name)

    from swarm_server.websocket import _broadcast

    _broadcast("soul_updated", {"agent_name": agent_name, "timestamp": __import__("time").time()})
    return JSONResponse({"status": "success", "message": f"Soul for '{agent_name}' updated."})


@app.get("/health")
async def health():
    return {"status": "ok", "agents": list(daemons.keys())}


# ---------------------------------------------------------------------------
# Dashboard Root
# ---------------------------------------------------------------------------
@app.get("/")
async def root_dashboard():
    dashboard_file = DASHBOARD_DIR / "index.html"
    if dashboard_file.exists():
        return FileResponse(str(dashboard_file))
    return HTMLResponse(
        "<h1>Dashboard not found</h1><p>Run from project root.</p>",
        status_code=404,
    )


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup():
    global _main_event_loop
    _main_event_loop = asyncio.get_running_loop()
    log.info("[Startup] Main event loop captured: %s", _main_event_loop)

    from swarm_server.websocket import _broadcast

    for agent_name, cfg in AGENTS.items():
        db_path = WORKSPACE_ROOT / cfg["workspace"] / f"{agent_name}_queue.db"
        if db_path.exists():
            try:
                db_path.unlink()
                log.info("[Startup] Cleaned up previous DB for '%s'", agent_name)
            except Exception as e:
                log.warning("[Startup] Could not delete DB %s: %s", db_path, e)

    loop = asyncio.get_running_loop()
    for agent_name, cfg in AGENTS.items():
        register_agent_daemon(agent_name, cfg, loop)

    log.info("[Startup] All agents running. LiteLLM at %s", LITELLM_API_BASE)
    log.info("[Startup] Dashboard at http://%s:%s/", SERVER_HOST, SERVER_PORT)


@app.on_event("shutdown")
async def on_shutdown():
    for daemon in daemons.values():
        if daemon._sweep_task:
            daemon._sweep_task.cancel()
    log.info("[Shutdown] All sweep tasks cancelled")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def register_agent_daemon(agent_name: str, cfg: Dict[str, Any], loop: asyncio.AbstractEventLoop):
    daemon = AgentDaemon(agent_name, cfg)
    daemons[agent_name] = daemon
    _daemon_registry[agent_name] = daemon
    daemon.start_sweep(loop)
    log.info("[Dynamic Registry] Registered agent '%s' daemon", agent_name)
