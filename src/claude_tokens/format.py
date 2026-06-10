"""Output formatting: human-readable table and JSON."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from typing import Iterable

from claude_tokens.core import Bucket


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}K"
    return str(n)


def fmt_cost(b: Bucket) -> str:
    if b.cost == 0 and not b.cost_known:
        return "-"
    s = f"${b.cost:.2f}"
    return s + ("*" if not b.cost_known else "")


def render_table(
    buckets: dict[tuple[str, ...], Bucket],
    group_keys: tuple[str, ...],
    date_from: date,
    date_to: date,
    tz_name: str,
    sources: list[str] | None = None,
) -> str:
    lines: list[str] = []
    src_suffix = f"  sources: {', '.join(sources)}" if sources else ""
    lines.append(
        f"# AI CLI token usage  {date_from} ~ {date_to}  ({tz_name})"
        f"  group by: {', '.join(group_keys)}{src_suffix}"
    )
    if not buckets:
        lines.append("(no data in range)")
        return "\n".join(lines)

    headers = list(group_keys) + ["msgs", "input", "output", "cache_w", "cache_r", "total", "≈cost"]

    sort_by_key = group_keys[0] == "day"
    items = list(buckets.items())
    if sort_by_key:
        items.sort(key=lambda kv: kv[0])
    else:
        items.sort(key=lambda kv: -kv[1].total)

    grand = Bucket()
    body: list[list[str]] = []
    for key, b in items:
        body.append(
            list(key)
            + [
                str(b.msgs),
                fmt_tokens(b.input),
                fmt_tokens(b.output),
                fmt_tokens(b.cache_create),
                fmt_tokens(b.cache_read),
                fmt_tokens(b.total),
                fmt_cost(b),
            ]
        )
        grand.msgs += b.msgs
        grand.input += b.input
        grand.output += b.output
        grand.cache_create += b.cache_create
        grand.cache_read += b.cache_read
        grand.cost += b.cost
        if not b.cost_known:
            grand.cost_known = False

    body.append(
        ["TOTAL"]
        + [""] * (len(group_keys) - 1)
        + [
            str(grand.msgs),
            fmt_tokens(grand.input),
            fmt_tokens(grand.output),
            fmt_tokens(grand.cache_create),
            fmt_tokens(grand.cache_read),
            fmt_tokens(grand.total),
            fmt_cost(grand),
        ]
    )

    widths = [max(len(row[i]) for row in [headers] + body) for i in range(len(headers))]
    fmt_parts = [
        "{:<" + str(w) + "}" if i < len(group_keys) else "{:>" + str(w) + "}"
        for i, w in enumerate(widths)
    ]
    fmt_str = "  ".join(fmt_parts)
    sep = "-" * (sum(widths) + 2 * (len(widths) - 1))

    lines.append(fmt_str.format(*headers))
    lines.append(sep)
    for row in body[:-1]:
        lines.append(fmt_str.format(*row))
    lines.append(sep)
    lines.append(fmt_str.format(*body[-1]))
    lines.append("")
    lines.append("* costs cover models with public Anthropic pricing (opus/sonnet/haiku);")
    lines.append("  '*' next to a total means rows used unknown-priced models (excluded).")
    lines.append("  Override or extend pricing via ~/.config/claude-tokens/pricing.toml.")
    return "\n".join(lines)


def render_json(
    buckets: dict[tuple[str, ...], Bucket],
    group_keys: tuple[str, ...],
    date_from: date,
    date_to: date,
    tz_name: str,
) -> str:
    out = []
    for key, b in buckets.items():
        entry: dict = dict(zip(group_keys, key))
        entry["msgs"] = b.msgs
        entry["input"] = b.input
        entry["output"] = b.output
        entry["cache_create"] = b.cache_create
        entry["cache_read"] = b.cache_read
        entry["total_tokens"] = b.total
        entry["cost_usd_estimate"] = round(b.cost, 4) if b.cost_known else None
        out.append(entry)
    return json.dumps(
        {
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
            "tz": tz_name,
            "group": list(group_keys),
            "rows": out,
        },
        indent=2,
        ensure_ascii=False,
    )
