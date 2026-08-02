#!/usr/bin/env python3
"""Evomem MCP server — expose a running evomem knowledge server as MCP tools.

Lets any MCP-capable agent (Claude Code, Hermes, Cursor, etc.) search,
synthesize, capture, and traverse an evomem knowledge base over the Model
Context Protocol.

Requires an evomem REST server to be running (``evomem serve``), reachable
at the URL in ``EVOMEM_URL`` (default http://localhost:7700).

Install:  pip install mcp
Run:      python mcp_server.py
"""

import json
import os
import urllib.parse
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP

EVOMEM_URL = os.environ.get("EVOMEM_URL", "http://localhost:7700")
mcp = FastMCP("evomem")


def _call(method: str, path: str, body: Any = None, timeout: int = 30) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{EVOMEM_URL}{path}", data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


@mcp.tool()
def evomem_search(query: str, mode: str = "balanced", limit: int = 10) -> str:
    """Hybrid lexical+vector+graph retrieval over the evomem knowledge base.

    Args:
        query: natural-language search query
        mode: conservative | balanced | tokenmax
        limit: max results
    """
    try:
        d = _call(
            "GET",
            f"/api/search?q={urllib.parse.quote(query)}&mode={mode}&limit={limit}",
        )
    except Exception as e:
        return f"error: {e}"
    hits = d.get("hits", [])
    if not hits:
        return f"intent: {d.get('intent', '?')} | mode: {mode} | 0 hits"
    lines = [f"{len(hits)} hits (intent: {d.get('intent', '?')}):"]
    for h in hits[:limit]:
        lines.append(
            f"- {h.get('slug')} [score {h.get('score', 0):.4f} | "
            f"{h.get('evidence', '')}] {h.get('snippet', '')[:120]}"
        )
    return "\n".join(lines)


@mcp.tool()
def evomem_think(query: str, mode: str = "balanced") -> str:
    """Knowledge synthesis: composed facts with citations + gap analysis.

    Args:
        query: question about what the knowledge base contains
        mode: conservative | balanced | tokenmax
    """
    try:
        d = _call("GET", f"/api/think?q={urllib.parse.quote(query)}&mode={mode}")
    except Exception as e:
        return f"error: {e}"
    return json.dumps(d, indent=2, default=str)[:4000]


@mcp.tool()
def evomem_capture(text: str) -> str:
    """Capture a quick thought into the inbox/ and index it immediately.

    Args:
        text: thought/note to store
    """
    try:
        d = _call("POST", "/api/capture", {"text": text})
    except Exception as e:
        return f"error: {e}"
    return f"captured: {d.get('path', '')}"


@mcp.tool()
def evomem_graph(start: str, hops: int = 2) -> str:
    """Traverse typed edges in the knowledge graph from a doc slug.

    Args:
        start: doc slug (e.g. 'sessions/2026-05-14_...' or 'lessons/...')
        hops: traversal depth
    """
    try:
        d = _call("GET", f"/api/graph?start={urllib.parse.quote(start)}&hops={hops}")
    except Exception as e:
        return f"error: {e}"
    return json.dumps(d, indent=2, default=str)[:3000]


@mcp.tool()
def evomem_stats() -> str:
    """Knowledge store statistics (docs, chunks, indexed words, sources)."""
    try:
        d = _call("GET", "/api/stats")
    except Exception as e:
        return f"error: {e}"
    return json.dumps(d, indent=2, default=str)[:1500]


@mcp.tool()
def evomem_sync() -> str:
    """Rescan the knowledge root (markdown files -> DB). Disk is source of truth."""
    try:
        d = _call("POST", "/api/sync", {})
    except Exception as e:
        return f"error: {e}"
    return json.dumps(d, indent=2, default=str)[:1500]


@mcp.tool()
def evomem_doc(slug: str) -> str:
    """Show a doc's metadata and full content.

    Args:
        slug: doc slug (from evomem_search results)
    """
    try:
        d = _call("GET", f"/api/docs/{urllib.parse.quote(slug)}")
    except Exception as e:
        return f"error: {e}"
    return json.dumps(d, indent=2, default=str)[:4000]


if __name__ == "__main__":
    mcp.run()
