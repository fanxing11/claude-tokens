"""Core: scan Claude Code session logs, dedupe, aggregate."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar, Iterable, Iterator, Protocol

from claude_tokens.pricing import Price, PriceRule, cost_usd, price_for


DEFAULT_LOG_DIR = Path.home() / ".claude" / "projects"


@dataclass
class UsageRow:
    day: str       # YYYY-MM-DD in caller-supplied tz
    model: str
    project: str
    source: str    # "claude-code" or "openclaw"
    input: int
    output: int
    cache_create: int
    cache_read: int

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_create + self.cache_read


@dataclass
class Bucket:
    msgs: int = 0
    input: int = 0
    output: int = 0
    cache_create: int = 0
    cache_read: int = 0
    cost: float = 0.0
    cost_known: bool = True   # False if any contributing row had no priced model

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_create + self.cache_read

    def add(self, row: UsageRow, price: Price | None) -> None:
        self.msgs += 1
        self.input += row.input
        self.output += row.output
        self.cache_create += row.cache_create
        self.cache_read += row.cache_read
        if price is None:
            self.cost_known = False
        else:
            self.cost += cost_usd(price, row.input, row.output, row.cache_create, row.cache_read)


def project_name_from_log_path(path: str, log_dir: Path) -> str:
    """Decode the encoded project directory name from a log file path.

    Both top-level session files (``<log_dir>/<project>/<session>.jsonl``) and
    subagent files (``<log_dir>/<project>/<session>/subagents/agent-*.jsonl``)
    yield the same project name — we always take the first path segment under
    ``log_dir``.

    Claude Code encodes the project cwd by replacing both ``/`` and ``.`` with
    ``-``. A hidden directory like ``/.openclaw`` therefore encodes to ``--``
    (the slash and the dot collapse into two dashes), so we decode ``--`` back
    to ``/.`` before turning the remaining single dashes into slashes —
    otherwise the dot is lost and a stray ``//`` appears. This decoder is
    best-effort: a segment that legitimately contained ``-`` can't be perfectly
    reversed, but the result is still a stable, human-readable identifier good
    enough for grouping.
    """
    try:
        rel = Path(path).relative_to(log_dir)
    except ValueError:
        # Path isn't under log_dir; fall back to immediate parent dir name.
        rel = Path(os.path.basename(os.path.dirname(path)))
    encoded = rel.parts[0] if rel.parts else ""
    if encoded.startswith("-"):
        # Leading dash represents the leading slash of an absolute path.
        # "--" marks a hidden dir ("/."); decode it first so the dot survives.
        return "/" + encoded[1:].replace("--", "/.").replace("-", "/")
    return encoded


class Source(Protocol):
    """A reader for one kind of session log directory.

    Implementations are not required to subclass Source — duck-typing
    suffices. Listed here as a single place to see the contract that
    collect() depends on.
    """

    name: ClassVar[str]   # "claude-code" / "openclaw" — matches UsageRow.source

    def exists(self) -> bool:
        """True if this source's underlying directory is present."""

    def scan(
        self,
        seen: set[str],
        date_from: date,
        date_to: date,
        tz: timezone,
    ) -> Iterator[UsageRow]:
        """Yield UsageRows in the requested window, adding their
        dedup keys to ``seen`` as it goes."""


class ClaudeCodeSource:
    """Scan ~/.claude/projects/ for Claude Code session jsonl files."""

    name: ClassVar[str] = "claude-code"

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
    sources: Iterable[Source],
    date_from: date,
    date_to: date,
    tz: timezone,
) -> list[UsageRow]:
    """Scan all sources and return one UsageRow per unique message id in range.

    A single ``seen: set[str]`` is threaded through every source so that
    Anthropic-style message IDs (``msg_*`` / ``msg_vrtx_*``), which are
    globally unique, are deduped exactly once even when the same response
    appears in multiple sources' logs.
    """
    seen: set[str] = set()
    rows: list[UsageRow] = []
    for src in sources:
        rows.extend(src.scan(seen, date_from, date_to, tz))
    return rows


def _parse_claude_code_line(
    line: str,
    project: str,
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
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not usage:
        return None
    mid = msg.get("id")
    if not mid or mid in seen:
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
    seen.add(mid)
    return UsageRow(
        day=day.isoformat(),
        model=msg.get("model") or "?",
        project=project,
        source="claude-code",
        input=int(usage.get("input_tokens") or 0),
        output=int(usage.get("output_tokens") or 0),
        cache_create=int(usage.get("cache_creation_input_tokens") or 0),
        cache_read=int(usage.get("cache_read_input_tokens") or 0),
    )


GROUP_KEYS = ("day", "model", "project", "source")


def aggregate(
    rows: Iterable[UsageRow],
    group_keys: tuple[str, ...],
    rules: Iterable[PriceRule],
) -> dict[tuple[str, ...], Bucket]:
    rules_t = tuple(rules)
    buckets: dict[tuple[str, ...], Bucket] = defaultdict(Bucket)
    for r in rows:
        key = tuple(getattr(r, k) for k in group_keys)
        buckets[key].add(r, price_for(r.model, rules_t))
    return buckets
