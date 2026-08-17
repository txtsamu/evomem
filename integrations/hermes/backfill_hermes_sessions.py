#!/usr/bin/env python3
"""Backfill ALL historical Hermes sessions into the evomem knowledge base.

Hermes stores its session list and transcripts in `~/.hermes/state.db`
(`sessions` + `messages` tables), with a few older transcripts only kept
as JSONL files in `~/.hermes/sessions/` (before the DB was introduced).

This script walks every Hermes session (excluding `claude_code` imports —
those are already captured by the Claude Code hooks — and archived rows),
renders each as an evomem markdown note in the same format the realtime
Hermes plugin produces, writes it to
`<EVOMEM_KB>/sessions/<created-date>_<session-id>.md` and triggers
`evomem sync` via the server API so the historical sessions become
searchable.

Idempotent: sessions whose markdown note already exists (realtime capture,
a previous backfill run, or the agentmemory export) are skipped.

Env vars:
  HERMES_DB             path to Hermes state.db (default ~/.hermes/state.db)
  HERMES_SESSIONS_DIR   dir with legacy <session-id>.jsonl transcripts
                        (default ~/.hermes/sessions)
  REALTIME_JSONL_DIR    dir the realtime Hermes plugin writes JSONL events
                        to; sessions present there are skipped because the
                        plugin owns them (default <script_dir>/data/sessions-hermes)
  EVOMEM_KB             knowledge base root (default <script_dir>/../../evomem-kb)
  EVOMEM_URL            evomem server URL (default http://localhost:7700)
  USER_MAX / ASSISTANT_MAX  per-turn truncation limits matching the realtime
                        plugin (default 500 / 2000)

Usage:
  python3 backfill_hermes_sessions.py            # backfill + sync
  python3 backfill_hermes_sessions.py --dry-run # preview counts, write nothing
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EVOMEM_URL = os.environ.get("EVOMEM_URL", "http://localhost:7700")
HERMES_DB = os.environ.get("HERMES_DB", str(Path.home() / ".hermes" / "state.db"))
HERMES_SESSIONS_DIR = os.environ.get(
    "HERMES_SESSIONS_DIR", str(Path.home() / ".hermes" / "sessions"))
_THIS_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
REALTIME_DIR = Path(os.environ.get(
    "REALTIME_JSONL_DIR", _THIS_DIR / "data" / "sessions-hermes"))
KB = Path(os.environ.get("EVOMEM_KB", (_THIS_DIR / ".." / ".." / "evomem-kb").resolve()))
OUT_DIR = KB / "sessions"
USER_MAX = int(os.environ.get("USER_MAX", "500"))
ASSISTANT_MAX = int(os.environ.get("ASSISTANT_MAX", "2000"))
DRY_RUN = "--dry-run" in sys.argv


def slugify(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:60]


def iso(ts) -> str:
    """Accept epoch float or ISO string -> UTC ISO string."""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return ts or ""


def evomem_sync() -> None:
    try:
        req = urllib.request.Request(f"{EVOMEM_URL}/api/sync", data=b"{}",
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
    except Exception as e:
        print(f"sync api error: {e}")


def build_records(messages, start_ts, end_ts, label) -> list:
    """Turn a flat message list into plugin-style JSONL records."""
    turns = []
    cur = None
    for m in messages:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role == "user" and content:
            cur = {"user": content[:USER_MAX], "ts": m.get("ts")}
            turns.append(cur)
        elif role == "assistant" and content:
            if cur is not None and "assistant" not in cur:
                cur["assistant"] = content[:ASSISTANT_MAX]
            else:
                cur = {"assistant": content[:ASSISTANT_MAX], "ts": m.get("ts")}
                turns.append(cur)

    records = [{"type": "session_start", "ts": iso(start_ts), "cwd": label,
                "source": "hermes"}]
    for t in turns:
        parts = []
        if t.get("user"):
            parts.append("USER: " + t["user"])
        if t.get("assistant"):
            parts.append("ASSISTANT: " + t["assistant"])
        if parts:
            records.append({"type": "conversation", "ts": iso(t.get("ts")),
                            "content": "\n".join(parts)})
    if end_ts:
        records.append({"type": "session_end", "ts": iso(end_ts)})
    return records


def render_hermes(records) -> str:
    first = records[0]
    sid = first.get("session_id", "")
    cwd = (first.get("cwd", "") or "").replace('"', "'")
    obs = sum(1 for r in records if r.get("type") in ("conversation", "summary", "memory_write"))
    lines = [
        "---",
        f'title: "{cwd}"',
        "type: session",
        f"created: {first.get('ts', '')[:10]}",
        f"aliases: [{sid}]",
        "---",
        "",
        f"# {cwd}",
        "",
        f"- **session id**: `{sid}`",
        f"- **cwd**: `{cwd}`",
        f"- **project**: `{cwd}`",
        f"- **status**: completed",
        f"- **observations**: {obs}",
        "",
        "## Observations",
        "",
    ]
    for r in records:
        ts = (r.get("ts") or "")[:19]
        kind = r.get("type", "")
        if kind == "session_start":
            lines.append(f"### {ts} — session_start\n\n*started {ts} (cwd: {r.get('cwd', '')})*\n")
        elif kind == "session_end":
            lines.append(f"\n### {ts} — session_end\n\n*ended {ts}*\n")
        elif kind in ("conversation", "summary", "memory_write"):
            lines.append(f"\n### {ts} — {kind}\n\n{r.get('content', '')}\n")
        else:
            lines.append(f"\n### {ts} — {kind}\n\n{r.get('content', '')}\n")
    return "\n".join(lines)


def db_messages(conn, session_id):
    rows = conn.execute(
        "SELECT role, content, timestamp FROM messages "
        "WHERE session_id=? AND active=1 AND role IN ('user','assistant') "
        "ORDER BY timestamp, id", (session_id,)).fetchall()
    return [{"role": r[0], "content": r[1] or "", "ts": r[2]} for r in rows]


def jsonl_messages(path):
    msgs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        role = d.get("role")
        if role in ("user", "assistant") and d.get("content"):
            msgs.append({"role": role, "content": d["content"],
                         "ts": d.get("timestamp")})
    return msgs


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    existing = {f.stem for f in OUT_DIR.glob("*.md")}
    realtime_ids = set()
    if REALTIME_DIR.is_dir():
        realtime_ids = {f.stem for f in REALTIME_DIR.glob("*.jsonl")}

    # 1) sessions from state.db (the authoritative list)
    conn = sqlite3.connect(HERMES_DB)
    db_rows = conn.execute(
        "SELECT id, started_at, ended_at, title, cwd, source FROM sessions "
        "WHERE archived=0 AND source != 'claude_code' ORDER BY started_at").fetchall()
    db_ids = {r[0] for r in db_rows}

    # 2) legacy JSONL transcripts not in the DB (pre-DB era)
    legacy = []
    sdir = Path(HERMES_SESSIONS_DIR)
    if sdir.is_dir():
        legacy = [f for f in sorted(sdir.glob("*.jsonl")) if f.stem not in db_ids]

    written = skipped_existing = skipped_live = skipped_empty = 0
    for rid, started_at, ended_at, title, cwd, source in db_rows:
        sid = rid
        if sid in realtime_ids:
            skipped_live += 1
            continue
        if f"{iso(started_at)[:10]}_{slugify(sid)}" in existing:
            skipped_existing += 1
            continue
        msgs = db_messages(conn, sid)
        if not msgs:
            skipped_empty += 1
            continue
        label = (cwd or title or source or "hermes").replace('"', "'")
        records = build_records(msgs, started_at, ended_at, label)
        for r in records:
            r.setdefault("session_id", sid)
        if not DRY_RUN:
            (OUT_DIR / f"{iso(started_at)[:10]}_{slugify(sid)}.md").write_text(
                render_hermes(records), encoding="utf-8")
        written += 1

    for f in legacy:
        sid = f.stem
        created = f"{sid[:4]}-{sid[4:6]}-{sid[6:8]}"
        if sid in realtime_ids:
            skipped_live += 1
            continue
        if f"{created}_{slugify(sid)}" in existing:
            skipped_existing += 1
            continue
        msgs = jsonl_messages(f)
        if not msgs:
            skipped_empty += 1
            continue
        start_ts = f"{created}T{sid[9:11]}:{sid[11:13]}:{sid[13:15]}"
        end_ts = msgs[-1].get("ts") or start_ts
        records = build_records(msgs, start_ts, end_ts, "hermes")
        for r in records:
            r.setdefault("session_id", sid)
        if not DRY_RUN:
            (OUT_DIR / f"{created}_{slugify(sid)}.md").write_text(
                render_hermes(records), encoding="utf-8")
        written += 1

    print(f"backfill: {written} written, {skipped_existing} already in KB, "
          f"{skipped_live} realtime-managed, {skipped_empty} empty")

    if written and not DRY_RUN:
        evomem_sync()
        print("evomem sync triggered")


if __name__ == "__main__":
    main()
