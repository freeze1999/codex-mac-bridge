#!/usr/bin/env python3
"""
Codex Mac Bridge, MCP Server
Exposes an `ask_codex` tool so an AI agent can delegate tasks to OpenAI
Codex CLI running on a remote Mac via SSH over Tailscale.

Configuration (env vars):
  CODEX_BRIDGE_SSH_HOST   SSH target, e.g. user@100.x.x.x  (required)
  CODEX_BRIDGE_CODEX_BIN  Path to codex binary on the Mac   (default: codex)
  CODEX_BRIDGE_TIMEOUT    Seconds before timeout            (default: 600)
  CODEX_BRIDGE_MODEL      Model override, e.g. gpt-4o       (optional)
  CODEX_BRIDGE_LOG_PATH   Path for delegation audit log     (default: ./bridge.log)
  CODEX_BRIDGE_LOG_MAX_BYTES  Rotate the log past this size (default: 10000000)
  CODEX_BRIDGE_SANDBOX    Codex --sandbox mode, e.g. read-only or
    workspace-write (optional). Unset keeps the Codex CLI's own default.
    The bridge always runs with --ask-for-approval never (exec mode is
    non-interactive), so the sandbox IS the safety boundary: any agent that
    can call this MCP tool can run whatever the sandbox mode allows on the
    Mac. Set read-only unless you need writes.
"""

import asyncio
import json
import os
import shlex
import time
import uuid
from datetime import datetime, timezone

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

MAC_HOST       = os.environ.get("CODEX_BRIDGE_SSH_HOST", "")
MAC_CODEX      = os.environ.get("CODEX_BRIDGE_CODEX_BIN", "codex")
CODEX_TIMEOUT  = int(os.environ.get("CODEX_BRIDGE_TIMEOUT", "600"))
CODEX_MODEL    = os.environ.get("CODEX_BRIDGE_MODEL", "").strip()
CODEX_SANDBOX  = os.environ.get("CODEX_BRIDGE_SANDBOX", "").strip()
LOG_MAX_BYTES  = int(os.environ.get("CODEX_BRIDGE_LOG_MAX_BYTES", "10000000"))
SSH_OPTS       = ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
                  "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]
LOG_FILE       = os.environ.get("CODEX_BRIDGE_LOG_PATH",
                 os.path.join(os.path.dirname(__file__), "bridge.log"))

app = Server("codex-bridge")


