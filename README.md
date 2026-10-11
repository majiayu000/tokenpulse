# TokenPulse

[![CI](https://github.com/majiayu000/tokenpulse/actions/workflows/ci.yml/badge.svg)](https://github.com/majiayu000/tokenpulse/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Measure the wait. Count the output. Keep the boundaries honest.**

A speed measurement component for LLM benchmarks, plus read-only analysis of Codex rollout logs and Claude Desktop transcripts. Python 3.10+, standard library only. It never sends model requests or starts conversations.

[中文说明及完整实验方案](README.zh-CN.md)

## Integrate with an existing benchmark

Use `measurement.StreamMeasurement` around a single request in your existing runner. Feed unique text/thinking deltas, then finish immediately with final usage. Apply slow output verification afterwards so scoring time does not enter request latency. It captures request-relative monotonic timestamps without storing text. The runner owns model calls and scoring.

The component reports end-to-end latency, first observable output, first-answer wait, weighted token/character rates and character throughput after the first chunk. Warmups, failures, mismatches and unverified outputs remain visible; only verified, completed, positive-duration measurements enter speed summaries. Missing token usage stays unknown. Chunk timing is not token timing.

Try the **synthetic** example without calling a model:

```bash
mkdir -p private-data
python3 -m examples.stream_demo > private-data/synthetic-stream.jsonl
python3 speed.py --stream private-data/synthetic-stream.jsonl \\
  --output reports/synthetic-stream.json
```

These example numbers use a fake clock and do not measure any model. See the [measurement formulas and integration contract](docs/speed-methodology.md). `--stream` reports are separate from historical logs and cannot be combined with log options or `--since`.

## What it measures

```text
elapsed = final assistant-message log timestamp − human-input log timestamp
weighted native token/s = sum(output_tokens) / sum(elapsed)
weighted visible character/s = sum(visible Unicode characters) / sum(elapsed)
```

Both clients use the same conceptual boundaries: input logged to final output logged. This includes thinking and waiting after input, and excludes setup before input and hooks after output. These are client log timestamps, not model-side generation timestamps. Logging and buffering can differ between clients.

The logs cannot prove that every hidden-thinking interval is covered. Work before the input timestamp is unobservable here; this metric is effective client throughput, not precise model generation speed.

In historical-log mode, only completed human turns without tools enter the report. Tool turns, automatic notifications, subagents, incomplete turns and nonpositive durations are excluded. Tool time is never estimated and subtracted.

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

Unknown input/cache counts remain `null`; input medians report their known-sample coverage. Conflicting duplicate response sets fail rather than silently keeping one. Historical wall-clock reports and monotonic stream reports are explicitly labeled and never pooled.

This parser targets the verified current log schemas; it has no old-format compatibility layer. Missing boundaries or models cannot form samples. Malformed records and missing deduplication IDs fail the run without writing a partial report. Skip counts cover formed samples, not every unparseable or ignored row.

Exit codes: **0** usable speed samples; **1** no eligible speed samples (a report is still written, including stream attempt diagnostics); **2** input/parse/write error. Reports are written atomically and cannot overwrite input files. Existing output files are preserved on error; check the exit code before using a report.

Reports omit source paths, prompt/response/thinking text and raw message IDs. Sample IDs are hashes of response IDs. They still contain model names, timestamps and configuration metadata, so review them before publishing.

## First-token speed is unknown here

`first_token_speed` is always `null`. A transcript's first assistant record can be a completed content block, not the arrival of the first token. Hidden thinking may precede visible text. Dividing all output tokens by a duration that starts at visible text can overstate throughput.

Claude streaming usage is cumulative, and thinking tokens are included in output totals: [Streaming messages](https://platform.claude.com/docs/en/build-with-claude/streaming), [Thinking accounting](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost). Published post-first-token Output Speed uses a different denominator: [Artificial Analysis methodology](https://artificialanalysis.ai/methodology).

## Controlled experiment protocol

The repository provides offline analysis and an importable stream measurement component. It does not implement model calls, a task bank, capability scoring or cost estimates. Use your existing benchmark runner; the full protocol and formulas are in the [measurement contract](docs/speed-methodology.md) and [Chinese methodology](README.zh-CN.md#改进后的受控实验方案).

1. Freeze identical prompts, system instructions, context and tool settings. Record exact model/effort/tier/output limits, input/cache counts, client or SDK version and network route. Effort names across providers are not equivalent controls.
2. Separate fixed, nonrepetitive long-text output from short, verifiable reasoning tasks. Verify exact long-text output; report answer accuracy for reasoning. Do not pool the two workloads.
3. Instrument streaming with a monotonic clock: request send, first nonempty observable output, first/last answer text, and completion with final usage. Empty events, signatures and heartbeats are not output. Hidden first-token time remains unknown when unobservable.
4. Keep end-to-end time as the primary metric. For character/s after the first chunk, exclude that chunk's characters from the numerator and use first-to-last answer-text arrival time. One chunk or zero span yields unknown. A chunk is not a token; this is not precise decoder speed. Never divide hidden-thinking tokens by a visible-only interval.
5. Run serial, seeded, shuffled rounds. List warmups separately and retain all failures, limits, truncations and output mismatches. Use a fixed sample budget. Three runs are a smoke check, not evidence of a speed change. Publish counts, failure rate, elapsed median/range, weighted rates, first-answer wait, thinking and cache/context metadata.

Use the same API harness for interface comparisons; CLI/Desktop tests measure client experience. An experiment result applies to its tested configuration and time window.

## Development and data handling

```bash
python3 -m compileall -q speed.py measurement.py test_speed.py test_measurement.py examples
python3 -m unittest -v
```

Tests use synthetic logs and clocks. They cover cumulative usage, thinking accounting, text deduplication, tool exclusion, notifications, incomplete turns, stream boundaries, output verification, failures/warmups, weighted aggregation, privacy and atomic error handling. CI also runs the synthetic CLI example without model access.

Raw logs and unreviewed generated reports stay out of Git: `private-data/`, `reports/`, JSONL, databases and environment files are ignored. Reviewed experiment summaries may be archived separately after removing conversations, credentials, private configuration, local paths and raw message/session IDs. No telemetry or automatic uploads.

## Archived experiments

- [2026-10-12: GPT-6.1 Sol, ordinary and Fast settings through Codex CLI](experiments/2026-10-12-codex-sol61-fast/README.md). Three runs per setting, using the historical input-to-final-output log boundary. This is a small client-experience observation, with unknown actual served tiers and differing cache/context counts, not a model leaderboard.

See [contributing](CONTRIBUTING.md), [security reporting](SECURITY.md), and the [MIT license](LICENSE).
