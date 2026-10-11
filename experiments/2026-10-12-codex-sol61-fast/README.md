# GPT-6.1 Sol: ordinary and Fast settings through Codex CLI

This archive contains six reviewed measurements made on **2026-10-12, 00:21–00:24 Asia/Shanghai**. All model requests were issued by **Codex CLI 0.162.1**. No custom HTTP client or Python model-request runner was used. Node supervised the CLI and calculated statistics offline from its persisted logs.

The machine-readable [results](results.json) contain metrics and the synthetic benchmark prompt. Raw conversations, full CLI logs, login material, private configuration, local paths and raw message/session IDs are excluded.

## Results

Rates below are **native output token/s, including thinking tokens**. The denominator is the time from the human-input log record to the final assistant-output log record, as defined by TokenPulse's [historical-log measurement contract](../../docs/speed-methodology.md). It is client effective throughput, not precise decoder speed or a post-first-token rate.

| Requested CLI setting | Run 1 | Run 2 | Run 3 | Total-weighted token/s | Mean log elapsed |
|---|---:|---:|---:|---:|---:|
| Ordinary (`default`) | 43.73 | 33.47 | 41.77 | **39.16** | 14.734 s |
| Fast (`priority`) | 62.36 | 63.68 | 65.76 | **63.90** | 8.957 s |

Weighted rates are calculated from totals, not an arithmetic average of the three individual rates:

```text
Ordinary: 1,731 output tokens / 44.203 seconds = 39.1602 token/s
Fast:     1,717 output tokens / 26.871 seconds = 63.8979 token/s
```

The Fast-requested group had **63.17% higher weighted throughput in this sample**. Three runs per setting are a smoke check. This observation does not establish a persistent speedup, isolate the effect of service tier, or measure complex coding performance.

## Workload and execution

Both settings used `gpt-6.1-sol` at `high` reasoning effort in fresh, independent Git working directories. The prompt required the following sentence exactly 32 times, one line per repetition, with no numbering, introduction, conclusion or code fence:

> A small wooden boat crossed the quiet lake while morning sunlight slowly warmed the distant hills.

All six outputs matched the expected text exactly. Each contained 550 visible output tokens; hidden thinking is included in the native output totals. There were no tool calls, warmups, failed calls or retries. Requests ran serially in this order:

```text
ordinary → Fast → ordinary → Fast → ordinary → Fast
```

The installed CLI was invoked with this command, using stdin for the prompt:

```sh
codex --no-daemon -a never exec --ignore-user-config --json \
  -s read-only -C <INDEPENDENT_GIT_WORKDIR> -m gpt-6.1-sol \
  -c 'model_reasoning_effort="high"' \
  -c 'service_tier="default"' \
  -c project_doc_max_bytes=0 -c 'developer_instructions=""' -
```

Fast runs replaced `service_tier="default"` with `service_tier="priority"`. Each call had a 150-second supervising timeout. The runner used Codex's existing login without reading or copying its authentication files. Global configuration was not changed. The CLI-managed system/context prefix was not archived; the complete request payload was not independently frozen.

## Accounting and verification

- Start: the timestamp of the `event_msg / item_completed / UserMessage` record.
- End: the timestamp of the final `response_item / message / assistant` record. A subsequent `task_complete` was required.
- Output usage: the last `token_usage_record` per response ID, summed without counting thinking twice. Every call had one response.
- Aggregation: total native output tokens divided by total corresponding log elapsed seconds.
- Validation: completed call, exact 32-line output, no tools, and the requested model and reasoning effort confirmed by the log.
- Offline calculation check: the Node implementation reproduced a previous TokenPulse sample exactly: `567 / 11.425 = 49.62800875273523 token/s`. It reused the formula, not the Python implementation.

All six calls used **131,376 input tokens**, including **35,072 cached input tokens**, and **3,448 output tokens**, including **148 thinking tokens**. The archived JSON preserves the individual counts and timestamps so totals can be recalculated without publishing raw logs.

## Limits and unknowns

- **Actual served tier is unknown.** `default` and `priority` are the requested CLI settings. The native rollout did not record a service tier or expose a backend served-tier receipt; the corresponding fields remain `null`.
- Context and cache were not constant. Inputs ranged from 21,636 to 22,156 tokens, with 0 to 13,568 cached tokens per call. The results do not isolate caching or client-prefix effects.
- The underlying network route was not instrumented and remains unknown. The recorded times include client-visible waiting and network delivery within the log boundary.
- The order was alternating, not randomized. There were no warmups, and only one short time window was sampled.
- Repeating one short sentence is a fixed regression workload, not a representative long-text, reasoning or coding benchmark.
- The earlier reference runs used CLI 0.160.0; this archive used 0.162.1. Shared workload and accounting do not remove client-version differences.
- Native task duration, process startup time and first-output fields are not mixed into the primary log-based metric. Exact model-side generation timing and hidden first-token timing are unknown.

An earlier direct-interface experiment used a different workload, effort and timing boundary. Its samples are excluded from this CLI archive and from every statistic above.
