#!/usr/bin/env python3
"""Convert Claude Code hook records (JSONL) into evomem markdown notes.

Reads every ``*.jsonl`` session file under ``data/sessions/`` (written by
hook.py), writes one markdown note per session into ``<knowledge-root>/claude/``,
then triggers ``evomem sync`` through the server API so the new notes are
indexed immediately.

Designed for a cron job (e.g. every 5 minutes): it only processes JSONL files
modified since the last run (tracked in ``data/.last_sync``).

Usage:
    EVOMEM_URL=http://localhost:7700 EVOMEM_KB=/path/to/knowledge-root \
        python3 sync_to_evomem.py

Environment:
  EVOMEM_URL       evomem server base URL (default http://localhost:7700)
  EVOMEM_KB        knowledge root that the evomem server indexes (default: ../../evomem-kb relative to this script)
  EVOMEM_DATA_DIR  where session JSONL records live (default: ./data next to this script)
"""

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EVOMEM_URL = os.environ.get("EVOMEM_URL", "http://localhost:7700")
_THIS_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = Path(os.environ.get("EVOMEM_DATA_DIR", _THIS_DIR / "data"))
# Sessions from the local host land in data/sessions/; sessions pulled from
# a remote host can be rsynced into data/sessions-<host>/ and will be labeled
# with that host's name in the generated notes.
SESSION_DIRS = [DATA_DIR / "sessions"] + sorted(
    p for p in DATA_DIR.glob("sessions-*") if p.is_dir()
)
OUT = Path(os.environ.get("EVOMEM_KB", _THIS_DIR / ".." / ".." / "evomem-kb")) / "claude"
OUT.mkdir(parents=True, exist_ok=True)

STAMP = DATA_DIR / ".last_sync"


def evomem_sync() -> None:
    try:
        req = urllib.request.Request(
            f"{EVOMEM_URL}/api/sync", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except Exception as e:
        print(f"sync api error: {e}")


def slugify(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:60]


def main() -> None:
    last = 0.0
    if STAMP.exists():
        last = STAMP.stat().st_mtime

    changed = 0
    for sdir in SESSION_DIRS:
        if not sdir.is_dir():
            continue
        host_label = sdir.name[9:] if sdir.name.startswith("sessions-") else "local"
        for f in sorted(sdir.glob("*.jsonl")):
            if f.stat().st_mtime <= last:
                continue
            session_id = f.stem
            try:
                records = [
                    json.loads(l)
                    for l in f.read_text(encoding="utf-8").splitlines()
                    if l.strip()
                ]
            except Exception as e:
                print(f"  parse {f.name}: {e}")
                continue
            if not records:
                continue

            lines = [
                "---",
                f'title: "Claude Code session {host_label} {session_id[:20]}"',
                "type: session",
                f"created: {records[0].get('ts', '')[:10]}",
                f"aliases: [claude-{host_label}-{session_id[:20]}]",
                "---",
                "",
                f"# Claude Code session {session_id} ({host_label})",
                "",
            ]
            for r in records:
                ts = (r.get("ts") or "")[:19]
                kind = r.get("type", "")
                if kind == "session_start":
                    lines.append(f"*started {ts} (cwd: {r.get('cwd', '')})*")
                elif kind == "prompt":
                    lines.append(f"\n## {ts} — user prompt\n\n{r.get('content', '')}\n")
                elif kind == "tool_result":
                    lines.append(f"\n### {ts} — tool: {r.get('tool', '')}\n\n{r.get('content', '')}\n")
                elif kind == "tool_failure":
                    lines.append(f"\n### {ts} — ⚠ tool FAILED: {r.get('tool', '')}\n\n{r.get('content', '')}\n")
                elif kind == "session_end":
                    lines.append(f"\n*ended {ts}*\n")
                else:
                    lines.append(f"\n### {ts} — {kind}\n\n{r.get('content', '')}\n")

            out_file = OUT / f"{host_label}_{slugify(session_id)}.md"
            out_file.write_text("\n".join(lines), encoding="utf-8")
            changed += 1

    if changed:
        evomem_sync()
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text(datetime.now(timezone.utc).isoformat())
    print(f"synced {changed} claude session note(s) into evomem")


if __name__ == "__main__":
    main()
