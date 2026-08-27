"""Tests for the pure decision logic: argument building, the remote command
wrapper, NDJSON output parsing, and log rotation. The SSH path is
deliberately untested here; it is exercised by a supervised live delegation."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server
from server import (
    _parse_codex_output,
    build_codex_args,
    build_remote_command,
    should_rotate,
)


class FakeProcess:
    def __init__(self, delay=0):
        self.delay = delay
        self.returncode = 0
        self.input = None
        self.terminated = False

    async def communicate(self, input=None):
        self.input = input
        if self.delay:
            await asyncio.sleep(self.delay)
        return b"stdout", b"stderr"

    def terminate(self):
        self.terminated = True

    async def wait(self):
        return self.returncode


def test_run_ssh_command_returns_process_output(monkeypatch):
    proc = FakeProcess()

    async def create_process(*args, **kwargs):
        return proc

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", create_process)
    result = asyncio.run(server._run_ssh_command("remote", "prompt", 1))
    assert result == (0, b"stdout", b"stderr")
    assert proc.input == b"prompt"


def test_run_ssh_command_cleans_up_timeout(monkeypatch):
    proc = FakeProcess(delay=1)

    async def create_process(*args, **kwargs):
        return proc

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", create_process)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(server._run_ssh_command("remote", "prompt", 0.001))
    assert proc.terminated


def test_args_new_session_shape():
    args = build_codex_args("codex", "", "", "")
    assert args[:5] == ["codex", "--ask-for-approval", "never", "exec", "--json"]
    assert args[-1] == '"$TASK"'
    assert "--ask-for-approval" in args and "--json" in args
    assert "--sandbox" not in args and "--model" not in args


def test_args_resume_prompt_is_positional():
    args = build_codex_args("codex", "sess1", "", "")
    assert args[-5:] == ["exec", "resume", "--json", "sess1", '"$TASK"']


def test_args_resume_is_quoted():
    args = build_codex_args("codex", "s; rm -rf /", "", "")
    assert args[-2] == "'s; rm -rf /'"


def test_args_model_and_sandbox():
    args = build_codex_args("codex", "", "gpt-4o", "read-only", "/tmp/my repo")
    m = args.index("--model")
    s = args.index("--sandbox")
    assert args[m + 1] == "gpt-4o"
    assert args[s + 1] == "read-only"
    assert args[args.index("--cd") + 1] == "'/tmp/my repo'"
    assert args.index("--sandbox") < args.index("exec")


def test_remote_command_reads_stdin_and_has_timeout():
    cmd = build_remote_command(["codex", "exec", '"$TASK"'], 600)
    assert cmd.startswith("TASK=$(cat); ")
    assert "590" in cmd
    assert "gtimeout" in cmd


def test_parse_extracts_last_agent_message_and_session():
    raw = "\n".join([
        '{"type": "session_started", "session_id": "abc"}',
        '{"type": "tool_call", "content": "ls -la"}',
        '{"type": "agent_message", "content": "the answer"}',
    ])
    text, sid = _parse_codex_output(raw)
    assert text == "the answer"
    assert sid == "abc"


def test_parse_current_codex_jsonl_shape():
    raw = "\n".join([
        '{"type":"thread.started","thread_id":"thread-123"}',
        '{"type":"item.completed","item":{"type":"command_execution","text":"nope"}}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"fixed"}}',
        '{"type":"turn.completed","usage":{"input_tokens":10}}',
    ])
    text, sid = _parse_codex_output(raw)
    assert text == "fixed"
    assert sid == "thread-123"


def test_parse_skips_tool_events():
    raw = "\n".join([
        '{"type": "agent_message", "content": "real"}',
        '{"type": "exec", "content": "rm file"}',
    ])
    text, _ = _parse_codex_output(raw)
    assert text == "real"


def test_parse_non_json_falls_back_to_raw():
    text, sid = _parse_codex_output("plain crash text")
    assert text == "plain crash text"
    assert sid == ""


def test_should_rotate(tmp_path):
    f = tmp_path / "log"
    assert should_rotate(str(f), 10) is False
    f.write_text("x" * 10)
    assert should_rotate(str(f), 10) is True
    assert should_rotate(str(f), 0) is False
