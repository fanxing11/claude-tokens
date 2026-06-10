from __future__ import annotations

import json
from datetime import date, timedelta, timezone
from pathlib import Path

from claude_tokens.core import ClaudeCodeSource, aggregate, collect, project_name_from_log_path
from claude_tokens.pricing import DEFAULT_RULES, Price, PriceRule, price_for


TZ = timezone(timedelta(hours=8))


def _write_session(dir_path: Path, session_id: str, entries: list[dict]) -> Path:
    f = dir_path / f"{session_id}.jsonl"
    with f.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return f


def _assistant(msg_id: str, ts: str, model: str, **usage) -> dict:
    return {
        "type": "assistant",
        "uuid": f"uuid-{msg_id}",
        "timestamp": ts,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": usage.get("input", 0),
                "output_tokens": usage.get("output", 0),
                "cache_creation_input_tokens": usage.get("cache_create", 0),
                "cache_read_input_tokens": usage.get("cache_read", 0),
            },
        },
    }


def test_collect_dedupes_by_message_id(tmp_path: Path) -> None:
    project_dir = tmp_path / "-home-user-code-foo"
    project_dir.mkdir()
    e1 = _assistant("msg_A", "2026-04-30T01:00:00Z", "claude-opus-4-7", input=10, output=20)
    e2 = _assistant("msg_A", "2026-04-30T01:00:00Z", "claude-opus-4-7", input=10, output=20)
    e3 = _assistant("msg_B", "2026-04-30T02:00:00Z", "claude-sonnet-4-6", input=5, output=5)
    _write_session(project_dir, "s1", [e1, e2])
    _write_session(project_dir, "s2", [e3])

    rows = collect([ClaudeCodeSource(tmp_path)], date(2026, 4, 1), date(2026, 4, 30), TZ)
    assert sorted(r.model for r in rows) == ["claude-opus-4-7", "claude-sonnet-4-6"]
    assert len(rows) == 2


def test_collect_filters_by_date_range(tmp_path: Path) -> None:
    pdir = tmp_path / "-foo"
    pdir.mkdir()
    early = _assistant("m1", "2026-04-01T00:00:00Z", "claude-opus-4-7", input=1)
    late = _assistant("m2", "2026-04-30T00:00:00Z", "claude-opus-4-7", input=1)
    _write_session(pdir, "s", [early, late])

    rows = collect([ClaudeCodeSource(tmp_path)], date(2026, 4, 15), date(2026, 4, 30), TZ)
    assert [r.model for r in rows] == ["claude-opus-4-7"]
    # date_from inclusive, applied after tz shift (UTC 04-01 00:00 -> +8 04-01 08:00)
    assert rows[0].day == "2026-04-30"


def test_collect_skips_non_assistant_and_missing_usage(tmp_path: Path) -> None:
    pdir = tmp_path / "-foo"
    pdir.mkdir()
    user = {"type": "user", "timestamp": "2026-04-30T00:00:00Z", "message": "hi"}
    no_usage = {"type": "assistant", "timestamp": "2026-04-30T00:00:00Z", "message": {"id": "x"}}
    real = _assistant("m", "2026-04-30T00:00:00Z", "claude-opus-4-7", input=3)
    _write_session(pdir, "s", [user, no_usage, real])
    rows = collect([ClaudeCodeSource(tmp_path)], date(2026, 4, 1), date(2026, 4, 30), TZ)
    assert len(rows) == 1


