# OpenClaw support — design

**Date:** 2026-05-26
**Status:** approved, ready for implementation plan

## Background

`claude-tokens` today reads `~/.claude/projects/*.jsonl` to report
token usage and cost for Claude Code sessions. The user also runs
[OpenClaw](https://github.com/openclaw/openclaw), a third-party agent
framework whose user-facing agents (`main`, `ops`, `intel`, `github`,
`ota-ops`) call LLMs directly through a LiteLLM proxy — these calls do
not appear in Claude Code's logs and are currently invisible to
`claude-tokens`.

### Prerequisite already done

OpenClaw was configured to talk to the proxy via OpenAI Chat Completions
(`api: openai-completions`), where its adapter did not populate
`message.usage`, leaving every session line with token counts of 0.
Switching the provider to `api: anthropic-messages` (the proxy already
supports the Anthropic protocol — Claude Code uses it) made OpenClaw
record real `input` / `output` / `cacheWrite` / `cacheRead` values in
its session jsonl files.

## Goal

Make `claude-tokens` count OpenClaw's own LLM consumption alongside
Claude Code's, with a clearly labeled `source` dimension so the two can
be inspected separately when wanted but are merged by default.

Non-goals: tracking Codex CLI usage, tracking OpenClaw → Claude Code or
OpenClaw → Codex shell delegations (already counted upstream),
supporting other agent frameworks.

## Data model

`UsageRow` gains one field; `GROUP_KEYS` gains one entry.

```python
@dataclass
class UsageRow:
    day: str
    model: str
    project: str
    source: str        # NEW: "claude-code" | "openclaw"
    input: int
    output: int
    cache_create: int
    cache_read: int

GROUP_KEYS = ("day", "model", "project", "source")
```

Field semantics per source:

| source | `project` field |
|---|---|
| `claude-code` | cwd (e.g. `/home/neolix/code/claude-tokens`) — unchanged |
| `openclaw` | OpenClaw agentId (`main` / `ops` / `intel` / `github` / `ota-ops`) — no prefix; `source` column already disambiguates |

## OpenClaw reader

New module `src/claude_tokens/openclaw.py` (~60 LOC).

- **Discovery:** `~/.openclaw/agents/<agentId>/sessions/*.jsonl*` —
  glob covers both active `.jsonl` and rotated `.jsonl.reset.<ts>`
  archives.
- **Skip list:** `frozenset({"claude-code", "codex"})` — these
  OpenClaw agents are shells that delegate to native Claude Code /
  Codex CLIs; their true usage is recorded in those CLIs' own log
  directories, so reading them would double-count.
- **Parsing per line:**
  - keep only `message.role == "assistant"`
  - drop rows where `message.usage` is missing or all four token
    counts are 0 (defensive: protects against pre-switch records)
  - field mapping:
    ```
    message.usage.input       -> UsageRow.input
    message.usage.output      -> UsageRow.output
    message.usage.cacheWrite  -> UsageRow.cache_create
    message.usage.cacheRead   -> UsageRow.cache_read
    ```
  - `message.model` → `UsageRow.model` (e.g.
    `claude-opus-4.6-vertex` — the existing substring rule for
    `"opus"` matches this and gives correct pricing without any
    changes)
  - top-level `timestamp` (ISO-8601) → day in caller-supplied tz
  - dedup key: `message.responseId` (post-switch is `msg_vrtx_*`,
    same id-space as Claude Code's `message.id`)
  - `project = agent_id`, `source = "openclaw"`

## Source orchestration

`core.collect()` is refactored to accept a list of `Source` objects
instead of a single `log_dir`. Each `Source` knows how to scan itself
and yield `UsageRow`s. They share one `seen: set[str]` so a single
`msg_*` id that somehow appears in both sources is counted exactly
once.

```python
def collect(sources: list[Source], date_from, date_to, tz) -> list[UsageRow]:
    seen: set[str] = set()
    rows = []
    for src in sources:
        rows.extend(src.scan(seen, date_from, date_to, tz))
    return rows
```

Two implementations: `ClaudeCodeSource(log_dir)` (wraps current
behavior) and `OpenClawSource(base_dir)`.

## CLI

No new flags. Source list is built from existing config:

```python
ENV_OPENCLAW_DIR = "CLAUDE_TOKENS_OPENCLAW_DIR"

def build_sources(args) -> list[Source]:
    sources = [ClaudeCodeSource(args.log_dir)]
    oc_dir = Path(os.environ.get(ENV_OPENCLAW_DIR,
                                 str(DEFAULT_OPENCLAW_DIR)))
    if oc_dir.is_dir():
        sources.append(OpenClawSource(oc_dir))
    return sources
```

- OpenClaw directory present → auto-included
- Missing → silently skipped
- `CLAUDE_TOKENS_OPENCLAW_DIR=""` (or pointing at a non-existent path)
  → effectively opts out
- `--group` accepts new key `source`; everything else unchanged

## Output format

Two small format changes in `format.py`:

1. **Header wording.** Today:
   `# Claude Code usage  <from> ~ <to>  (tz)  group by: <keys>`
   Becomes:
   `# AI CLI token usage  <from> ~ <to>  (tz)  group by: <keys>  sources: <list>`
   `sources:` lists only sources actually scanned (so a machine
   without OpenClaw still says `sources: claude-code`).
2. **`source` column.** Renders through the existing generic
   group-key path — no fmt-string changes needed.

JSON output picks up the new `source` field automatically.

`README.md` gains a short note on automatic OpenClaw discovery and the
opt-out env var.

## Testing

Targeted at *new* behavior only — existing `tests/test_core.py` stays
authoritative for the Claude Code path (with one mechanical fix-up:
construction sites that build `UsageRow` need `source="claude-code"`
added).

New `tests/test_openclaw.py`, fixtures generated in `tmp_path`:

1. parser extracts usage and sets `source="openclaw"`
2. parser drops zero-usage rows (pre-switch defense)
3. parser skips non-assistant roles
4. discovery skips `claude-code` and `codex` shell agents
5. discovery includes both `.jsonl` and `.jsonl.reset.<ts>` files
6. `collect()` dedupes the same `msg_*` id across Claude Code and
   OpenClaw sources

### Manual verification (end of implementation)

On the real machine:

1. `claude-tokens --days 7` — header shows
   `sources: claude-code, openclaw`
2. `claude-tokens --days 7 --group source,day` — two source rows per day
3. OpenClaw row totals match the real `cacheWrite + input + output`
   observed in `~/.openclaw/agents/main/sessions/*.jsonl`

## Risk notes

- The protocol switch in `~/.openclaw/openclaw.json` (and a small
  backup `openclaw.json.before-anthropic-switch`) is a prerequisite,
  not part of this change. Reverting it would not break
  `claude-tokens` — OpenClaw would just start recording zero-usage
  rows again, which the parser already filters out.
- If a future OpenClaw release renames `cacheWrite`/`cacheRead` or
  moves `responseId`, the parser fails closed (returns no rows) rather
  than miscounting.
