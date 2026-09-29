# ClaudeDemo

A minimal web UI that wraps the local `claude` CLI as a subprocess and streams
its conversation — including **real-time "thinking" blocks** — to a browser.

This is a stripped-down port of [cdesktop](https://github.com/cdesktop-ai/cdesktop)
(Rust + Tauri) to a plain Python + React stack, so you can see the same
mechanics with ~300 lines of code.

## How it works

```
Browser (React)
   ▲  WebSocket /ws  (JSON events)
   │
Python FastAPI (uvicorn)
   │  asyncio.create_subprocess_exec("claude", ...)
   ▼
local claude CLI  ←→  stdin/stdout (JSONL stream)
```

The CLI is launched with:

```
claude --output-format stream-json --input-format stream-json \
       --include-partial-messages --verbose --dangerously-skip-permissions
```

`--include-partial-messages` is the flag that makes streaming visible.
Each stdout line is a JSON event; the interesting ones are:

| event | meaning |
|---|---|
| `content_block_start` with `content_block.type == "thinking"` | a thinking block opens |
| `content_block_delta` with `delta.type == "thinking_delta"` | new thinking text arrives (we append it) |
| `content_block_delta` with `delta.type == "text_delta"` | new assistant text arrives |
| `content_block_stop` | block closes |
| `result` | turn finished, usage stats |

The backend accumulates deltas per content-block index and forwards a small
normalized event stream to the browser. The frontend renders thinking blocks
as collapsible gray cards and assistant text as plain bubbles.

## Run it

Prereqs: Python 3.11+, Node 20+, and the [`claude` CLI](https://docs.claude.com/en/docs/claude-code/quickstart)
installed and logged in locally.

```bash
# 1. backend
cd backend
pip install -r requirements.txt
uvicorn main:app --port 8765 --reload

# 2. frontend (in another terminal)
cd frontend
npm install
npm run build        # produces frontend/dist, served by FastAPI
# or, for hot-reload dev:
npm run dev          # Vite proxies /ws to 127.0.0.1:8765
```

Open http://127.0.0.1:8765 (after `npm run build`) or http://127.0.0.1:5173
(for the Vite dev server). Type a message, hit Enter.

## Project layout

```
backend/
  main.py            FastAPI app, /ws WebSocket endpoint, serves frontend/dist
  claude_session.py  subprocess spawn, JSONL parsing, event normalization
frontend/
  src/App.tsx        chat UI, WS client, thinking-block rendering
```

## Migrating to your Tauri/Rust app

The Rust equivalent of `claude_session.py` is straightforward:
- `tokio::process::Command` with the same args
- `BufReader` over stdout, `serde_json::from_str::<CliMessage>` per line
- a `HashMap<usize, (kind, buffer)>` for streaming deltas
- instead of WebSocket, use `app.emit("claude:event", ...)` and `window.listen(...)`
  in the webview — zero ports, zero CORS.

See `claude_session.py` for the exact event shape to mirror.
