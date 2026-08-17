#!/usr/bin/env python3
"""Backfill historical agent sessions into the evomem knowledge base.

Two modes:

1. Hermes (default) — walks every Hermes session in ~/.hermes/state.db
   (`sessions` + `messages` tables), plus legacy JSONL transcripts in
   ~/.hermes/sessions/ that predate the DB, and renders each as a markdown
   note in `<EVOMEM_KB>/sessions/` (same format as the realtime plugin).
   Excludes `claude_code` imports (the Claude mode owns those) and archived
   rows.

2. Claude Code (--claude-dir) — parses native Claude Code transcripts
   (~/.claude/projects/<cwd>/<session-id>.jsonl) with the same parser the
   Hermes session importer uses, and renders each as a markdown note in
   `<EVOMEM_KB>/claude/` with a host label (same format as the Claude Code
   hooks produce). Pass --claude-dir + --host once per host.

Both modes are idempotent: sessions whose markdown note already exists
(realtime capture, hooks, a previous run) are skipped.

Env vars:
  HERMES_DB             path to Hermes state.db (default ~/.hermes/state.db)
  HERMES_SESSIONS_DIR   dir with legacy <session-id>.jsonl transcripts
                        (default ~/.hermes/sessions)
  REALTIME_JSONL_DIR    dir the realtime Hermes plugin writes JSONL events
                        to; sessions present there are skipped (default
                        <script_dir>/data/sessions-hermes)
  EVOMEM_KB             knowledge base root (default <script_dir>/../../evomem-kb)
  EVOMEM_URL            evomem server URL (default http://localhost:7700)
  USER_MAX / ASSISTANT_MAX  per-turn truncation limits for the Hermes mode,
                        matching the realtime plugin (default 500 / 2000)

Usage:
  python3 backfill_hermes_sessions.py                                  # Hermes sessions + sync
  python3 backfill_hermes_sessions.py --claude-dir ~/.claude/projects --host homelab
  python3 backfill_hermes_sessions.py --claude-dir /tmp/fedora-projects --host fedora
  python3 backfill_hermes_sessions.py --dry-run                        # preview counts, write nothing
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
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
USER_MAX = int(os.environ.get("USER_MAX", "500"))
ASSISTANT_MAX = int(os.environ.get("ASSISTANT_MAX", "2000"))


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


# --------------------------------------------------------------------------
# Hermes mode
# --------------------------------------------------------------------------

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


def backfill_hermes(conn, legacy_files, out_dir, existing, realtime_ids, dry):
    written = skipped_existing = skipped_live = skipped_empty = 0

    db_rows = conn.execute(
        "SELECT id, started_at, ended_at, title, cwd, source FROM sessions "
        "WHERE archived=0 AND source != 'claude_code' ORDER BY started_at").fetchall()

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
        if not dry:
            (out_dir / f"{iso(started_at)[:10]}_{slugify(sid)}.md").write_text(
                render_hermes(records), encoding="utf-8")
        written += 1

    for f in legacy_files:
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
        if not dry:
            (out_dir / f"{created}_{slugify(sid)}.md").write_text(
                render_hermes(records), encoding="utf-8")
        written += 1

    return written, skipped_existing, skipped_live, skipped_empty


# --------------------------------------------------------------------------
# Claude Code mode
# --------------------------------------------------------------------------

def parse_claude_jsonl(path):
    """Parse a Claude Code session JSONL into Hermes-shaped messages.

    Returns (messages, title, ended, started_ts). Lines without a
    user/assistant message (mode, last-prompt, system, etc.) are skipped.
    Subagent/child files are handled by the caller.
    """
    messages = []
    title = None
    ended = False
    started_ts = None

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn tail write — Claude still appending

            ltype = rec.get("type")
            ts = rec.get("timestamp")

            if ltype == "summary":
                ended = True
                if not title and rec.get("summary"):
                    title = str(rec["summary"])[:120]
                continue
            if ltype == "ai-title":
                if not title and rec.get("aiTitle"):
                    title = str(rec["aiTitle"])[:120]
                continue
            if ltype not in ("user", "assistant"):
                continue

            msg = rec.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                text = content
                if not text.strip():
                    continue
                if started_ts is None and ts:
                    started_ts = ts
                messages.append({
                    "role": "user" if ltype == "user" else "assistant",
                    "content": text,
                    "timestamp": ts,
                })
                continue

            if not isinstance(content, list):
                continue

            if started_ts is None and ts:
                started_ts = ts

            if ltype == "user":
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_result":
                        result = _block_text(block.get("content"))
                        if not result:
                            continue
                        messages.append({
                            "role": "tool",
                            "content": result,
                            "tool_call_id": block.get("tool_use_id"),
                            "timestamp": ts,
                        })
                    elif btype == "text":
                        text = block.get("text") or ""
                        if text.strip():
                            messages.append({
                                "role": "user",
                                "content": text,
                                "timestamp": ts,
                            })
            else:  # assistant
                tool_calls = []
                text_parts = []
                reasoning = None
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text") or "")
                    elif btype == "thinking":
                        thinking = block.get("thinking")
                        if thinking:
                            reasoning = reasoning or thinking
                    elif btype == "tool_use":
                        tool_calls.append({
                            "id": block.get("id"),
                            "type": "function",
                            "function": {
                                "name": block.get("name"),
                                "arguments": json.dumps(block.get("input") or {}),
                            },
                        })
                if tool_calls:
                    messages.append({
                        "role": "assistant",
                        "content": "".join(text_parts),
                        "tool_calls": tool_calls,
                        "reasoning": reasoning,
                        "timestamp": ts,
                    })
                elif text_parts and "".join(text_parts).strip():
                    messages.append({
                        "role": "assistant",
                        "content": "".join(text_parts),
                        "reasoning": reasoning,
                        "timestamp": ts,
                    })

    return messages, title, ended, started_ts


def _block_text(content):
    """Flatten a Claude block content (str, list of parts, or None) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(p.get("text") or "")
                elif p.get("type") == "image":
                    continue
                else:
                    parts.append(p.get("text") or "")
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content)


