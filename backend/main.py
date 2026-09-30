"""
FastAPI entry: serves the built frontend and exposes a /ws WebSocket that
backs a single ClaudeSession per connection.

Run:
    pip install fastapi uvicorn websockets
    uvicorn main:app --port 8765 --reload
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# Windows subprocess support (spawning `claude.cmd`) requires the Proactor
# event loop. uvicorn's CLI defaults to SelectorEventLoop on Windows, which
# raises NotImplementedError on create_subprocess_exec.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from claude_session import ClaudeSession, ClaudeSessionError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("claude-demo")

app = FastAPI(title="ClaudeDemo")

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


@app.get("/")
async def index():
    idx = FRONTEND_DIST / "index.html"
    if idx.exists():
        return FileResponse(idx)
    return {"detail": "frontend not built; run `cd frontend && npm run build`"}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    cwd = os.getcwd()
    model: str | None = None
    session: ClaudeSession | None = None

    async def forward_events():
        nonlocal session
        try:
            async for ev in session.events():  # type: ignore[union-attr]
                await websocket.send_json(ev)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.exception("forward error: %s", e)

    forward_task: asyncio.Task | None = None
    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")

            if mtype == "start":
                if session is None:
                    cwd = msg.get("cwd") or cwd
                    model = msg.get("model") or model
                    try:
                        session = await ClaudeSession.create(cwd=cwd, model=model)
                    except ClaudeSessionError as e:
                        await websocket.send_json({"type": "error", "message": str(e)})
                        continue
                    forward_task = asyncio.create_task(forward_events())
                    await websocket.send_json({"type": "ready"})
                else:
                    await websocket.send_json({"type": "error", "message": "session already running"})

            elif mtype == "user_message":
                if session is None:
                    await websocket.send_json({"type": "error", "message": "send `start` first"})
                    continue
                await session.send_user_message(msg.get("text", ""))

            elif mtype == "close":
                break

    except WebSocketDisconnect:
        log.info("ws disconnected")
    finally:
        if session:
            await session.close()
        if forward_task:
            forward_task.cancel()


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8765, reload=False)