async def _log_codex_version():
    """Log the Codex CLI version at startup for debugging."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", *SSH_OPTS, MAC_HOST, f"{MAC_CODEX} --version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        version = out.decode().strip().splitlines()[0] if out else "unknown"
        log_event({"event": "startup", "codex_version": version,
                   "ts": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        log_event({"event": "startup", "codex_version": f"error: {e}",
                   "ts": datetime.now(timezone.utc).isoformat()})

# Event types that are tool calls / shell execs, excluded from response extraction
_TOOL_TYPES = frozenset((
    "tool_call", "tool_result", "function_call", "function_result",
    "bash", "shell", "exec", "command",
))


def should_rotate(path: str, max_bytes: int) -> bool:
    try:
        return max_bytes > 0 and os.path.getsize(path) >= max_bytes
    except OSError:
        return False


def log_event(event: dict):
    """Append one JSON line; size-rotate to a single .1 generation first.
    The log holds full task/response text, rotation keeps it from growing
    without bound."""
    try:
        if should_rotate(LOG_FILE, LOG_MAX_BYTES):
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(event) + "\n")
            f.flush()
    except Exception:
        pass


def build_codex_args(codex_bin: str, resume_session: str, model: str,
                     sandbox: str) -> list[str]:
    """The literal '"$TASK"' is load-bearing: the task text is piped in over
    stdin (remote command starts with TASK=$(cat)) so it never touches the
    command line and cannot inject into the shell. Do not "fix" it into an
    f-string or shlex.quote of the prompt.

    New session:  codex exec --ask-for-approval never --json [opts] "$TASK"
    Resume:       codex exec resume SESSION_ID "$TASK" [opts]
                  Per Codex docs the follow-up prompt is positional after
                  the session id."""
    common = ["--ask-for-approval", "never", "--json"]
    if model:
        common += ["--model", shlex.quote(model)]
    if sandbox:
        common += ["--sandbox", shlex.quote(sandbox)]
    if resume_session:
        return ([codex_bin, "exec", "resume", shlex.quote(resume_session),
                 '"$TASK"'] + common)
    return [codex_bin, "exec"] + common + ['"$TASK"']


def build_remote_command(args: list[str], timeout_s: int) -> str:
    """Wrap with a remote timeout so Codex is killed on the Mac if SSH drops.
    Portable: 'timeout' (GNU) or 'gtimeout' (macOS + coreutils), or no
    timeout if neither exists. Remote budget is 10s under the local one so
    the remote side dies first."""
    remote_timeout = max(timeout_s - 10, 30)
    prefix = (
        '_T=$(command -v timeout 2>/dev/null || command -v gtimeout 2>/dev/null || true); '
        f'${{_T:+$_T {remote_timeout}}}'
    )
    return f'TASK=$(cat); {prefix} {" ".join(args)}'


def _parse_codex_output(raw: str) -> tuple[str, str]:
    """
    Parse NDJSON output from `codex exec --json`.
    Returns (response_text, session_id).

    Event type names are not filtered by hardcoded strings, any event with a
    content/text/message field is treated as a potential response. Only known
    tool-call event types are excluded. This keeps the parser working across
    Codex CLI versions regardless of exact event naming.

    Falls back to raw stdout if no structured events are found.
    """
    response   = ""
    session_id = ""
    events     = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    if not events:
        return raw.strip(), ""

    # Walk in reverse: grab last non-tool event with content + session_id
    for ev in reversed(events):
        ev_type = ev.get("type", "")

        if not session_id:
            session_id = ev.get("session_id", "") or ev.get("sessionId", "")

        if not response and ev_type not in _TOOL_TYPES:
            content = (ev.get("content", "") or ev.get("text", "")
                       or ev.get("message", "") or ev.get("output", "")
                       or ev.get("response", ""))
            if isinstance(content, list):
                content = " ".join(
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
            if isinstance(content, str) and content.strip():
                response = content.strip()

        if response and session_id:
            break

    # Last fallback: join all non-tool text across all events
    if not response:
        parts = []
        for ev in events:
            if ev.get("type", "") not in _TOOL_TYPES:
                text = (ev.get("content", "") or ev.get("text", "")
                        or ev.get("message", "") or ev.get("output", "")
                        or ev.get("response", ""))
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        response = "\n".join(parts) or raw.strip()

    return response, session_id


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="ask_codex",
            description=(
                "Delegate a coding task to OpenAI Codex CLI running on a remote Mac via SSH. "
                "Full Codex agent with Mac filesystem access, can write, run, and debug code. "
                "Returns Codex's response plus a session_id, pass session_id back as "
                "resume_session_id to continue the same conversation across calls."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The coding task or question for Codex. Be specific."
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional extra context: code snippets, error messages, background."
                    },
                    "resume_session_id": {
                        "type": "string",
                        "description": (
                            "Optional. Session ID returned by a previous ask_codex call. "
                            "Pass this to continue the same session."
                        )
                    }
                },
                "required": ["task"]
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name != "ask_codex":
        raise ValueError(f"Unknown tool: {name}")

    if not MAC_HOST:
        return [types.TextContent(type="text",
            text="Error: CODEX_BRIDGE_SSH_HOST env var not set. See README.")]

    task           = arguments.get("task", "").strip()
    context        = arguments.get("context", "").strip()
    resume_session = arguments.get("resume_session_id", "").strip()

    if not task:
        return [types.TextContent(type="text", text="Error: `task` cannot be empty.")]

    call_id    = uuid.uuid4().hex[:8]
    started_at = time.monotonic()
    ts         = datetime.now(timezone.utc).isoformat()

    log_event({
        "event":      "start",
        "id":         call_id,
        "ts":         ts,
        "task":       task,
        "context":    context or None,
        "session_id": resume_session or None,
    })

    prompt = f"Context:\n{context}\n\n---\n\nTask:\n{task}" if context else task

    codex_args = build_codex_args(MAC_CODEX, resume_session, CODEX_MODEL,
                                  CODEX_SANDBOX)
    remote_cmd = build_remote_command(codex_args, CODEX_TIMEOUT)

    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", *SSH_OPTS, MAC_HOST, remote_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()), timeout=CODEX_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            duration = round(time.monotonic() - started_at, 1)
            log_event({"event": "timeout", "id": call_id,
                       "ts": datetime.now(timezone.utc).isoformat(), "duration": duration})
            return [types.TextContent(type="text",
                text=f"Codex timed out after {CODEX_TIMEOUT}s.")]

        duration = round(time.monotonic() - started_at, 1)

        if proc.returncode != 0:
            err = stderr.decode().strip() or f"exit code {proc.returncode}"
            log_event({"event": "error", "id": call_id,
                       "ts": datetime.now(timezone.utc).isoformat(),
                       "duration": duration, "error": err})
            return [types.TextContent(type="text", text=f"Codex error: {err}")]

        raw            = stdout.decode().strip()
        result, session_id = _parse_codex_output(raw)

        log_event({
            "event":      "done",
            "id":         call_id,
            "ts":         datetime.now(timezone.utc).isoformat(),
            "duration":   duration,
            "session_id": session_id,
            "response":   result,
        })

        footer = f"\n\n---\n session_id: `{session_id}`  {duration}s" if session_id else f"\n\n---\n {duration}s"
        return [types.TextContent(type="text", text=result + footer)]

    except Exception as exc:
        duration = round(time.monotonic() - started_at, 1)
        err = f"Bridge error ({type(exc).__name__}): {exc}"
        log_event({"event": "error", "id": call_id,
                   "ts": datetime.now(timezone.utc).isoformat(),
                   "duration": duration, "error": err})
        return [types.TextContent(type="text", text=err)]


async def main():
    if MAC_HOST:
        asyncio.ensure_future(_log_codex_version())  # non-blocking: do not delay stdio ready
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
