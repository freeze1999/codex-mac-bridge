# codex-mac-bridge

MCP server that lets an AI agent delegate coding tasks to **OpenAI Codex CLI running on a remote Mac** via SSH over Tailscale. Supports persistent sessions and full Mac filesystem access.

## How it works

```
Agent (server) → ask_codex tool → SSH → Codex CLI (Mac) → response
```

The bridge runs Codex non-interactively (`codex --ask-for-approval never exec`), captures the NDJSON output, parses the response, and returns it with a `session_id` for conversation continuity.

## When this is useful

Use the bridge when your main agent or UI runs on another machine, but the code,
toolchain, and authenticated Codex CLI live on your Mac. One MCP call delegates
the task to that local coding environment and returns structured output with a
reusable session ID.

For longer jobs, pass that ID back as `resume_session_id`. Codex resumes the
existing session instead of starting cold, keeping the earlier conversation,
decisions, and task context while the worktree remains on the Mac.

## Requirements

- [Codex CLI](https://github.com/openai/codex) installed on the Mac
- GNU `timeout` for remote process cleanup, install via `brew install coreutils` on macOS (provides `gtimeout`). Falls back gracefully if unavailable, but remote Codex processes may outlive timed-out SSH connections without it.
- Tailscale running on both machines
- Passwordless SSH from server → Mac (key-based auth)
- Python 3.10+ on the server
- `mcp` Python package

## Setup

### 1. Install Codex CLI on the Mac

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
# or
brew install codex
```

Sign in on the Mac:
```bash
codex  # follow the sign-in prompt
```

Verify non-interactive mode works:
```bash
codex --ask-for-approval never exec --json "what is 2+2"
```

### 2. SSH key auth (server → Mac)

```bash
# On server
ssh-keygen -t ed25519 -f ~/.ssh/mac_bridge
ssh-copy-id -i ~/.ssh/mac_bridge.pub user@<mac-tailscale-ip>
# Test:
ssh -i ~/.ssh/mac_bridge user@<mac-tailscale-ip> "codex --ask-for-approval never exec --json 'hello'"
```

### 3. Configure env vars

```bash
export CODEX_BRIDGE_SSH_HOST="user@100.x.x.x"           # required
export CODEX_BRIDGE_CODEX_BIN="/usr/local/bin/codex"    # if not in PATH
export CODEX_BRIDGE_MODEL="gpt-4o"                      # optional
export CODEX_BRIDGE_TIMEOUT="600"                       # optional, default 600s
export CODEX_BRIDGE_SANDBOX="read-only"                 # optional, see trust boundary
export CODEX_BRIDGE_LOG_MAX_BYTES="10000000"            # optional, log rotation
```

### 4. Connect your agent

Install the bridge on the machine where your main agent runs:

```bash
git clone https://github.com/freeze1999/codex-mac-bridge.git
cd codex-mac-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

BRIDGE_DIR="$PWD"
codex mcp add codex_bridge \
  --env CODEX_BRIDGE_SSH_HOST="user@100.x.x.x" \
  --env CODEX_BRIDGE_CODEX_BIN="/opt/homebrew/bin/codex" \
  --env CODEX_BRIDGE_SANDBOX="workspace-write" \
  -- "$BRIDGE_DIR/.venv/bin/python" "$BRIDGE_DIR/server.py"
```

Run `codex mcp list` to confirm the server is configured, then restart Codex.
In the Codex TUI, `/mcp` shows the active server. The agent discovers the
`ask_codex` tool from the server automatically. For calls longer than Codex's
default MCP tool timeout, add `tool_timeout_sec = 620` under
`[mcp_servers.codex_bridge]` in `~/.codex/config.toml`.

Other MCP hosts can launch the same STDIO server. The equivalent
[Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
is:

```toml
[mcp_servers.codex_bridge]
command = "/absolute/path/to/codex-mac-bridge/.venv/bin/python"
args = ["/absolute/path/to/codex-mac-bridge/server.py"]
tool_timeout_sec = 620

[mcp_servers.codex_bridge.env]
CODEX_BRIDGE_SSH_HOST = "user@100.x.x.x"
CODEX_BRIDGE_CODEX_BIN = "/opt/homebrew/bin/codex"
CODEX_BRIDGE_SANDBOX = "workspace-write"
```

## Tool usage

```python
# Basic task
ask_codex(task="Write a Python script that parses this CSV...")

# Work in a specific repo on the Mac
ask_codex(task="Fix the failing tests", workdir="/Users/me/projects/app")

# With context
ask_codex(
    task="This function is throwing a KeyError, fix it",
    context="def foo(d): return d['missing_key']"
)

# Continue a session
result = ask_codex(task="Write a sorting algorithm")
# result footer contains session_id
ask_codex(task="Now add unit tests for it", resume_session_id="<session_id>")
```

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `CODEX_BRIDGE_SSH_HOST` | _(required)_ | SSH target, e.g. `user@100.x.x.x` |
| `CODEX_BRIDGE_CODEX_BIN` | `codex` | Path to codex binary on the Mac |
| `CODEX_BRIDGE_TIMEOUT` | `600` | Seconds before timeout (wall-clock, never pauses) |
| `CODEX_BRIDGE_MODEL` | _(default)_ | Model override, e.g. `gpt-4o` |
| `CODEX_BRIDGE_LOG_PATH` | `./bridge.log` | Path for delegation audit log |

## Session chaining

Every response includes a `session_id`. Pass it back as `resume_session_id` to continue the same conversation, Codex remembers all prior context and decisions.

**Rule: always chain session_ids for related tasks.** Without `resume_session_id`, each call is a cold start.

```python
r1 = ask_codex(task="Build the auth module")
# r1 footer contains: session_id = "abc123"

r2 = ask_codex(task="Add rate limiting to the auth module", resume_session_id="abc123")
r3 = ask_codex(task="Write tests for all of it", resume_session_id="abc123")
```

## Long tasks: the tmux blocking loop

The MCP tool has a hard wall-clock timeout (default 600s). For tasks that may run longer, project scaffolding, multi-file builds, long refactors, use this tmux pattern instead. It blocks until Codex finishes and returns the full result + `session_id`.

Unlike the Claude bridge, no confirmation-prompt handling is needed, `--ask-for-approval never` already makes Codex fully non-interactive.

```bash
#!/usr/bin/env bash
MAC="user@<mac-tailscale-ip>"
TMUX="/opt/homebrew/bin/tmux"
CODEX="codex"             # or full path e.g. /usr/local/bin/codex
SESSION="agent-work"
MAX_WAIT=3600             # 60 min ceiling
POLL=45

# 1. write task to file (avoids SSH quoting hell)
scp /tmp/task.md "$MAC:~/task.md"

# 2. start fresh tmux session
ssh "$MAC" "$TMUX kill-session -t $SESSION 2>/dev/null; \
  $TMUX new-session -d -s $SESSION -x 220 -y 50"

# 3. launch codex with task file
ssh "$MAC" "$TMUX send-keys -t $SESSION \
  '$CODEX --ask-for-approval never exec --json \
  "Read ~/task.md and execute every step." 2>&1 | tee ~/task_output.ndjson' Enter"

# 4. blocking poll -- exit when shell prompt returns (codex finished)
elapsed=0
while [ $elapsed -lt $MAX_WAIT ]; do
  sleep $POLL
  elapsed=$((elapsed + POLL))
  pane=$(ssh "$MAC" "$TMUX capture-pane -t $SESSION -p 2>/dev/null | tail -5")
  echo "$pane" | grep -qE '^\s*(%|\$|>)\s*$' && break
done

# 5. extract session_id from NDJSON output
result=$(ssh "$MAC" "cat ~/task_output.ndjson 2>/dev/null")
session_id=$(echo "$result" | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        e = json.loads(line)
        sid = (e.get('session_id') or e.get('sessionId')
               or e.get('thread_id') or e.get('threadId'))
        if sid: print(sid); break
    except: pass
" 2>/dev/null)

# 6. cleanup
ssh "$MAC" "$TMUX kill-session -t $SESSION 2>/dev/null; rm -f ~/task.md ~/task_output.ndjson"
echo "done in ${elapsed}s | session_id: $session_id"
```

**When to use tmux vs MCP tool:**

| Task type | Use |
|-----------|-----|
| Quick question, code review, research | MCP tool |
| Multi-step work with session chaining | MCP tool + `resume_session_id` |
| Project scaffolding / multi-file builds | tmux blocking loop |
| Unknown duration / likely >10 min | tmux blocking loop |

**If unsure → tmux.** A 30s task in tmux costs nothing extra. A 12-min task in the MCP tool gets killed at 10 min.


## Monitoring

```bash
python3 monitor.py          # watch delegations live
python3 monitor.py /path/to/bridge.log
```

## Security notes

- `bridge.log` contains full task/response history, gitignored by default, keep it local
- `--ask-for-approval never` is passed so Codex runs autonomously; only use on a trusted Mac
- Never commit `.env` or SSH private keys

## Known limitations

- Session recovery depends on the saved Codex session remaining on the Mac
- Async server, concurrent tool calls are supported but each delegation opens its own SSH connection

## Trust boundary

The bridge always runs `codex --ask-for-approval never exec` (exec mode is
non-interactive), so the sandbox is the safety boundary: any agent that can
call this MCP tool can run whatever the sandbox mode allows on the Mac. Set
`CODEX_BRIDGE_SANDBOX=read-only` unless the delegated work needs writes;
unset, the Codex CLI's own default applies.

The audit log (`bridge.log`) records the full task, context, and response
text of every delegation. Treat it as sensitive and leave it out of any
repo (it is gitignored here).

## Status

The bridge supports remote working directories, structured Codex output,
session recovery, concurrent calls, audit logging, and log rotation.

Argument building, NDJSON parsing, and log rotation are unit-tested. New and
resumed Codex sessions have also been verified with live CLI runs.
