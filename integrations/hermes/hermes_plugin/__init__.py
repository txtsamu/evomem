"""
evomem memory provider for Hermes Agent — real-time session capture.

Drop this folder into ~/.hermes/plugins/evomem/ and set
memory.provider: evomem in config.yaml.

Every Hermes session is recorded as JSONL events under
<bridge>/data/sessions-hermes/<session_id>.jsonl (same format the Claude
Code hook uses), then sync_to_evomem.py converts new events into markdown
under the evomem knowledge base and POSTs /api/sync — so the data lands in
evomem's SQLite index (FTS + vectors) within seconds of session end (and
at each context compression).

The built-in memory provider (MEMORY / USER PROFILE) stays active
alongside — this provider only *captures*; it never injects context
or exposes tools (use the evomem MCP tools for search/capture).

Env var:
  EVOMEM_BRIDGE_DIR   directory containing sync_to_evomem.py and the
                      `data/` dir (default: <repo>/integrations/claude-code
                      when running from a checkout, or set it to your
                      deployed bridge dir).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

try:
    from agent.memory_provider import MemoryProvider
except ImportError:
    from abc import ABC, abstractmethod

    class MemoryProvider(ABC):
        @property
        @abstractmethod
        def name(self) -> str: ...
        @abstractmethod
        def is_available(self) -> bool: ...
        @abstractmethod
        def initialize(self, session_id: str, **kwargs: Any) -> None: ...
        @abstractmethod
        def get_tool_schemas(self) -> list[dict]: ...
        @abstractmethod
        def handle_tool_call(self, name: str, args: dict) -> str: ...
        def get_config_schema(self) -> list[dict]:
            return []
        def save_config(self, values: dict, hermes_home: str) -> None:
            pass
        def system_prompt_block(self) -> str:
            return ""
        def prefetch(self, query: str, **kwargs: Any) -> str:
            return ""
        def queue_prefetch(self, query: str, **kwargs: Any) -> None:
            pass
        def sync_turn(self, user: str, assistant: str, **kwargs: Any) -> None:
            pass
        def on_session_end(self, messages: list, **kwargs: Any) -> None:
            pass
        def on_pre_compress(self, messages: list, **kwargs: Any) -> None:
            pass
        def on_memory_write(self, action: str, target: str, content: str, **kwargs: Any) -> None:
            pass
        def shutdown(self, **kwargs: Any) -> None:
            pass


EVOMEM_URL = os.environ.get("EVOMEM_URL", "http://localhost:7700")
_DEFAULT_BRIDGE = Path(__file__).resolve().parents[3] / "integrations" / "claude-code"
_BRIDGE = Path(os.environ.get("EVOMEM_BRIDGE_DIR", str(_DEFAULT_BRIDGE)))
_SESSION_DIR = _BRIDGE / "data" / "sessions-hermes"
_SYNC_SCRIPT = _BRIDGE / "sync_to_evomem.py"

_append_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_file(session_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:60]
    return _SESSION_DIR / f"{safe}.jsonl"


def _append_record(session_id: str, record: dict) -> None:
    """Append one JSONL event line (thread-safe, never raises)."""
    if not session_id or session_id == "unknown":
        return
    path = _session_file(session_id)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with _append_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        pass  # never break Hermes on capture failure


def _trigger_sync() -> None:
    """Fire-and-forget: convert new JSONL → markdown → evomem /api/sync."""
    if not _SYNC_SCRIPT.exists():
        return
    try:
        subprocess.Popen(
            [sys.executable, str(_SYNC_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def _evomem_ok() -> bool:
    try:
        req = Request(f"{EVOMEM_URL}/api/stats", method="GET")
        with urlopen(req, timeout=2):
            return True
    except Exception:
        return False


class EvomemMemoryProvider(MemoryProvider):

    @property
    def name(self) -> str:
        return "evomem"

    def is_available(self) -> bool:
        return _evomem_ok()

    def unavailable_reason(self) -> str:
        return f"evomem server not reachable at {EVOMEM_URL} (is evomem.service running?)"

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id
        self._project = kwargs.get("cwd", os.getcwd())
        _append_record(session_id, {
            "type": "session_start",
            "ts": _now(),
            "cwd": self._project,
            "source": "hermes",
        })

    def get_config_schema(self) -> list[dict]:
        return [{
            "key": "url",
            "description": "evomem server URL",
            "default": "http://localhost:7700",
            "env_var": "EVOMEM_URL",
        }]

    def system_prompt_block(self) -> str:
        # Never inject context — evomem is queried on demand via MCP tools.
        return ""

    def prefetch(self, query: str, **kwargs: Any) -> str:
        return ""

    def queue_prefetch(self, query: str, **kwargs: Any) -> None:
        pass

    def get_tool_schemas(self) -> list[dict]:
        # No tools here: the evomem MCP server already exposes
        # evomem_search / evomem_capture / evomem_think / etc.
        return []

    def handle_tool_call(self, name: str, args: dict) -> str:
        return json.dumps({"error": f"Unknown tool: {name}"})

    def sync_turn(self, user: str, assistant: str, **kwargs: Any) -> None:
        session_id = kwargs.get("session_id", getattr(self, "_session_id", ""))
        user_t = (user or "")[:500]
        assistant_t = (assistant or "")[:2000]
        content = f"USER: {user_t}\nASSISTANT: {assistant_t}".strip()
        if not content:
            return
        _append_record(session_id, {
            "type": "conversation",
            "ts": _now(),
            "content": content,
        })

    def on_pre_compress(self, messages: list, **kwargs: Any) -> None:
        session_id = kwargs.get("session_id", getattr(self, "_session_id", ""))
        _append_record(session_id, {
            "type": "summary",
            "ts": _now(),
            "content": "context compressed — session continuing",
        })
        # Mid-session checkpoint: get what we have into evomem now.
        _trigger_sync()

    def on_memory_write(self, action: str, target: str, content: str, **kwargs: Any) -> None:
        if not content:
            return
        session_id = kwargs.get("session_id", getattr(self, "_session_id", ""))
        _append_record(session_id, {
            "type": "memory_write",
            "ts": _now(),
            "content": f"action={action} target={target}\n{content}"[:2000],
        })

    def on_session_end(self, messages: list, **kwargs: Any) -> None:
        session_id = kwargs.get("session_id", getattr(self, "_session_id", ""))
        _append_record(session_id, {"type": "session_end", "ts": _now()})
        # Realtime: push to evomem's DB right now (cron stays as safety net).
        _trigger_sync()

    def shutdown(self, **kwargs: Any) -> None:
        pass


def register(ctx: Any) -> None:
    ctx.register_memory_provider(EvomemMemoryProvider())
