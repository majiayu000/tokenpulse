# TokenPulse

[![CI](https://github.com/majiayu000/tokenpulse/actions/workflows/ci.yml/badge.svg)](https://github.com/majiayu000/tokenpulse/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Measure the wait. Count the output. Keep the boundaries honest.**

A read-only LLM throughput analyzer for Codex rollout logs and Claude Desktop transcripts, with a protocol for controlled comparisons. Python 3.10+, standard library only. It never sends model requests or starts conversations.

[中文说明及完整实验方案](README.zh-CN.md)

## What it measures

```text
elapsed = final assistant-message log timestamp − human-input log timestamp
weighted native token/s = sum(output_tokens) / sum(elapsed)
weighted visible character/s = sum(visible Unicode characters) / sum(elapsed)
```

Both clients use the same conceptual boundaries: input logged to final output logged. This includes thinking and waiting after input, and excludes setup before input and hooks after output. These are client log timestamps, not model-side generation timestamps. Logging and buffering can differ between clients.

The logs cannot prove that every hidden-thinking interval is covered. Work before the input timestamp is unobservable here; this metric is effective client throughput, not precise model generation speed.

Only completed human turns without tools enter the report. Tool turns, automatic notifications, subagents, incomplete turns and nonpositive durations are excluded. Tool time is never estimated and subtracted.

Native output tokens include thinking; thinking counts are a breakdown, never added again. Visible characters count text with Python `len`, excluding thinking and tool payloads. Native tokenizers differ, so native token/s alone cannot establish a cross-model ranking. Character/s is useful with identical output, but does not control differences between unrelated historical tasks.

**Historical logs measure past client experience. They are not a controlled model leaderboard.** Prompts, context, caching, effort, service tier and client versions matter. Missing metadata stays unknown.

## Quick start

```bash
git clone https://github.com/majiayu000/tokenpulse.git
cd tokenpulse
python3 -m unittest -v
python3 speed.py --help
```

Codex:

```bash
python3 speed.py \
  --codex /path/to/codex/rollouts \
  --since 2026-10-01T00:00:00+08:00 \
  --output reports/codex.json
```

Claude Desktop:

```bash
python3 speed.py \
  --claude "$HOME/.claude/projects" \
  --desktop "$HOME/Library/Application Support/Claude/claude-code-sessions" \
  --since 2026-10-01T00:00:00+08:00 \
  --output reports/claude.json
```

Pass both sources to produce one report. `--since` filters **input timestamps** and requires a timezone. Explicitly choose the actual log directories; Claude configuration directories can vary. Desktop metadata identifies transcripts by `cliSessionId`, excluding independent CLI sessions. It does not supply historical effort settings.

For remote logs, copy the needed transcripts and Desktop metadata locally with SSH/SCP into `private-data/` first. No auth files are needed. The analyzer does not manage SSH, access the network or modify input logs.

## Accounting and report

- **Codex:** requires `token_usage_record.payload.response_id`; keeps the last usage per response. Starts at the `item_completed / UserMessage` record and ends at the last assistant `response_item`. A subsequent `task_complete` is required. Uses `turn_context` settings and `session_meta` version.
- **Claude:** keeps the maximum cumulative usage per `message.id`, and counts text blocks once per `(message.id, apiBlockIndex)` or record UUID. Starts at explicit `origin.kind=human`; requires the last reply to finish with `stop_reason=end_turn`. Stop-hook timestamps are excluded. Effort comes from transcript records only.
- **Aggregation:** groups by source, model, effort, service tier and client version. Reports total-weighted rates, sample counts, elapsed median/range, median input tokens and individual samples. Duplicate response sets are excluded.

This parser targets the verified current log schemas; it has no old-format compatibility layer. Missing boundaries or models cannot form samples. Malformed records and missing deduplication IDs fail the run without writing a partial report. Skip counts cover formed samples, not every unparseable or ignored row.

Exit codes: **0** usable results; **1** no eligible samples (an empty report is written); **2** input/parse error (no new report is written). Existing output files are not removed on error; check the exit code before using a report.

Reports omit source paths, prompt/response/thinking text and raw message IDs. Sample IDs are hashes of response IDs. They still contain model names, timestamps and configuration metadata, so review them before publishing.

## First-token speed is unknown here

`first_token_speed` is always `null`. A transcript's first assistant record can be a completed content block, not the arrival of the first token. Hidden thinking may precede visible text. Dividing all output tokens by a duration that starts at visible text can overstate throughput.

Claude streaming usage is cumulative, and thinking tokens are included in output totals: [Streaming messages](https://platform.claude.com/docs/en/build-with-claude/streaming), [Thinking accounting](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost). Published post-first-token Output Speed uses a different denominator: [Artificial Analysis methodology](https://artificialanalysis.ai/methodology).

## Controlled experiment protocol

The first release implements **offline analysis**, not a live benchmark runner. The full protocol is in the [Chinese methodology](README.zh-CN.md#改进后的受控实验方案).

1. Freeze identical prompts, system instructions, context and tool settings. Record exact model/effort/tier/output limits, input/cache counts, client or SDK version and network route. Effort names across providers are not equivalent controls.
2. Separate fixed, nonrepetitive long-text output from short, verifiable reasoning tasks. Verify exact long-text output; report answer accuracy for reasoning. Do not pool the two workloads.
3. Instrument streaming with a monotonic clock: request send, first nonempty observable output, first/last answer text, and completion with final usage. Empty events, signatures and heartbeats are not output. Hidden first-token time remains unknown when unobservable.
4. Keep end-to-end time as the primary metric. For character/s after the first chunk, exclude that chunk's characters from the numerator and use first-to-last answer-text arrival time. One chunk or zero span yields unknown. A chunk is not a token; this is not precise decoder speed. Never divide hidden-thinking tokens by a visible-only interval.
5. Run serial, seeded, shuffled rounds. List warmups separately and retain all failures, limits, truncations and output mismatches. Use a fixed sample budget. Three runs are a smoke check, not evidence of a speed change. Publish counts, failure rate, elapsed median/range, weighted rates, first-answer wait, thinking and cache/context metadata.

Use the same API harness for interface comparisons; CLI/Desktop tests measure client experience. An experiment result applies to its tested configuration and time window.

## Development and data handling

```bash
python3 -m compileall -q speed.py test_speed.py
python3 -m unittest -v
```

Tests use synthetic logs and cover cumulative usage, thinking accounting, text deduplication, tool exclusion, notifications, incomplete turns, timing, weighted aggregation, report privacy and error handling. CI runs these checks without model access.

Real logs and generated reports stay out of Git: `private-data/`, `reports/`, JSONL, databases and environment files are ignored. No telemetry or automatic uploads.

See [contributing](CONTRIBUTING.md), [security reporting](SECURITY.md), and the [MIT license](LICENSE).
