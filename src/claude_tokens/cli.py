"""Command-line entry point for claude-tokens."""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from claude_tokens import __version__
from claude_tokens.core import (
    DEFAULT_LOG_DIR,
    GROUP_KEYS,
    ClaudeCodeSource,
    Source,
    aggregate,
    collect,
)
from claude_tokens.format import render_json, render_table
from claude_tokens.openclaw import DEFAULT_OPENCLAW_DIR, OpenClawSource
from claude_tokens.pricing import default_config_path, load_rules


ENV_LOG_DIR = "CLAUDE_TOKENS_LOG_DIR"
ENV_TZ = "CLAUDE_TOKENS_TZ"
ENV_PRICING = "CLAUDE_TOKENS_PRICING"
ENV_OPENCLAW_DIR = "CLAUDE_TOKENS_OPENCLAW_DIR"


def parse_tz(name: str) -> tuple[timezone, str]:
    """Return (tzinfo, display name). Accepts UTC, Asia/Shanghai, +N, -N, +HH:MM."""
    if name.upper() == "UTC":
        return timezone.utc, "UTC"
    if name == "Asia/Shanghai":
        return timezone(timedelta(hours=8)), "Asia/Shanghai"
    if name and name[0] in "+-":
        sign = 1 if name[0] == "+" else -1
        body = name[1:]
        if ":" in body:
            h, m = body.split(":", 1)
            return timezone(sign * timedelta(hours=int(h), minutes=int(m))), name
        return timezone(sign * timedelta(hours=int(body))), name
    raise argparse.ArgumentTypeError(
        f"unknown timezone {name!r} (use UTC, Asia/Shanghai, or +N / -N / +HH:MM)"
    )


def parse_iso_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-tokens",
        description="Token usage and cost analyzer for Claude Code and OpenClaw session logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  claude-tokens                              # last 7 days, by day\n"
            "  claude-tokens --days 30                    # last 30 days\n"
            "  claude-tokens --from 2026-04-01 --to 2026-04-15\n"
            "  claude-tokens --group model                # by model\n"
            "  claude-tokens --group day,model            # nested\n"
            "  claude-tokens --json                       # JSON output\n"
            "  claude-tokens --watch 5                    # live refresh every 5s\n"
            "\nEnv overrides: "
            f"{ENV_LOG_DIR}, {ENV_TZ}, {ENV_PRICING}, {ENV_OPENCLAW_DIR}"
        ),
    )
    p.add_argument("--version", action="version", version=f"claude-tokens {__version__}")
    p.add_argument("--days", type=int, default=7, help="last N days (default 7, ignored if --from set)")
    p.add_argument("--from", dest="date_from", type=parse_iso_date, help="start date YYYY-MM-DD (inclusive)")
    p.add_argument("--to", dest="date_to", type=parse_iso_date, help="end date YYYY-MM-DD (inclusive)")
    p.add_argument(
        "--group",
        default="day",
        help=f"comma-separated keys: {', '.join(GROUP_KEYS)} (default: day)",
    )
    p.add_argument(
        "--tz",
        default=os.environ.get(ENV_TZ, "Asia/Shanghai"),
        help="timezone for day buckets (default Asia/Shanghai or $%s)" % ENV_TZ,
    )
    p.add_argument(
        "--log-dir",
        type=Path,
        default=Path(os.environ.get(ENV_LOG_DIR, str(DEFAULT_LOG_DIR))),
        help="Claude Code projects log directory (default ~/.claude/projects or $%s)" % ENV_LOG_DIR,
    )
    p.add_argument(
        "--pricing-file",
        type=Path,
        default=Path(os.environ[ENV_PRICING]) if os.environ.get(ENV_PRICING) else None,
        help="TOML file overriding/extending pricing (default ~/.config/claude-tokens/pricing.toml)",
    )
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    p.add_argument(
        "--watch",
        nargs="?",
        const=5,
        type=int,
        metavar="SECONDS",
        help="re-render every N seconds (default 5); Ctrl-C to stop",
    )
    return p


def resolve_dates(args, tz: timezone) -> tuple[date, date]:
    today = datetime.now(timezone.utc).astimezone(tz).date()
    if args.date_from:
        date_from = args.date_from
    else:
        date_from = today - timedelta(days=args.days)
    date_to = args.date_to or today
    if date_from > date_to:
        raise SystemExit(f"--from {date_from} is after --to {date_to}")
    return date_from, date_to


def parse_group(value: str) -> tuple[str, ...]:
    keys = tuple(k.strip() for k in value.split(",") if k.strip())
    bad = [k for k in keys if k not in GROUP_KEYS]
    if bad:
        raise SystemExit(f"unknown --group key(s): {bad}. Valid: {list(GROUP_KEYS)}")
    if not keys:
        raise SystemExit("--group must not be empty")
    return keys


def build_sources(args) -> list[Source]:
    sources: list[Source] = [ClaudeCodeSource(args.log_dir)]
    oc_dir = Path(os.environ.get(ENV_OPENCLAW_DIR, str(DEFAULT_OPENCLAW_DIR)))
    if oc_dir.is_dir():
        sources.append(OpenClawSource(oc_dir))
    return sources


def render_once(args, tz: timezone, tz_name: str, group_keys: tuple[str, ...], pricing_path: Path | None) -> str:
    date_from, date_to = resolve_dates(args, tz)
    sources = build_sources(args)
    present = [s for s in sources if s.exists()]
    if not present:
        return "no log sources found (need ~/.claude/projects or ~/.openclaw/agents)"
    rules = load_rules(pricing_path)
    rows = collect(sources, date_from, date_to, tz)
    buckets = aggregate(rows, group_keys, rules)
    source_names = [s.name for s in present]
    if args.json:
        return render_json(buckets, group_keys, date_from, date_to, tz_name)
    return render_table(buckets, group_keys, date_from, date_to, tz_name, source_names)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tz, tz_name = parse_tz(args.tz)
    group_keys = parse_group(args.group)
    pricing_path = args.pricing_file or default_config_path()

    if args.watch is None:
        print(render_once(args, tz, tz_name, group_keys, pricing_path))
        return 0

    interval = max(1, int(args.watch))
    if sys.platform == "win32":
        os.system("")  # enable VT processing in legacy cmd.exe; no-op elsewhere
    try:
        while True:
            sys.stdout.write("\x1b[2J\x1b[H")  # clear screen + home cursor
            sys.stdout.write(render_once(args, tz, tz_name, group_keys, pricing_path))
            sys.stdout.write(
                f"\n\n[watch every {interval}s, last refresh {datetime.now().strftime('%H:%M:%S')} — Ctrl-C to stop]\n"
            )
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        return 0
