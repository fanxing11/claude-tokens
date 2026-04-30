"""Pricing tables and per-model price lookup.

Defaults are Anthropic public list prices (USD per 1M tokens) at the time of
writing. Users can override or extend via a TOML config file at
``~/.config/ccstats/pricing.toml`` (or ``$XDG_CONFIG_HOME/ccstats/pricing.toml``)
or by passing ``--pricing-file`` on the command line.

Config file format::

    # Match by substring (case-insensitive). First match wins.
    # Order matters — put more specific keywords first.
    [[models]]
    match = "claude-opus-4-7"
    input = 15.0
    output = 75.0
    cache_create = 18.75
    cache_read = 1.5

    [[models]]
    match = "qwen3"
    input = 1.0
    output = 4.0
    cache_create = 1.0
    cache_read = 0.1
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - we require 3.11+
    raise RuntimeError("ccstats requires Python 3.11+")


@dataclass(frozen=True)
class Price:
    """Price per 1M tokens for the four token classes."""

    input: float
    output: float
    cache_create: float
    cache_read: float


@dataclass(frozen=True)
class PriceRule:
    match: str  # case-insensitive substring matched against model name
    price: Price


# Anthropic public list pricing (USD per 1M tokens) — keyword-matched.
DEFAULT_RULES: tuple[PriceRule, ...] = (
    PriceRule("opus", Price(15.0, 75.0, 18.75, 1.5)),
    PriceRule("sonnet", Price(3.0, 15.0, 3.75, 0.3)),
    PriceRule("haiku", Price(0.8, 4.0, 1.0, 0.08)),
)


def default_config_path() -> Path:
    """Per-OS user config path.

    - Linux/BSD: $XDG_CONFIG_HOME/ccstats/pricing.toml or ~/.config/ccstats/pricing.toml
    - macOS: ~/Library/Application Support/ccstats/pricing.toml
    - Windows: %APPDATA%/ccstats/pricing.toml
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ccstats" / "pricing.toml"


def load_rules(path: Path | None = None) -> tuple[PriceRule, ...]:
    """Load pricing rules. User rules take precedence over defaults."""
    user_rules = _load_user_rules(path) if path else _load_user_rules(default_config_path())
    # User rules first so they win the "first match" lookup.
    return tuple(user_rules) + DEFAULT_RULES


def _load_user_rules(path: Path) -> list[PriceRule]:
    if not path.is_file():
        return []
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    rules: list[PriceRule] = []
    for entry in data.get("models", []):
        try:
            rules.append(
                PriceRule(
                    match=str(entry["match"]),
                    price=Price(
                        input=float(entry["input"]),
                        output=float(entry["output"]),
                        cache_create=float(entry["cache_create"]),
                        cache_read=float(entry["cache_read"]),
                    ),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid pricing entry in {path}: {entry!r} ({exc})")
    return rules


def price_for(model: str, rules: Iterable[PriceRule]) -> Price | None:
    """Return the Price whose match keyword appears in ``model`` (case-insensitive)."""
    m = (model or "").lower()
    for rule in rules:
        if rule.match.lower() in m:
            return rule.price
    return None


def cost_usd(price: Price, input_t: int, output_t: int, cache_create_t: int, cache_read_t: int) -> float:
    """Compute USD cost given token counts and a Price (per 1M)."""
    return (
        input_t * price.input
        + output_t * price.output
        + cache_create_t * price.cache_create
        + cache_read_t * price.cache_read
    ) / 1_000_000