def test_aggregate_sums_and_costs(tmp_path: Path) -> None:
    pdir = tmp_path / "-foo"
    pdir.mkdir()
    _write_session(
        pdir,
        "s",
        [
            _assistant("a", "2026-04-30T00:00:00Z", "claude-opus-4-7", input=1_000_000),
            _assistant("b", "2026-04-30T01:00:00Z", "claude-opus-4-7", output=1_000_000),
            _assistant("c", "2026-04-30T02:00:00Z", "unknown-model", input=999_999),
        ],
    )
    rows = collect([ClaudeCodeSource(tmp_path)], date(2026, 4, 1), date(2026, 4, 30), TZ)
    buckets = aggregate(rows, ("day",), DEFAULT_RULES)
    key = ("2026-04-30",)
    b = buckets[key]
    assert b.msgs == 3
    # opus: 1M input * $15 + 1M output * $75 = $90
    assert abs(b.cost - 90.0) < 1e-6
    assert b.cost_known is False  # unknown-model contaminated this bucket


def test_project_name_decoding() -> None:
    log_dir = Path("/x/.claude/projects")
    assert project_name_from_log_path(
        "/x/.claude/projects/-home-neolix-code-foo/sess.jsonl", log_dir
    ) == "/home/neolix/code/foo"
    # Subagent files (one level deeper) yield the same project name
    assert project_name_from_log_path(
        "/x/.claude/projects/-home-neolix-code-foo/sess-uuid/subagents/agent-abc.jsonl",
        log_dir,
    ) == "/home/neolix/code/foo"
    # Non-dash-prefixed paths pass through verbatim
    assert project_name_from_log_path(
        "/x/.claude/projects/local-thing/sess.jsonl", log_dir
    ) == "local-thing"


def test_collect_picks_up_subagent_files(tmp_path: Path) -> None:
    """Subagent calls live at <project>/<session>/subagents/agent-*.jsonl."""
    project_dir = tmp_path / "-home-user-code-foo"
    project_dir.mkdir()

    # Top-level session file
    _write_session(
        project_dir,
        "session_top",
        [_assistant("msg_main", "2026-04-30T01:00:00Z", "claude-opus-4-7", input=100)],
    )

    # Subagent file two levels deeper
    sub_dir = project_dir / "session_uuid" / "subagents"
    sub_dir.mkdir(parents=True)
    _write_session(
        sub_dir,
        "agent-deadbeef",
        [_assistant("msg_sub", "2026-04-30T01:05:00Z", "claude-haiku-4-5", input=200)],
    )

    rows = collect([ClaudeCodeSource(tmp_path)], date(2026, 4, 1), date(2026, 4, 30), TZ)
    by_model = {r.model: r for r in rows}
    assert set(by_model) == {"claude-opus-4-7", "claude-haiku-4-5"}
    # Subagent's project name decodes from the project root, not the "subagents" dir
    assert by_model["claude-haiku-4-5"].project == "/home/user/code/foo"


def test_pricing_user_rules_take_precedence(tmp_path: Path) -> None:
    config = tmp_path / "pricing.toml"
    config.write_text(
        '[[models]]\nmatch = "opus"\ninput = 1.0\noutput = 1.0\ncache_create = 1.0\ncache_read = 1.0\n'
    )
    from claude_tokens.pricing import load_rules

    rules = load_rules(config)
    p = price_for("claude-opus-4-7", rules)
    assert p == Price(1.0, 1.0, 1.0, 1.0)


def test_usage_row_has_source_field():
    from claude_tokens.core import UsageRow
    row = UsageRow(day="2026-05-26", model="claude-opus", project="p",
                   source="claude-code",
                   input=1, output=2, cache_create=3, cache_read=4)
    assert row.source == "claude-code"


def test_render_table_header_includes_sources():
    from datetime import date
    from claude_tokens.format import render_table
    out = render_table({}, ("day",), date(2026, 1, 1), date(2026, 1, 2), "UTC",
                       sources=["claude-code", "openclaw"])
    assert "sources: claude-code, openclaw" in out
    assert "AI CLI token usage" in out
    assert "Claude Code usage" not in out  # NEW: old header retired


def test_render_table_header_omits_sources_when_none():
    from datetime import date
    from claude_tokens.format import render_table
    out = render_table({}, ("day",), date(2026, 1, 1), date(2026, 1, 2), "UTC")
    assert "sources:" not in out
    assert "AI CLI token usage" in out
