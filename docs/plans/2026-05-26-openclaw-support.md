# OpenClaw support Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `claude-tokens` count OpenClaw's own LLM consumption alongside Claude Code's, with a `source` dimension so the two can be inspected separately.

**Architecture:** Refactor `core.collect()` to drive a list of `Source` objects (one per data origin) that share a single dedup set. Add `OpenClawSource` that reads `~/.openclaw/agents/<agentId>/sessions/*.jsonl*`, skipping the `claude-code` / `codex` shell agents to avoid double-counting against the existing Claude Code reader.

**Tech Stack:** Python 3.11+, stdlib only (json, glob, pathlib), pytest for tests. No new dependencies.

**Design doc:** `docs/plans/2026-05-26-openclaw-support-design.md` (committed in `c8e7183`).

**Real-system fixture data:** A non-zero OpenClaw assistant line on this machine looks like:
```json
{"type":"message","id":"905c94d9","parentId":"f3516ac2",
 "timestamp":"2026-05-26T08:25:43.822Z",
 "message":{"role":"assistant",
   "content":[{"type":"text","text":"..."}],
   "api":"anthropic-messages","provider":"custom__litellm_proxy",
   "model":"claude-opus-4.6-vertex",
   "usage":{"input":3,"output":34,"cacheRead":0,"cacheWrite":21122,
            "totalTokens":21159,
            "cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}},
   "stopReason":"stop","timestamp":1779783943666,
   "responseId":"msg_vrtx_011oBNtPkryDUtMGehRaM4jk"}}
```

Pre-switch (zero-usage) lines have `"usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,...}` and must be dropped by the parser.

---

## Task 1: Add `source` field to UsageRow

**Files:**
- Modify: `src/claude_tokens/core.py:20-32` (UsageRow), `:160-167` (_parse_line return), `:170` (GROUP_KEYS)
- Modify: `tests/test_core.py` (every `UsageRow(...)` construction site)

**Step 1: Write a failing test asserting UsageRow has a `source` field**

Append to `tests/test_core.py`:

```python
def test_usage_row_has_source_field():
    from claude_tokens.core import UsageRow
    row = UsageRow(day="2026-05-26", model="claude-opus", project="p",
                   source="claude-code",
                   input=1, output=2, cache_create=3, cache_read=4)
    assert row.source == "claude-code"
```

**Step 2: Run it, confirm it fails**

Run: `cd /home/neolix/code/claude-tokens && python -m pytest tests/test_core.py::test_usage_row_has_source_field -v`
Expected: FAIL — `TypeError: UsageRow.__init__() got an unexpected keyword argument 'source'`

**Step 3: Add `source` to UsageRow and GROUP_KEYS, set in `_parse_line`**

In `src/claude_tokens/core.py`:

```python
# UsageRow — add source field after `project`
@dataclass
class UsageRow:
    day: str
    model: str
    project: str
    source: str          # NEW
    input: int
    output: int
    cache_create: int
    cache_read: int
    # ... .total property unchanged
```

```python
# GROUP_KEYS
GROUP_KEYS = ("day", "model", "project", "source")
```

```python
# _parse_line return — add source="claude-code"
return UsageRow(
    day=day.isoformat(),
    model=msg.get("model") or "?",
    project=project,
    source="claude-code",          # NEW
    input=int(usage.get("input_tokens") or 0),
    output=int(usage.get("output_tokens") or 0),
    cache_create=int(usage.get("cache_creation_input_tokens") or 0),
    cache_read=int(usage.get("cache_read_input_tokens") or 0),
)
```

**Step 4: Fix every existing UsageRow construction in tests**

In `tests/test_core.py`, every `UsageRow(...)` literal needs `source="claude-code"` added (positional or keyword — match the style already present).

Use `grep -n 'UsageRow(' tests/test_core.py` to find them all.

**Step 5: Run the full test suite, confirm green**

