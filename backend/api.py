from __future__ import annotations
"""
backend/api.py
FastAPI REST + WebSocket server for the FRAME-MO frontend dashboard.

Endpoints:
  POST /tasks          — Start a new task
  GET  /tasks          — List all checkpointed tasks
  GET  /tasks/{id}     — Get live status of a task
  POST /tasks/{id}/hitl — Submit HITL approval decision
  WS   /ws/{id}        — WebSocket stream for live agent updates
  GET  /mcp/health     — MCP server health status
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.checkpoint import CheckpointStore
from core.orchestrator import MasterOrchestrator
from tools.mcp_manager import MCPConnectionManager

log = logging.getLogger("frame_mo.api")

# ── Shared singletons ─────────────────────────────────────────────────────────
checkpoint = CheckpointStore(
    db_path=os.environ.get("CHECKPOINT_DB_PATH", "agent_checkpoints.db")
)
mcp = MCPConnectionManager()
orchestrator = MasterOrchestrator(checkpoint_store=checkpoint, mcp_manager=mcp)

# Active WebSocket connections: task_id → list[WebSocket]
_ws_clients: dict[str, list[WebSocket]] = {}


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Connecting MCP servers...")
    await mcp.connect_all()
    log.info("API server ready.")
    yield
    await mcp.close_all()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="FRAME-MO API",
    description="Fault-Resilient Agentic Multi-Orchestral Engine — Dashboard API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request/Response models ───────────────────────────────────────────────────

class StartTaskRequest(BaseModel):
    goal: str
    task_id: str | None = None
    inject_failure: str | None = None     # "github:rate_limit"


class HITLDecisionRequest(BaseModel):
    approved: bool


# ── REST Endpoints ────────────────────────────────────────────────────────────

@app.post("/tasks", summary="Start a new agent task")
async def start_task(req: StartTaskRequest):
    inject = {}
    if req.inject_failure:
        for item in req.inject_failure.split(","):
            parts = item.strip().split(":")
            if len(parts) == 2:
                inject[parts[0]] = parts[1]

    orch = MasterOrchestrator(
        checkpoint_store=checkpoint,
        mcp_manager=mcp,
        inject_failures=inject,
    )

    task_id = req.task_id or None

    async def _run_and_broadcast():
        try:
            result = await orch.run(goal=req.goal, task_id=task_id)
            actual_id = result.get("task_id")
            await _broadcast(actual_id, {"event": "complete", "result": result})
        except Exception as exc:
            await _broadcast(task_id or "unknown", {"event": "error", "message": str(exc)})

    asyncio.create_task(_run_and_broadcast())
    actual_id = task_id or "pending"
    return {"task_id": actual_id, "status": "started", "goal": req.goal}


@app.get("/tasks", summary="List all checkpointed tasks")
async def list_tasks():
    return checkpoint.list_tasks()


@app.get("/tasks/{task_id}", summary="Get live task status")
async def get_task(task_id: str):
    status = orchestrator.get_live_status(task_id)
    if not status:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return status


@app.post("/tasks/{task_id}/hitl", summary="Submit HITL approval decision")
async def hitl_decision(task_id: str, req: HITLDecisionRequest):
    success = orchestrator.hitl.submit_decision(task_id, req.approved)
    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"No pending HITL request for task '{task_id}'"
        )
    outcome = "approved" if req.approved else "rejected"
    await _broadcast(task_id, {"event": "hitl_decision", "decision": outcome})
    return {"task_id": task_id, "decision": outcome}


@app.get("/tasks/{task_id}/hitl/pending", summary="Get pending HITL requests")
async def get_hitl_pending(task_id: str):
    pending = [
        p for p in orchestrator.hitl.get_pending()
        if p["task_id"] == task_id
    ]
    return {"task_id": task_id, "pending": pending}


@app.get("/mcp/health", summary="MCP server health status")
async def mcp_health():
    return mcp.get_health()


@app.get("/health", summary="API health check")
async def health():
    return {"status": "ok", "version": "1.0.0"}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/{task_id}")
async def websocket_endpoint(websocket: WebSocket, task_id: str):
    await websocket.accept()
    _ws_clients.setdefault(task_id, []).append(websocket)
    log.info(f"[ws] Client connected for task_id={task_id}")

    try:
        # Send current state immediately on connect
        status = orchestrator.get_live_status(task_id)
        if status:
            await websocket.send_text(json.dumps({"event": "status", **status}))

        # Keep connection alive — poll for status updates every second
        while True:
            await asyncio.sleep(1)
            status = orchestrator.get_live_status(task_id)
            if status:
                await websocket.send_text(
                    json.dumps({"event": "status", **status})
                )
            if status and status.get("status") in ("complete", "failed"):
                break

    except WebSocketDisconnect:
        log.info(f"[ws] Client disconnected for task_id={task_id}")
    finally:
        if task_id in _ws_clients:
            _ws_clients[task_id] = [
                ws for ws in _ws_clients[task_id] if ws != websocket
            ]


async def _broadcast(task_id: str, message: dict):
    """Send a message to all WebSocket clients watching a task."""
    clients = _ws_clients.get(task_id, [])
    disconnected = []
    for ws in clients:
        try:
            await ws.send_text(json.dumps(message, default=str))
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        clients.remove(ws)
