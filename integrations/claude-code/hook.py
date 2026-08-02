#!/usr/bin/env python3
"""Evomem Claude Code hook — record Claude Code activity into evomem.

Replaces the agentmemory plugin hooks: every Claude Code event
(SessionStart, UserPromptSubmit, PostToolUse, ...) is appended as a JSONL
record under ``data/sessions/`` (relative to this script), then a separate
sync job (see sync_to_evomem.py) converts the records into markdown notes
inside the evomem knowledge root and triggers ``evomem sync``.

Why JSONL instead of writing markdown directly? Hook execution must never
block Claude Code: appending a line is O(1) and cannot fail the session.
The heavy work (markdown conversion + reindex) happens in a background
cron job.

Usage in ~/.claude/settings.json:

    {
      "hooks": {
        "SessionStart":     [{ "hooks": [{ "type": "command", "command": "python3 /path/to/hook.py session-start" }] }],
        "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python3 /path/to/hook.py prompt-submit" }] }],
        "PostToolUse":      [{ "hooks": [{ "type": "command", "command": "python3 /path/to/hook.py post-tool-use" }] }]
      }
    }

Environment:
  EVOMEM_DATA_DIR  where session JSONL records are stored (default: ./data next to this script)
"""

import json
import os
import sys
from datetime import datetime, timezone

DATA_DIR = os.environ.get("EVOMEM_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
SESSION_DIR = os.path.join(DATA_DIR, "sessions")
os.makedirs(SESSION_DIR, exist_ok=True)


def _session_file(session_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:60]
    return os.path.join(SESSION_DIR, f"{safe}.jsonl")


def append_record(session_id: str, record: dict) -> None:
    if not session_id or session_id == "unknown":
        return
    path = _session_file(session_id)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass  # never break Claude Code on hook failure


def read_stdin() -> dict:
    data = sys.stdin.read()
    try:
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    payload = read_stdin()
    session_id = payload.get("session_id") or payload.get("sessionId") or "unknown"

    if event == "session-start":
        append_record(session_id, {
            "type": "session_start", "ts": now(),
            "cwd": payload.get("cwd") or os.getcwd(),
            "source": "claude-code",
        })
        return

    if event == "prompt-submit":
        prompt = payload.get("prompt") or payload.get("userPrompt") or ""
        if not prompt:
            return
        append_record(session_id, {"type": "prompt", "ts": now(), "content": prompt[:4000]})
        return

    if event in ("post-tool-use", "post-tool-failure"):
        tool = payload.get("tool_name") or payload.get("tool") or "unknown"
        result = payload.get("tool_response") or payload.get("toolResult") or ""
        if isinstance(result, (dict, list)):
            result = json.dumps(result)[:4000]
        append_record(session_id, {
            "type": "tool_result" if event == "post-tool-use" else "tool_failure",
            "ts": now(), "tool": tool, "content": str(result)[:4000],
        })
        return

    if event == "session-end":
        append_record(session_id, {"type": "session_end", "ts": now()})
        return

    # Unhandled events: record quietly for debugging.
    append_record(session_id, {
        "type": event, "ts": now(), "content": json.dumps(payload)[:2000],
    })


if __name__ == "__main__":
    main()