Run: `cd /home/neolix/code/claude-tokens && python -m pytest -v`
Expected: all tests pass, including the new `test_usage_row_has_source_field`.

**Step 6: Commit**

```bash
git add src/claude_tokens/core.py tests/test_core.py
git commit -m "Add source field to UsageRow

UsageRow now carries source ('claude-code' for now) and GROUP_KEYS
includes it. Preparation for OpenClaw source."
```

---

## Task 2: Extract ClaudeCodeSource, refactor collect()

**Files:**
- Modify: `src/claude_tokens/core.py` (introduce `ClaudeCodeSource`, change `collect()` signature)
- Modify: `src/claude_tokens/cli.py:127-135` (`render_once` — build sources list, pass to collect)

**Step 1: Run the existing test suite to capture baseline**

Run: `python -m pytest -v`
Expected: all pass. Record the count for comparison.

**Step 2: Refactor `core.py` — add `ClaudeCodeSource`, change `collect()`**

Replace the current top-level `iter_log_files` and `collect` with:

```python
class ClaudeCodeSource:
    """Scan ~/.claude/projects/ for Claude Code session jsonl files."""
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir

    def exists(self) -> bool:
        return self.log_dir.is_dir()

    def scan(
        self,
        seen: set[str],
        date_from: date,
        date_to: date,
        tz: timezone,
    ) -> Iterator[UsageRow]:
        file_cutoff = datetime.combine(date_from, datetime.min.time(), tzinfo=tz) - timedelta(days=2)
        for path in self.log_dir.rglob("*.jsonl"):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            if mtime < file_cutoff:
                continue
            proj = project_name_from_log_path(str(path), self.log_dir)
            try:
                fh = open(path, encoding="utf-8")
            except OSError:
                continue
            with fh:
                for line in fh:
                    row = _parse_claude_code_line(line, proj, date_from, date_to, tz, seen)
                    if row is not None:
                        yield row


def collect(
    sources: list,                          # list[Source-like]
    date_from: date,
    date_to: date,
    tz: timezone,
) -> list[UsageRow]:
    seen: set[str] = set()
    rows: list[UsageRow] = []
    for src in sources:
        rows.extend(src.scan(seen, date_from, date_to, tz))
    return rows
```

Rename `_parse_line` → `_parse_claude_code_line` (no behavior change, just clearer once a second parser appears).

Drop the now-unused top-level `iter_log_files` (folded into `ClaudeCodeSource.scan`).

Keep `project_name_from_log_path` as a module-level helper — `ClaudeCodeSource` uses it.

**Step 3: Update `cli.py`**

In `render_once`, replace:
```python
if not args.log_dir.is_dir():
    return f"log directory not found: {args.log_dir}"
rules = load_rules(pricing_path)
rows = collect(args.log_dir, date_from, date_to, tz)
```
with:
```python
from claude_tokens.core import ClaudeCodeSource
sources = [ClaudeCodeSource(args.log_dir)]
if not any(s.exists() for s in sources):
    return f"log directory not found: {args.log_dir}"
rules = load_rules(pricing_path)
rows = collect(sources, date_from, date_to, tz)
```

**Step 4: Update any test in `tests/test_core.py` that calls `collect(log_dir, ...)`**

`grep -n 'collect(' tests/test_core.py` — change call sites to `collect([ClaudeCodeSource(tmp_path)], ...)`.

**Step 5: Run the full test suite, confirm green**

Run: `python -m pytest -v`
Expected: same count as baseline, all pass.

**Step 6: Commit**

```bash
git add src/claude_tokens/core.py src/claude_tokens/cli.py tests/test_core.py
git commit -m "Refactor: collect() drives list of Source objects

ClaudeCodeSource wraps the existing scan. collect() now takes a sources
list and threads a single shared dedup set through them. No behavior
change."
```

---

## Task 3: OpenClaw parser — happy path + zero-usage drop

