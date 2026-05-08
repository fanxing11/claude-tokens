"""Core: scan Claude Code session logs, dedupe, aggregate."""

from __future__ import annotations

import glob
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from claude_tokens.pricing import Price, PriceRule, cost_usd, price_for


DEFAULT_LOG_DIR = Path.home() / ".claude" / "projects"


@dataclass
class UsageRow:
    day: str       # YYYY-MM-DD in caller-supplied tz
    model: str
    project: str
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


def project_name_from_log_path(path: str) -> str:
    """Decode ~/.claude/projects/-home-neolix-code-foo -> /home/neolix/code/foo.

    Claude Code encodes the project cwd by replacing ``/`` with ``-``. This
    decoder is best-effort: a path segment that legitimately contained ``-``
    can't be perfectly reversed, but the result is still a stable, human-readable
    identifier good enough for grouping.
    """
    base = os.path.basename(os.path.dirname(path))
    if base.startswith("-"):
        # Leading dash represents the leading slash of an absolute path.
        return "/" + base[1:].replace("-", "/")
    return base


def iter_log_files(log_dir: Path, file_cutoff: datetime) -> Iterator[str]:
    """Yield jsonl files under ``log_dir`` with mtime >= cutoff."""
    pattern = str(log_dir / "*" / "*.jsonl")
    for f in glob.glob(pattern):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(f), timezone.utc)
        except OSError:
            continue
        if mtime >= file_cutoff:
            yield f


def collect(
    log_dir: Path,
    date_from: date,
    date_to: date,
    tz: timezone,
) -> list[UsageRow]:
    """Scan logs and return one UsageRow per unique Anthropic message id in range."""
    file_cutoff = datetime.combine(date_from, datetime.min.time(), tzinfo=tz) - timedelta(days=2)
    seen: set[str] = set()
    rows: list[UsageRow] = []

    for f in iter_log_files(log_dir, file_cutoff):
        proj = project_name_from_log_path(f)
        try:
            fh = open(f, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                row = _parse_line(line, proj, date_from, date_to, tz, seen)
                if row is not None:
                    rows.append(row)
    return rows


def _parse_line(
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
        input=int(usage.get("input_tokens") or 0),
        output=int(usage.get("output_tokens") or 0),
        cache_create=int(usage.get("cache_creation_input_tokens") or 0),
        cache_read=int(usage.get("cache_read_input_tokens") or 0),
    )


GROUP_KEYS = ("day", "model", "project")


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
