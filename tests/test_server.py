"""Tests for the pure decision logic: argument building, the remote command
wrapper, NDJSON output parsing, and log rotation. The SSH path is
deliberately untested here; it is exercised by a supervised live delegation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import (
    _parse_codex_output,
    build_codex_args,
    build_remote_command,
    should_rotate,
)


def test_args_new_session_shape():
    args = build_codex_args("codex", "", "", "")
    assert args[:2] == ["codex", "exec"]
    assert args[-1] == '"$TASK"'
    assert "--ask-for-approval" in args and "--json" in args
    assert "--sandbox" not in args and "--model" not in args


def test_args_resume_prompt_is_positional():
    args = build_codex_args("codex", "sess1", "", "")
    assert args[:5] == ["codex", "exec", "resume", "sess1", '"$TASK"']


def test_args_resume_is_quoted():
    args = build_codex_args("codex", "s; rm -rf /", "", "")
    assert args[3] == "'s; rm -rf /'"


def test_args_model_and_sandbox():
    args = build_codex_args("codex", "", "gpt-4o", "read-only")
    m = args.index("--model")
    s = args.index("--sandbox")
    assert args[m + 1] == "gpt-4o"
    assert args[s + 1] == "read-only"


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