**Files:**
- Create: `src/claude_tokens/openclaw.py`
- Create: `tests/test_openclaw.py`

**Step 1: Write three failing parser tests**

Create `tests/test_openclaw.py`:

```python
import json
from datetime import date, timezone
from pathlib import Path

import pytest

from claude_tokens.openclaw import _parse_openclaw_line


TZ = timezone.utc
RANGE = (date(2026, 5, 1), date(2026, 12, 31))


def _line(**overrides) -> str:
    """Build a realistic OpenClaw assistant jsonl line, allow overrides."""
    obj = {
        "type": "message",
        "id": "905c94d9",
        "timestamp": "2026-05-26T08:25:43.822Z",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4.6-vertex",
            "responseId": "msg_vrtx_011oBNtPkryDUtMGehRaM4jk",
            "usage": {
                "input": 3, "output": 34,
                "cacheRead": 100, "cacheWrite": 21122,
            },
        },
    }
    for k, v in overrides.items():
        if "." in k:
            outer, inner = k.split(".", 1)
            obj[outer][inner] = v
        else:
            obj[k] = v
    return json.dumps(obj)


def test_parser_extracts_usage_and_sets_source():
    seen: set[str] = set()
    row = _parse_openclaw_line(_line(), "main", *RANGE, TZ, seen)
    assert row is not None
    assert row.source == "openclaw"
    assert row.project == "main"
    assert row.model == "claude-opus-4.6-vertex"
    assert row.input == 3 and row.output == 34
    assert row.cache_create == 21122   # cacheWrite -> cache_create
    assert row.cache_read == 100       # cacheRead -> cache_read
    assert row.day == "2026-05-26"


def test_parser_drops_all_zero_usage():
    """Defense: pre-protocol-switch records have all-zero usage; drop them."""
    seen: set[str] = set()
    zero = json.dumps({
        "type": "message",
        "timestamp": "2026-05-26T08:25:43.822Z",
        "message": {"role": "assistant", "model": "x",
                    "responseId": "msg_zero_1",
                    "usage": {"input": 0, "output": 0,
                              "cacheRead": 0, "cacheWrite": 0}},
    })
    assert _parse_openclaw_line(zero, "main", *RANGE, TZ, seen) is None


def test_parser_skips_non_assistant_roles():
    seen: set[str] = set()
    for role in ("user", "toolResult", "system"):
        line = json.dumps({
            "type": "message", "timestamp": "2026-05-26T08:25:43.822Z",
            "message": {"role": role, "usage": {"input": 100, "output": 100,
                                                "cacheRead": 0, "cacheWrite": 0},
                        "responseId": f"msg_{role}"},
        })
        assert _parse_openclaw_line(line, "main", *RANGE, TZ, seen) is None
```

**Step 2: Run them, confirm they fail**

Run: `python -m pytest tests/test_openclaw.py -v`
Expected: ImportError — `No module named 'claude_tokens.openclaw'`

**Step 3: Implement minimal parser**

Create `src/claude_tokens/openclaw.py`:

```python
"""Reader for OpenClaw (https://github.com/openclaw/openclaw) session logs.

OpenClaw stores per-agent session transcripts at
``~/.openclaw/agents/<agentId>/sessions/<sessionId>.jsonl`` (with rotated
``.jsonl.reset.<ts>`` archives). When the provider's ``api`` is set to
``anthropic`` / ``anthropic-messages``, every assistant line carries a
``message.usage`` block with the real token counts. This module turns
those into :class:`UsageRow`s for the shared aggregator in ``core``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from claude_tokens.core import UsageRow


DEFAULT_OPENCLAW_DIR = Path.home() / ".openclaw" / "agents"
SKIP_AGENTS = frozenset({"claude-code", "codex"})


def _parse_openclaw_line(
    line: str,
    agent_id: str,
    date_from: date,
    date_to: date,
    tz: timezone,
    seen: set[str],
) -> UsageRow | None:
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None
    msg = d.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    inp = int(usage.get("input") or 0)
    out = int(usage.get("output") or 0)
    cw = int(usage.get("cacheWrite") or 0)
    cr = int(usage.get("cacheRead") or 0)
    if inp == 0 and out == 0 and cw == 0 and cr == 0:
        return None
    rid = msg.get("responseId")
    if not rid or rid in seen:
        return None
    ts = d.get("timestamp")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)
    except ValueError:
        return None
    day = dt.date()
    if day < date_from or day > date_to:
        return None
    seen.add(rid)
    return UsageRow(
        day=day.isoformat(),
        model=msg.get("model") or "?",
        project=agent_id,
        source="openclaw",
        input=inp,
        output=out,
        cache_create=cw,
        cache_read=cr,
    )
```

