#!/usr/bin/env python3
"""
Bridge Monitor, live feed of Codex delegations.
Usage: python3 monitor.py [path/to/bridge.log]
"""

import json
import os
import sys
import time
from datetime import datetime

LOG_FILE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "bridge.log")

RESET = "\033[0m"; BOLD  = "\033[1m"; DIM   = "\033[2m"
CYAN  = "\033[96m"; YELLOW = "\033[93m"; GREEN = "\033[92m"
RED   = "\033[91m"; MAGENTA = "\033[95m"
WIDTH = 70


def fmt_ts(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M:%S")
    except Exception:
        return iso[:19]


def wrap(text, indent=4, width=WIDTH - 6):
    lines = []
    for raw in text.splitlines():
        while len(raw) > width:
            lines.append(" " * indent + raw[:width])
            raw = raw[width:]
        lines.append(" " * indent + raw)
    return "\n".join(lines)


def divider(char="─", color=DIM):
    return color + char * WIDTH + RESET


def print_header():
    print("\n" * 2)
    print(BOLD + CYAN + "  Codex Bridge Monitor ,  delegation feed" + RESET + DIM + "  (live)" + RESET)
    print(divider("═", CYAN))
    print(DIM + f"  watching: {LOG_FILE}" + RESET)
    print(divider("─"))
    print()


def render_start(ev, pending):
    call_id = ev.get("id", "?")
    pending[call_id] = ev
    print(divider("═", YELLOW))
    print(BOLD + YELLOW + "  TASK" + RESET + DIM + f"  •  {fmt_ts(ev.get('ts', ''))}" + RESET)
    print(divider("─"))
    print()
    print(wrap(ev.get("task", "")))
    ctx = ev.get("context")
    if ctx:
        lines = ctx.splitlines()
        print(); print(DIM + "  context:" + RESET)
        for l in lines[:3]: print(DIM + "    " + l[:WIDTH - 6] + RESET)
        if len(lines) > 3: print(DIM + f"    ... +{len(lines) - 3} more lines" + RESET)
    print()
    print(MAGENTA + "  running..." + RESET)
    print()
    sys.stdout.flush()


def render_done(ev, pending):
    pending.pop(ev.get("id", "?"), None)
    sid = ev.get("session_id", "")
    print(GREEN + BOLD + "  RESPONSE" + RESET + DIM +
          f"  •  {fmt_ts(ev.get('ts', ''))}  •  {ev.get('duration', '?')}s" + RESET)
    print(divider("─"))
    print()
    if sid: print(DIM + f"  session: {sid[:16]}..." + RESET)
    lines = ev.get("response", "").splitlines()
    print(wrap("\n".join(lines[:40])))
    if len(lines) > 40: print(DIM + f"    ... +{len(lines) - 40} more lines" + RESET)
    print()
    sys.stdout.flush()


def render_error(ev, pending):
    pending.pop(ev.get("id", "?"), None)
    print(RED + BOLD + "  ERROR" + RESET + DIM +
          f"  •  {fmt_ts(ev.get('ts', ''))}  •  {ev.get('duration', '?')}s" + RESET)
    print(divider("─"))
    print(); print(wrap(ev.get("error", "unknown"))); print()
    sys.stdout.flush()


def render_timeout(ev, pending):
    pending.pop(ev.get("id", "?"), None)
    print(RED + BOLD + "  TIMEOUT" + RESET + DIM +
          f"  •  {fmt_ts(ev.get('ts', ''))}  •  {ev.get('duration', '?')}s" + RESET)
    print(divider("─"))
    print()
    sys.stdout.flush()


def tail_log():
    pending = {}
    print_header()
    try:
        f = open(LOG_FILE, "r"); f.seek(0, 2)
        print(DIM + "  waiting for delegations..." + RESET); print()
    except FileNotFoundError:
        f = None
        print(DIM + "  log file not found yet, waiting..." + RESET); print()

    while True:
        if f is None:
            if os.path.exists(LOG_FILE):
                f = open(LOG_FILE, "r"); f.seek(0, 2)
            else:
                time.sleep(1); continue
        line = f.readline()
        if not line:
            time.sleep(0.3); continue
        line = line.strip()
        if not line: continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("event")
        if t == "start":     render_start(ev, pending)
        elif t == "done":    render_done(ev, pending)
        elif t == "error":   render_error(ev, pending)
        elif t == "timeout": render_timeout(ev, pending)


if __name__ == "__main__":
    try:
        tail_log()
    except KeyboardInterrupt:
        print(); print(DIM + "\n  monitor stopped." + RESET); print()
