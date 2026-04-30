# ccstats

Token usage and cost analyzer for [Claude Code](https://docs.anthropic.com/claude-code) session logs.

`ccstats` reads the JSONL session files Claude Code writes under `~/.claude/projects/`
and produces a per-day / per-model / per-project breakdown of input, output,
cache-write, and cache-read tokens — with an optional USD cost estimate based on
[Anthropic's public list pricing](https://www.anthropic.com/pricing).

```
$ ccstats --days 7
# Claude Code usage  2026-04-23 ~ 2026-04-30  (Asia/Shanghai)  group by: day
day         msgs   input  output  cache_w  cache_r   total      ≈cost
---------------------------------------------------------------------
2026-04-23  1753  250.6K    1.0M    45.8M   277.8M  324.9M  $1355.56
2026-04-24   601   51.0K  439.0K     9.5M   279.7M  289.7M    $631.24
...
TOTAL       6317    2.5M    4.2M    99.8M    1.45B   1.56B  $4367.07
```

Works on macOS and Linux (any platform Claude Code itself runs on). No third-party
dependencies — pure Python standard library, requires Python 3.11+.

## Install

With [`uv`](https://github.com/astral-sh/uv) (recommended):

```sh
uv tool install ccstats
```

With [`pipx`](https://pipx.pypa.io/):

```sh
pipx install ccstats
```

With plain pip:

```sh
pip install --user ccstats
```

## Usage

```sh
ccstats                              # last 7 days, grouped by day
ccstats --days 30                    # last 30 days
ccstats --from 2026-04-01            # since a specific day
ccstats --from 2026-04-01 --to 2026-04-15

ccstats --group model                # break down by model
ccstats --group project              # by working directory
ccstats --group day,model            # nested: day then model

ccstats --json                       # JSON output (pipe to jq, etc.)
ccstats --watch 5                    # live refresh every 5 seconds

ccstats --tz UTC                     # change timezone (default Asia/Shanghai)
ccstats --tz +0                      # numeric offsets work too
```

## How it works

Claude Code writes one JSONL file per session under
`~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`. Each assistant turn
contains the **server-reported** token usage:

```json
{
  "message": {
    "id": "msg_…",
    "model": "claude-opus-4-7",
    "usage": {
      "input_tokens": 6,
      "output_tokens": 724,
      "cache_creation_input_tokens": 51914,
      "cache_read_input_tokens": 0
    }
  }
}
```

`ccstats`:

1. Walks every `*.jsonl` under the log directory.
2. Deduplicates by Anthropic's globally unique `message.id` — the same response
   may appear in several files due to session resume / rewind / sub-agents, but
   each API call is counted exactly once.
3. Buckets entries by the local date of their `timestamp` (configurable
   timezone) and by the requested `--group` keys.
4. Multiplies token counts by the configured USD price per million tokens.

Because cache hit/miss is decided server-side and reported back in the same
`usage` object, the cache-read / cache-write columns reflect the actual billed
behavior — not a client-side guess.

## Pricing configuration

Default pricing covers Anthropic's public list price for the Opus, Sonnet, and
Haiku families. To override these rates or add prices for other models (e.g.
when routing through Vertex with a negotiated rate, or using third-party models
like Qwen / GLM / Kimi), drop a TOML file at `~/.config/ccstats/pricing.toml`:

```toml
# Match by case-insensitive substring against the model id. First match wins,
# so put more specific entries first. User entries always win over defaults.

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
```

Use `--pricing-file PATH` or `CCSTATS_PRICING=PATH` to point at a different file.

## Environment variables

| Variable           | Purpose                              | Default                |
|--------------------|--------------------------------------|------------------------|
| `CCSTATS_LOG_DIR`  | Claude Code projects log directory   | `~/.claude/projects`   |
| `CCSTATS_TZ`       | Timezone for day buckets             | `Asia/Shanghai`        |
| `CCSTATS_PRICING`  | Pricing override TOML path           | `~/.config/ccstats/pricing.toml` |

## Caveats

- This reports what your **client** logged. If you also use Claude Code on
  another machine or container, those sessions aren't included unless you
  point `--log-dir` at a shared location.
- USD cost is an **estimate** based on list price. Vertex / Bedrock / enterprise
  contracts may price differently — check your cloud provider's billing report
  for authoritative numbers.
- Models without a configured price are still counted in the token columns but
  contribute `-` (or `*` in totals) in the cost column.

## License

MIT — see [LICENSE](LICENSE).