**Step 4: Run the new tests, confirm green**

Run: `python -m pytest tests/test_openclaw.py -v`
Expected: 3 passed.

**Step 5: Commit**

```bash
git add src/claude_tokens/openclaw.py tests/test_openclaw.py
git commit -m "Add OpenClaw line parser

Maps cacheWrite/cacheRead to cache_create/cache_read, drops all-zero
usage rows (pre-protocol-switch defense), dedupes on
message.responseId."
```

---

## Task 4: OpenClawSource — file discovery

**Files:**
- Modify: `src/claude_tokens/openclaw.py` (add `OpenClawSource` class)
- Modify: `tests/test_openclaw.py` (add discovery tests)

**Step 1: Write failing discovery tests**

Append to `tests/test_openclaw.py`:

```python
def _write_session(base: Path, agent: str, session_id: str, lines: list[str], suffix: str = ".jsonl") -> Path:
    d = base / agent / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}{suffix}"
    p.write_text("\n".join(lines) + "\n")
    return p


def test_discovery_skips_shell_agents(tmp_path):
    from claude_tokens.openclaw import OpenClawSource
    _write_session(tmp_path, "main", "s1", [_line(**{"message.responseId": "msg_main"})])
    _write_session(tmp_path, "claude-code", "s2", [_line(**{"message.responseId": "msg_cc"})])
    _write_session(tmp_path, "codex", "s3", [_line(**{"message.responseId": "msg_cx"})])

    rows = list(OpenClawSource(tmp_path).scan(set(), *RANGE, TZ))
    projects = {r.project for r in rows}
    assert projects == {"main"}


def test_discovery_includes_reset_archives(tmp_path):
    from claude_tokens.openclaw import OpenClawSource
    _write_session(tmp_path, "main", "s1", [_line(**{"message.responseId": "msg_live"})])
    _write_session(tmp_path, "main", "s2",
                   [_line(**{"message.responseId": "msg_arch"})],
                   suffix=".jsonl.reset.2026-05-20T00-00-00.000Z")

    rows = list(OpenClawSource(tmp_path).scan(set(), *RANGE, TZ))
    assert {r.input for r in rows} == {3}      # both yielded
    assert len(rows) == 2
```

**Step 2: Run, confirm they fail**

Run: `python -m pytest tests/test_openclaw.py -v`
Expected: ImportError on `OpenClawSource` (or AttributeError).

**Step 3: Implement `OpenClawSource`**

Append to `src/claude_tokens/openclaw.py`:

