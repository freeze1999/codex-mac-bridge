# PORTING

This repo has a sibling: claude-mac-bridge. The two servers share most of server.py by
design (duplication over a shared dependency, the repos stay standalone).

When you change server.py here, diff against the sibling and port what
applies. Intentional differences, keep this list current:

- tool name and env prefix (ask_codex / CODEX_BRIDGE_* vs ask_claude / CLAUDE_BRIDGE_*)
- output parsing: claude parses one JSON object; codex parses NDJSON events
  (_parse_codex_output, _TOOL_TYPES)
- safety knob: claude has CLAUDE_BRIDGE_SKIP_PERMISSIONS; codex has
  CODEX_BRIDGE_SANDBOX (approval is always "never" in exec mode)
- codex has a model override (CODEX_BRIDGE_MODEL); claude does not need one
- resume: claude uses --resume ID; codex uses exec resume ID with the
  prompt positional

Shared and load-bearing in both: the '"$TASK"' stdin trick (see the comment
on the arg builder), the remote timeout wrapper, log rotation, and the
event log schema.
