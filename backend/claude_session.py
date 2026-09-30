"""
Claude Code subprocess wrapper.

Spawns the local `claude` CLI in stream-json mode and converts its JSONL
stdout into a small, frontend-friendly event stream. This is a minimal
port of cdesktop's protocol (crates/executors/src/executors/claude.rs).

Key CLI flags:
  --output-format stream-json     stdout = JSONL events
  --input-format  stream-json     stdin  = JSONL user messages
  --include-partial-messages      emit content_block_delta (thinking_delta, text_delta)
  --verbose
  --dangerously-skip-permissions  minimal demo: no can_use_tool handshake
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


class ClaudeSessionError(Exception):
    pass


@dataclass
class _BlockState:
    kind: str  # "thinking" | "text" | "tool_use" | "other"
    buffer: str = ""


@dataclass
class ClaudeSession:
    """One claude CLI child process, exposed as an async event stream."""

    cwd: str = "."
    model: str | None = None
    # set after spawn
    _proc: asyncio.subprocess.Process | None = field(default=False, init=False, repr=False)  # type: ignore[assignment]
    _blocks: dict[int, _BlockState] = field(default_factory=dict, init=False)
    _event_queue: asyncio.Queue[dict[str, Any] | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=10000), init=False
    )

    # ------------------------------------------------------------------ spawn

    @classmethod
    async def create(cls, cwd: str = ".", model: str | None = None) -> "ClaudeSession":
        import sys
        exe = shutil.which("claude")
        if not exe:
            raise ClaudeSessionError(
                "`claude` CLI not found in PATH. Install it first: "
                "https://docs.claude.com/en/docs/claude-code/quickstart"
            )

        base_args = [
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if model:
            base_args += ["--model", model]

        # On Windows, Node-installed CLIs are `claude.cmd` shims. CreateProcess
        # cannot run .cmd/.bat directly — wrap with `cmd.exe /c`.
        if sys.platform == "win32" and exe.lower().endswith((".cmd", ".bat")):
            args = ["cmd.exe", "/c", exe, *base_args]
        else:
            args = [exe, *base_args]

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        sess = cls(cwd=cwd, model=model)
        sess._proc = proc
        sess._emit(type="log", message=f"spawning: {' '.join(args)}")
        asyncio.create_task(sess._pump_stdout())
        asyncio.create_task(sess._pump_stderr())
        return sess

    # ----------------------------------------------------------------- events

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            ev = await self._event_queue.get()
            if ev is None:
                break
            yield ev

    def _emit(self, **kw: Any) -> None:
        try:
            self._event_queue.put_nowait(kw)
        except asyncio.QueueFull:
            logger.warning("event queue full, dropping event %s", kw.get("type"))

    # --------------------------------------------------------------- user io

    async def send_user_message(self, text: str) -> None:
        if not self._proc or not self._proc.stdin:
            raise ClaudeSessionError("session not started")
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self._emit(type="user_echo", text=text)
        line = (json.dumps(msg) + "\n").encode()
        self._proc.stdin.write(line)
        await self._proc.stdin.drain()

    async def close(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._emit(type="closed")
        await self._event_queue.put(None)

    # ------------------------------------------------------------- stdout pump

    async def _pump_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON stdout line — surface it as a log so the user sees it.
                    self._emit(type="log", message="stdout: " + line.decode(errors="replace").rstrip())
                    continue
                self._handle_message(msg)
        finally:
            try:
                rc = await self._proc.wait()
            except Exception:
                rc = -1
            self._emit(type="exit", code=rc)
            self._emit(type="closed")
            await self._event_queue.put(None)

    async def _pump_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            logger.info("claude stderr: %s", text)
            self._emit(type="log", message="stderr: " + text)

    # ---------------------------------------------------------- message router

    def _handle_message(self, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        if t == "system":
            self._emit(
                type="system_init",
                model=msg.get("model"),
                session_id=msg.get("session_id"),
                cwd=msg.get("cwd"),
            )
            return

        if t == "result":
            self._emit(
                type="result",
                is_error=bool(msg.get("is_error")),
                usage=msg.get("usage"),
                duration_ms=msg.get("duration_ms") or msg.get("durationMs"),
            )
            return

        if t == "stream_event":
            self._handle_stream_event(msg.get("event") or {})
            return

        if t == "assistant":
            # Non-streaming fallback: emit each content block as a complete block.
            content = (msg.get("message") or {}).get("content") or []
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for i, item in enumerate(content):
                self._emit_block_from_item(i, item)
            return

        # user / tool_use / tool_result / control_request: ignored in minimal demo.
        return

    def _emit_block_from_item(self, index: int, item: dict[str, Any]) -> None:
        itype = item.get("type")
        if itype == "text":
            kind, text = "text", item.get("text", "")
        elif itype == "thinking":
            kind, text = "thinking", item.get("thinking", "")
        elif itype == "tool_use":
            kind, text = "tool_use", item.get("name", "tool")
        else:
            return
        self._emit(type="block_start", index=index, kind=kind)
        if text:
            self._emit(type="block_delta", index=index, kind=kind, text=text)
        self._emit(type="block_stop", index=index)

    # ----------------------------------------------------------- stream events

    def _handle_stream_event(self, ev: dict[str, Any]) -> None:
        et = ev.get("type")

        if et == "content_block_start":
            idx = ev.get("index", 0)
            cb = ev.get("content_block") or {}
            cbtype = cb.get("type")
            if cbtype == "text":
                kind, text = "text", cb.get("text", "")
            elif cbtype == "thinking":
                kind, text = "thinking", cb.get("thinking", "")
            elif cbtype == "tool_use":
                kind, text = "tool_use", cb.get("name", "tool")
            else:
                kind, text = "other", ""
            self._blocks[idx] = _BlockState(kind=kind, buffer=text)
            self._emit(type="block_start", index=idx, kind=kind)
            if text:
                self._emit(type="block_delta", index=idx, kind=kind, text=text)
            return

        if et == "content_block_delta":
            idx = ev.get("index", 0)
            delta = ev.get("delta") or {}
            dt = delta.get("type")
            block = self._blocks.setdefault(idx, _BlockState(kind="other"))
            if dt == "text_delta":
                block.kind = "text"
                block.buffer += delta.get("text", "")
                self._emit(type="block_delta", index=idx, kind="text", text=block.buffer)
            elif dt == "thinking_delta":
                block.kind = "thinking"
                block.buffer += delta.get("thinking", "")
                self._emit(type="block_delta", index=idx, kind="thinking", text=block.buffer)
            elif dt == "signature_delta":
                pass  # encrypted reasoning signature; not displayable
            return

        if et == "content_block_stop":
            idx = ev.get("index", 0)
            self._emit(type="block_stop", index=idx)
            return

        # message_start / message_delta / message_stop: nothing to surface yet.
        return