```python
class OpenClawSource:
    """Scan ~/.openclaw/agents/ for non-shell agent session jsonl files."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir

    def exists(self) -> bool:
        return self.base_dir.is_dir()

    def scan(
        self,
        seen: set[str],
        date_from: date,
        date_to: date,
        tz: timezone,
    ) -> Iterator[UsageRow]:
        file_cutoff = datetime.combine(date_from, datetime.min.time(), tzinfo=tz) - timedelta(days=2)
        for agent_dir in sorted(self.base_dir.iterdir() if self.base_dir.is_dir() else []):
            if not agent_dir.is_dir() or agent_dir.name in SKIP_AGENTS:
                continue
            sessions = agent_dir / "sessions"
            if not sessions.is_dir():
                continue
            # Active sessions: *.jsonl ; rotated archives: *.jsonl.reset.*
            for path in sorted(sessions.iterdir()):
                name = path.name
                if not (name.endswith(".jsonl") or ".jsonl.reset." in name):
                    continue
                try:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                except OSError:
                    continue
                if mtime < file_cutoff:
                    continue
                try:
                    fh = open(path, encoding="utf-8")
                except OSError:
                    continue
                with fh:
                    for line in fh:
                        row = _parse_openclaw_line(
                            line, agent_dir.name, date_from, date_to, tz, seen,
                        )
                        if row is not None:
                            yield row
```

Note: `tmp_path`-written files have a fresh mtime so they pass the cutoff. Real `.reset.*` archives with old mtimes outside the requested window won't be opened — that's correct behavior, mirroring `ClaudeCodeSource`.

**Step 4: Run all tests, confirm green**

Run: `python -m pytest -v`
Expected: all pass (5 in `tests/test_openclaw.py` + original suite).

**Step 5: Commit**

```bash
git add src/claude_tokens/openclaw.py tests/test_openclaw.py
git commit -m "OpenClawSource: discover and scan agent session files

Skips claude-code/codex shell agents (their usage is already recorded
upstream by the native CLIs). Includes both live .jsonl and rotated
.jsonl.reset.* archives."
```

---

## Task 5: Cross-source dedup

**Files:**
- Modify: `tests/test_openclaw.py` (add cross-source test)

**Step 1: Write a failing test**

Append:

```python
def test_dedupe_across_sources(tmp_path):
    """A msg_vrtx_* id appearing in both Claude Code and OpenClaw files
    must be counted exactly once."""
    from claude_tokens.core import ClaudeCodeSource, collect
    from claude_tokens.openclaw import OpenClawSource

    shared_id = "msg_vrtx_DUPLICATE_ME"

    # OpenClaw side
    oc_root = tmp_path / "openclaw"
    _write_session(oc_root, "main", "s1",
                   [_line(**{"message.responseId": shared_id})])

    # Claude Code side: use the shared id as `message.id`
    cc_root = tmp_path / "claude" / "projects"
    project_dir = cc_root / "-home-neolix-x"
    project_dir.mkdir(parents=True)
    cc_line = json.dumps({
        "timestamp": "2026-05-26T08:25:43.822Z",
        "message": {
            "id": shared_id, "model": "claude-opus-4-7",
            "usage": {"input_tokens": 3, "output_tokens": 34,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    })
    (project_dir / "session.jsonl").write_text(cc_line + "\n")

    rows = collect(
        [ClaudeCodeSource(cc_root), OpenClawSource(oc_root)],
        *RANGE, TZ,
    )
    assert len(rows) == 1, f"expected single deduped row, got {rows}"
```

**Step 2: Run, confirm it fails or passes**

Run: `python -m pytest tests/test_openclaw.py::test_dedupe_across_sources -v`

Expected: PASS already — both parsers add to the same `seen` set in `collect()`. If it fails, you've found a bug in earlier task wiring; fix before commit.

**Step 3: Commit (regardless of whether the test had to drive new code)**

```bash
git add tests/test_openclaw.py
git commit -m "Test: single msg id is deduped across Claude Code and OpenClaw sources"
```

---

## Task 6: CLI auto-discovery

**Files:**
- Modify: `src/claude_tokens/cli.py` (add `ENV_OPENCLAW_DIR`, `build_sources`, update `render_once`)
- Modify: `src/claude_tokens/cli.py:48-101` (mention env var in help epilog)

**Step 1: Write a failing test for `build_sources`**

Append to `tests/test_openclaw.py`:

