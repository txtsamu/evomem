# Evomem integrations

Plugins and adapters that connect evomem to AI agents and tools. The core
evomem binary (Rust) stays dependency-free; these integrations are optional
and live outside the crate.

| Directory     | What it does                                                              |
| ------------- | ------------------------------------------------------------------------- |
| `mcp/`        | MCP server exposing evomem search/think/capture/graph/stats/sync/doc to any MCP-capable agent |
| `claude-code/`| Claude Code hooks that record every session/prompt/tool event into the knowledge base |
| `hermes/`     | Hermes Agent memory-provider plugin (realtime capture) + backfill script for historical sessions |

## MCP server

Lets any MCP-capable agent (Claude Code, Cursor, Hermes, ...) query and write
to an evomem knowledge base over the Model Context Protocol.

```bash
# 1. Start the evomem REST server
evomem serve --host 127.0.0.1 --port 7700

# 2. Install the MCP SDK
pip install -r mcp/requirements.txt

# 3. Register with your agent (example: Claude Code)
claude mcp add evomem -- python3 "$(pwd)/mcp/mcp_server.py"
```

Exposed tools:

- `evomem_search` — hybrid lexical+vector+graph retrieval
- `evomem_think` — knowledge synthesis with citations + gap analysis
- `evomem_capture` — store a thought into `inbox/` (indexed immediately)
- `evomem_graph` — traverse typed edges from a doc slug
- `evomem_stats` — store statistics
- `evomem_sync` — rescan the knowledge root
- `evomem_doc` — show a doc's metadata and full content

Set `EVOMEM_URL` (default `http://localhost:7700`) to point at a remote
evomem server.

## Claude Code hooks

Records Claude Code activity (sessions, prompts, tool results, failures) into
evomem so the agent's past work becomes searchable knowledge. It mirrors the
hook coverage of agentmemory-style memory plugins but writes into a plain
markdown knowledge root.

Flow:

```
Claude Code ──hooks──▶ hook.py ──JSONL──▶ data/sessions/*.jsonl
                                              │
                                              ▼  (cron, e.g. every 5 min)
                                     sync_to_evomem.py ──▶ evomem-kb/claude/*.md
                                              │
                                              ▼
                                     evomem sync (via server API)
```

Why JSONL in between? Hook execution must never block Claude Code. Appending
one line per event is O(1); markdown conversion and reindexing happen in a
background job.

### Setup

1. Copy `hook.py` and `sync_to_evomem.py` to a stable path, e.g.
   `~/evomem-bridge/hooks/`.
2. Add the hooks to `~/.claude/settings.json` — see
   `settings.example.json` (or run `claude mcp add` for the MCP side).
3. Add the MCP server so Claude Code can *query* the knowledge base:
   `claude mcp add evomem -- python3 ~/evomem-bridge/mcp_server.py`
4. Run `sync_to_evomem.py` on a schedule:

   ```bash
   EVOMEM_URL=http://localhost:7700 EVOMEM_KB=$HOME/evomem-kb \
     python3 ~/evomem-bridge/hooks/sync_to_evomem.py
   ```

   Example cron (every 5 minutes):

   ```
   */5 * * * * EVOMEM_URL=http://localhost:7700 EVOMEM_KB=$HOME/evomem-kb \
     python3 $HOME/evomem-bridge/hooks/sync_to_evomem.py >> /tmp/evomem-sync.log 2>&1
   ```

### Multi-host setup

If Claude Code runs on several machines sharing one evomem server, rsync the
remote host's JSONL into `data/sessions-<host>/` before running the sync job.
Notes from that host get a `<host>` label in the generated markdown:

```bash
rsync -az remote:/path/to/data/sessions/ ~/evomem-bridge/hooks/data/sessions-fedora/
python3 ~/evomem-bridge/hooks/sync_to_evomem.py
```

### Environment variables

| Variable         | Default            | Used by          |
| ---------------- | ------------------ | ---------------- |
| `EVOMEM_URL`     | `http://localhost:7700` | MCP server, sync script |
| `EVOMEM_KB`      | `../../evomem-kb` relative to script | sync script |
| `EVOMEM_DATA_DIR`| `./data` next to hook.py | hook, sync script |

## Hermes integration

Records Hermes Agent sessions into evomem — both in realtime (memory-provider
plugin) and historically (backfill script), so the agent's past work becomes
searchable knowledge.

### Realtime capture (plugin)

Hermes memory providers get lifecycle hooks (`session_start`, per-turn
conversation, context-compression summary, memory writes, session end). The
plugin in `hermes/hermes_plugin/` writes JSONL events into
`<bridge>/data/sessions-hermes/<session_id>.jsonl` — the exact same format the
Claude Code hooks use — and spawns `sync_to_evomem.py` at session end /
pre-compress, so data reaches the evomem index within seconds. It never
injects context and exposes no tools; it only *captures*.

```bash
# 1. Install the plugin
cp -r hermes/hermes_plugin ~/.hermes/plugins/evomem

# 2. Enable it in config.yaml
#    memory.provider: evomem   (the built-in provider stays active alongside)
#    plugins.enabled: [..., evomem]

# 3. Point it at your deployed bridge (contains sync_to_evomem.py + data/)
#    export EVOMEM_BRIDGE_DIR=/path/to/your/evomem-bridge/hooks

# 4. Restart the Hermes gateway (memory providers load at agent init)
```

### Historical backfill

Hermes stores session transcripts in `~/.hermes/state.db` (`sessions` +
`messages` tables) with a few older ones only as JSONL files in
`~/.hermes/sessions/`. `hermes/backfill_hermes_sessions.py` walks every
session (excluding `claude_code` imports, which the Claude Code hooks already
own), renders each as a markdown note in `evomem-kb/sessions/` and triggers
`evomem sync`. Idempotent — sessions already captured (realtime or a previous
run) are skipped.

```bash
python3 hermes/backfill_hermes_sessions.py              # backfill + sync
python3 hermes/backfill_hermes_sessions.py --dry-run    # preview counts only
```

Environment variables:

| Variable             | Default                    | Used by          |
| -------------------- | -------------------------- | ---------------- |
| `EVOMEM_URL`         | `http://localhost:7700`    | plugin, backfill |
| `EVOMEM_BRIDGE_DIR`  | `<repo>/integrations/claude-code` | plugin |
| `HERMES_DB`          | `~/.hermes/state.db`       | backfill         |
| `HERMES_SESSIONS_DIR`| `~/.hermes/sessions`       | backfill         |
| `REALTIME_JSONL_DIR` | `<script>/data/sessions-hermes` | backfill    |
| `EVOMEM_KB`          | `../../evomem-kb` relative to script | backfill |
| `USER_MAX` / `ASSISTANT_MAX` | `500` / `2000`      | backfill         |
