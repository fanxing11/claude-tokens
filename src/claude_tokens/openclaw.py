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
from typing import ClassVar, Iterator

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


class OpenClawSource:
    """Scan ~/.openclaw/agents/ for non-shell agent session jsonl files."""

    name: ClassVar[str] = "openclaw"

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
        # Two-level walk (agents/ -> sessions/) so we can filter by agent name;
        # ClaudeCodeSource uses rglob since its layout is one project per dir.
        for agent_dir in sorted(self.base_dir.iterdir()):
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