```python
def test_build_sources_auto_includes_openclaw_when_dir_exists(tmp_path, monkeypatch):
    from claude_tokens.cli import build_sources
    cc_dir = tmp_path / "claude_projects"
    cc_dir.mkdir()
    oc_dir = tmp_path / "openclaw_agents"
    oc_dir.mkdir()
    monkeypatch.setenv("CLAUDE_TOKENS_OPENCLAW_DIR", str(oc_dir))

    class Args:
        log_dir = cc_dir
    srcs = build_sources(Args())
    names = [type(s).__name__ for s in srcs]
    assert names == ["ClaudeCodeSource", "OpenClawSource"]


def test_build_sources_skips_openclaw_when_dir_missing(tmp_path, monkeypatch):
    from claude_tokens.cli import build_sources
    cc_dir = tmp_path / "claude_projects"
    cc_dir.mkdir()
    monkeypatch.setenv("CLAUDE_TOKENS_OPENCLAW_DIR",
                       str(tmp_path / "does_not_exist"))

    class Args:
        log_dir = cc_dir
    srcs = build_sources(Args())
    assert [type(s).__name__ for s in srcs] == ["ClaudeCodeSource"]
```

**Step 2: Run, confirm they fail**

Run: `python -m pytest tests/test_openclaw.py::test_build_sources_auto_includes_openclaw_when_dir_exists tests/test_openclaw.py::test_build_sources_skips_openclaw_when_dir_missing -v`
Expected: ImportError on `build_sources`.

**Step 3: Implement in `cli.py`**

Add imports + constant near the top:
```python
from claude_tokens.core import ClaudeCodeSource, DEFAULT_LOG_DIR, GROUP_KEYS, aggregate, collect
from claude_tokens.openclaw import DEFAULT_OPENCLAW_DIR, OpenClawSource

ENV_OPENCLAW_DIR = "CLAUDE_TOKENS_OPENCLAW_DIR"
```

Add helper:
```python
def build_sources(args) -> list:
    sources = [ClaudeCodeSource(args.log_dir)]
    oc_dir = Path(os.environ.get(ENV_OPENCLAW_DIR, str(DEFAULT_OPENCLAW_DIR)))
    if oc_dir.is_dir():
        sources.append(OpenClawSource(oc_dir))
    return sources
```

Rewrite `render_once`:
```python
def render_once(args, tz, tz_name, group_keys, pricing_path):
    date_from, date_to = resolve_dates(args, tz)
    sources = build_sources(args)
    present = [s for s in sources if s.exists()]
    if not present:
        return "no log sources found (need ~/.claude/projects or ~/.openclaw/agents)"
    rules = load_rules(pricing_path)
    rows = collect(sources, date_from, date_to, tz)
    buckets = aggregate(rows, group_keys, rules)
    source_names = [
        "claude-code" if isinstance(s, ClaudeCodeSource) else "openclaw"
        for s in present
    ]
    if args.json:
        return render_json(buckets, group_keys, date_from, date_to, tz_name)
    return render_table(buckets, group_keys, date_from, date_to, tz_name, source_names)
```

Update epilog string to mention `ENV_OPENCLAW_DIR`.

**Step 4: Run full test suite**

Run: `python -m pytest -v`
Expected: all pass.

**Step 5: Commit**

```bash
git add src/claude_tokens/cli.py tests/test_openclaw.py
git commit -m "CLI auto-discovers OpenClaw at ~/.openclaw/agents

CLAUDE_TOKENS_OPENCLAW_DIR overrides the path; pointing it at a
non-existent directory effectively opts out."
```

---

## Task 7: Format header `sources:` line

**Files:**
- Modify: `src/claude_tokens/format.py:30-43` (render_table signature + header)
- Modify: `tests/test_core.py` (any call site of render_table — likely none; check)

**Step 1: Run baseline**

Run: `python -m pytest -v` — capture pass count.

**Step 2: Update `render_table` signature and header**