def build_claude_records(msgs, started_ts, ended, cwd, sid):
    tool_names = {}
    for m in msgs:
        for tc in (m.get("tool_calls") or []):
            tc_id = tc.get("id")
            if tc_id:
                tool_names[tc_id] = (tc.get("function") or {}).get("name") or "tool"
    records = [{"type": "session_start", "ts": started_ts, "cwd": cwd,
                "session_id": sid}]
    for m in msgs:
        role = m.get("role")
        ts = m.get("timestamp") or started_ts
        if role == "user":
            records.append({"type": "prompt", "ts": ts, "content": m.get("content", "")})
        elif role == "assistant":
            c = (m.get("content") or "").strip()
            if c:
                records.append({"type": "assistant", "ts": ts, "content": c})
        elif role == "tool":
            records.append({"type": "tool_result", "ts": ts,
                            "tool": tool_names.get(m.get("tool_call_id"), "tool"),
                            "content": m.get("content", "")})
    if ended:
        records.append({"type": "session_end",
                        "ts": msgs[-1].get("timestamp") or started_ts})
    return records


def render_claude(records, host_label):
    sid = records[0].get("session_id", "")
    sid20 = sid[:20]
    lines = [
        "---",
        f'title: "Claude Code session {host_label} {sid20}"',
        "type: session",
        f"created: {records[0].get('ts', '')[:10]}",
        f"aliases: [claude-{host_label}-{sid20}]",
        "---",
        "",
        f"# Claude Code session {sid} ({host_label})",
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
    return "\n".join(lines)


def backfill_claude(root, host, out_dir, existing, dry):
    files = sorted(p for p in root.rglob("*.jsonl")
                   if not p.name.startswith(".") and "subagents" not in p.parts)
    written = skipped_existing = skipped_empty = 0
    for f in files:
        sid = f.stem
        stem = f"{host}_{slugify(sid)}"
        if stem in existing:
            skipped_existing += 1
            continue
        msgs, _title, ended, started_ts = parse_claude_jsonl(f)
        if not msgs:
            skipped_empty += 1
            continue
        pdir = f.parent.name
        cwd = ("/" + pdir.lstrip("-").replace("-", "/")) if pdir.startswith("-") else pdir
        records = build_claude_records(msgs, started_ts, ended, cwd, sid)
        if not dry:
            (out_dir / f"{stem}.md").write_text(render_claude(records, host),
                                                encoding="utf-8")
        written += 1
    return written, skipped_existing, skipped_empty


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="preview counts, write nothing")
    ap.add_argument("--claude-dir", action="append", default=[],
                    help="native Claude transcripts dir (~/.claude/projects); "
                         "repeat once per host, paired with --host")
    ap.add_argument("--host", action="append", default=[],
                    help="host label matching each --claude-dir (homelab, fedora, ...)")
    args = ap.parse_args()

    if args.claude_dir and len(args.claude_dir) != len(args.host):
        print("ERROR: --claude-dir and --host must be given the same number of times")
        sys.exit(2)

    changed = 0

    if args.claude_dir:
        out_dir = KB / "claude"
        out_dir.mkdir(parents=True, exist_ok=True)
        existing = {f.stem for f in out_dir.glob("*.md")}
        for root, host in zip(args.claude_dir, args.host):
            rpath = Path(root).expanduser()
            if not rpath.is_dir():
                print(f"ERROR: {rpath} is not a directory")
                sys.exit(2)
            written, skip_ex, skip_empty = backfill_claude(
                rpath, host, out_dir, existing, args.dry_run)
            existing |= {f"{host}_{slugify(f.stem)}" for f in rpath.rglob("*.jsonl")
                         if "subagents" not in f.parts}
            print(f"claude[{host}]: {written} written, {skip_ex} already in KB, "
                  f"{skip_empty} empty")
            changed += written
    else:
        out_dir = KB / "sessions"
        out_dir.mkdir(parents=True, exist_ok=True)
        existing = {f.stem for f in out_dir.glob("*.md")}
        realtime_ids = set()
        if REALTIME_DIR.is_dir():
            realtime_ids = {f.stem for f in REALTIME_DIR.glob("*.jsonl")}

        conn = sqlite3.connect(HERMES_DB)
        db_ids = {r[0] for r in conn.execute("SELECT id FROM sessions")}

        legacy = []
        sdir = Path(HERMES_SESSIONS_DIR)
        if sdir.is_dir():
            legacy = [f for f in sorted(sdir.glob("*.jsonl")) if f.stem not in db_ids]

        written, skip_ex, skip_live, skip_empty = backfill_hermes(
            conn, legacy, out_dir, existing, realtime_ids, args.dry_run)
        conn.close()
        print(f"backfill: {written} written, {skip_ex} already in KB, "
              f"{skip_live} realtime-managed, {skip_empty} empty")
        changed += written

    if changed and not args.dry_run:
        evomem_sync()
        print("evomem sync triggered")


if __name__ == "__main__":
    import sys
    main()
