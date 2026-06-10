import json
from datetime import date, timezone
from pathlib import Path

import pytest

from claude_tokens.core import ClaudeCodeSource, collect
from claude_tokens.openclaw import OpenClawSource, _parse_openclaw_line


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


def _write_session(base: Path, agent: str, session_id: str, lines: list[str], suffix: str = ".jsonl") -> Path:
    d = base / agent / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}{suffix}"
    p.write_text("\n".join(lines) + "\n")
    return p


def test_discovery_skips_shell_agents(tmp_path):
    _write_session(tmp_path, "main", "s1", [_line(**{"message.responseId": "msg_main"})])
    _write_session(tmp_path, "claude-code", "s2", [_line(**{"message.responseId": "msg_cc"})])
    _write_session(tmp_path, "codex", "s3", [_line(**{"message.responseId": "msg_cx"})])

    rows = list(OpenClawSource(tmp_path).scan(set(), *RANGE, TZ))
    projects = {r.project for r in rows}
    assert projects == {"main"}


def test_discovery_includes_reset_archives(tmp_path):
    _write_session(tmp_path, "main", "s1",
                   [_line(**{"message.responseId": "msg_live"})])  # input=3 by default
    _write_session(tmp_path, "main", "s2",
                   [_line(**{"message.responseId": "msg_arch",
                             "message.usage": {"input": 999, "output": 1,
                                               "cacheRead": 0, "cacheWrite": 0}})],
                   suffix=".jsonl.reset.2026-05-20T00-00-00.000Z")

    rows = list(OpenClawSource(tmp_path).scan(set(), *RANGE, TZ))
    assert {r.input for r in rows} == {3, 999}, \
        "both live .jsonl and .jsonl.reset.* archive must be parsed"
    assert len(rows) == 2


def test_dedupe_across_sources(tmp_path):
    """A msg_vrtx_* id appearing in both Claude Code and OpenClaw files
    must be counted exactly once."""
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
    assert rows[0].source == "claude-code", \
        "first source in the list should win the dedup"


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