```python
def render_table(
    buckets,
    group_keys,
    date_from,
    date_to,
    tz_name,
    sources: list[str] | None = None,    # NEW, default keeps backward compat
) -> str:
    lines: list[str] = []
    src_suffix = f"  sources: {', '.join(sources)}" if sources else ""
    lines.append(
        f"# AI CLI token usage  {date_from} ~ {date_to}  ({tz_name})"
        f"  group by: {', '.join(group_keys)}{src_suffix}"
    )
    # ... rest unchanged
```

Add a small test in `tests/test_core.py`:
```python
def test_render_table_header_includes_sources():
    from datetime import date
    from claude_tokens.core import Bucket
    from claude_tokens.format import render_table
    out = render_table({}, ("day",), date(2026,1,1), date(2026,1,2), "UTC",
                       sources=["claude-code", "openclaw"])
    assert "sources: claude-code, openclaw" in out
    assert "AI CLI token usage" in out
```

**Step 3: Run tests, confirm green**

Run: `python -m pytest -v`
Expected: all pass + new one.

**Step 4: Commit**

```bash
git add src/claude_tokens/format.py tests/test_core.py
git commit -m "Format: header reflects active sources

Drops the literal 'Claude Code' from the header since OpenClaw rows
may also be present; appends 'sources: ...' so the table self-describes
which tools it covers."
```

---

## Task 8: README

**Files:**
- Modify: `README.md`

**Step 1: Add a section after the existing "Usage" block**

Append:

```markdown
## OpenClaw support

If you also use [OpenClaw](https://github.com/openclaw/openclaw),
`claude-tokens` automatically picks up its session logs at
`~/.openclaw/agents/` (skipping the `claude-code` and `codex` shell
agents, which would double-count against the native CLIs). The table
header lists which sources are active; a new `source` group key lets
you split them apart:

```sh
claude-tokens --group source,day
```

To opt out, point the env var at a non-existent path:

```sh
CLAUDE_TOKENS_OPENCLAW_DIR=/dev/null claude-tokens
```

**Note:** OpenClaw only records non-zero token usage when its model
provider is configured with `"api": "anthropic"` (or
`"anthropic-messages"`). If you're going through an OpenAI-Completions
adapter and seeing zeros, that's a known OpenClaw issue — switch the
provider's `api` field, restart the gateway, and new sessions will
have real numbers.
```

**Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README mentions OpenClaw auto-discovery and opt-out"
```

---

## Task 9: End-to-end manual verification on the real machine

**Step 1: Run on real data**

```sh
cd /home/neolix/code/claude-tokens
python -m claude_tokens --days 7
```

Expected:
- Table header includes `sources: claude-code, openclaw`
- TOTAL row's token counts are at least as large as the previous (claude-code-only) run, by an amount matching the user's recent OpenClaw activity.

**Step 2: Inspect the split**

```sh
python -m claude_tokens --days 7 --group source,day
```

Expected: each day has up to two rows (`claude-code`, `openclaw`); the OpenClaw row appears for days the user had OpenClaw conversations.

**Step 3: Spot-check OpenClaw numbers against raw jsonl**

For a recent OpenClaw session file, sum its `usage.input + output + cacheRead + cacheWrite` per assistant line by hand (e.g. with a one-liner `python3 -c "..."`) and confirm it lines up with the day's OpenClaw total in `--group source,day` output.

**Step 4: Opt-out smoke test**

```sh
CLAUDE_TOKENS_OPENCLAW_DIR=/nope python -m claude_tokens --days 7
```

Expected: header reads `sources: claude-code` only; numbers match pre-feature behavior.

**Step 5: No commit (verification only).** If everything checks out, mention to the user that the feature is ready. If anything is off, debug with `superpowers:systematic-debugging` and fix in a follow-up task.

---

## Wrap-up

After Task 9 passes, follow up with:
- `superpowers:requesting-code-review` to get a fresh-eyes review on the diff against `main`
- `superpowers:finishing-a-development-branch` to decide on PR vs direct merge
