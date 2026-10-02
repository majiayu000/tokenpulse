# 速度测量与接入约定

TokenPulse 是同一个 benchmark 仓库里的速度组件。它负责采样和计算，不调用模型、不出题、不判定模型综合能力、不计算费用。能力评测可使用自己的题库和 verifier，复用这里的计时组件。

## 两种证据分别报告

| 来源 | 起点与终点 | 能说明什么 | 不能说明什么 |
|---|---|---|---|
| `retrospective-log` | 用户输入落盘 → 最终 assistant 输出落盘 | 历史客户端有效吞吐 | 精确请求开始、首 token、完整隐藏思考时间 |
| `client-stream` | 发送请求前的采样点 → 流结束且取得最终 usage | 当前 harness 观察到的请求耗时、首正文等待、正文 chunk 吞吐 | 服务端排队/推理时间拆分、不可见思考首 token、精确解码速度 |

CLI 禁止把两种证据混进同一份报告。日志模式用带时区的壁钟时间；流式模式的耗时只使用同一进程的 `time.perf_counter_ns()` 差值。`started_at` 仅用于描述测试发生的时间窗口，不参与耗时计算。纳秒存储不等于纳秒级测量准确度。[Python 时钟文档](https://docs.python.org/3/library/time.html#time.perf_counter_ns)

## 流式指标的确切公式

设请求采样点为 `t0`，首个非空可观察输出为 `to`，首个非空正文 delta 为 `tf`，最后一个非空正文 delta 为 `tl`，流结束并取得最终 usage 为 `tc`。

| 字段 | 公式 / 含义 |
|---|---|
| `end_to_end_seconds` | `tc − t0`，包含等待、可见或隐藏思考、输出、usage 尾部等待 |
| `first_observable_output_seconds` | `to − t0`；可见思考或正文均可，隐藏思考时不推断 |
| `first_answer_seconds` | `tf − t0`；首正文等待，不命名为真实首 token 时间 |
| `answer_span_seconds` | `tl − tf`；不含首正文之前的等待和最后正文之后的尾部 |
| `end_to_end_native_tokens_per_second` | 原生 `output_tokens / (tc − t0)`，包含思考的 token 总量 |
| `end_to_end_characters_per_second` | 正文 Unicode 字符数 `/ (tc − t0)` |
| `after_first_chunk_characters_per_second` | `(正文总字符数 − 首 chunk 字符数) / (tl − tf)` |
| `answer_chunk_gaps_ns` | 相邻非空正文 delta 的到达间隔；不是逐 token 间隔 |

只有一个正文 chunk、没有正文或 `tl == tf` 时，首 chunk 后速度为 `null`。空 delta、角色声明、签名、心跳和只含 usage 的事件不触发首输出。必须按唯一事件喂入增量文本，不能反复喂累计正文。

分子和分母必须属于同一区间。全部思考 token 不能除以首正文之后的时间；首 chunk 的字符不能算进首 chunk 之后的速度。`first_token_speed` 仍为 `null`。

原生 tokenizer 不同，不把跨供应商原生 token/s 当成相同单位。正文字符数是 Python `len`，不是字节或屏幕字形。固定输出内容、语言和长度时，字符吞吐更容易对比。

vLLM 也指出：不同工具的指标名称不一定对应同一公式，而且一次流式输出可包含多个 token。本项目按客户端 chunk 的可观察事实命名，不把 chunk 当单个 token。[vLLM 指标说明](https://github.com/vllm-project/vllm/blob/main/docs/benchmarking/cli.md#understanding-the-latency-metrics)

## 接入现有 benchmark

```python
from measurement import StreamMeasurement, fingerprint, summarize_stream

# 以下是接入位置示意。你的现有 runner 负责请求与供应商事件解析。
# 不包含 API 调用实现，执行本仓库不会自动发请求。
request_key = fingerprint({
    "messages": canonical_messages,  # 完整 system、历史和本次输入
    "tools": [],
    "max_output_tokens": max_output_tokens,
    "sampling": sampling_settings,
    "harness_concurrency": 1,
})
meter = StreamMeasurement(
    model, workload_id, request_key,
    effort=effort, service_tier=service_tier,
    client_version=client_version, route=route_label,
    expected_text=reference_text,  # 固定文本任务；其他任务可交给 verifier
)
# 紧接着发送一个请求；关闭 SDK 的隐藏重试。
# 每个唯一正文 delta 调用 meter.text(delta)。
# 每个非空可见思考 delta 调用 meter.thinking(delta)。
# 所有正文喂入后，等待流完成和最终 usage，立即 finish。
# 慢速 verifier 必须在 finish 之后运行，避免把评分时间算进模型耗时。
record = meter.finish(
    output_tokens=final_output_tokens,  # 已包含思考，不要再次相加
    thinking_tokens=final_thinking_tokens,
    input_tokens=total_input_tokens,  # 已包含缓存命中和缓存创建输入
    cached_input_tokens=cache_read_tokens,
    completed=stream_completed_normally,
    stop_reason=stop_reason,
)
# finish 已核对固定文本的哈希；需要其他能力评分时，再运行原有 verifier。
# 没有 expected_text 的任务，可以在评分后设置：
# record['valid_output'] = verifier_result  # True / False / None
# 若同时使用哈希检查和 verifier，两者都通过才标为 True。
# 将 record 即时追加到你自己的 JSONL；之后可以反复离线汇总。
report = summarize_stream([record])
```

每次网络尝试单独创建 meter。异常时调用 `meter.finish(completed=False, error=exception)` 并保存结果；它只保存异常类名，不保存可能含凭证的错误正文。截断、工具交接、拒绝等状态由了解接口语义的 runner/verifier 处理，不靠一张供应商别名表猜测。发生异常后不要隐式重试同一个 meter；下一次尝试是另一条记录。

耗时截止点必须早于能力评分、写盘和报告生成。`finish(valid_output=...)` 仅用于已经得到的验证结果，例如增量验证器；不能为了先取得这个参数而推迟完成采样。固定文本哈希核对发生在完成时间采样之后。SDK 缓冲、JSON 解析、回调和客户端调度仍会影响观察值，不能据此分解服务端真实推理耗时。

`fingerprint` 使用排序键的紧凑 JSON 和 UTF-8 SHA-256。参与哈希的是去掉凭证的规范化请求条件；不同供应商的 payload 应先由现有 benchmark 转成同一份语义输入。组件无法证明你传入的 fingerprint 包含所有实验条件。模型、effort、服务档位、客户端版本和线路标签另行分组。线路使用不含凭证的描述标签，不保存带 token 的 URL。

提供 `expected_text` 时，按所有正文 delta 拼接后的 UTF-8 SHA-256 精确核对，保留空白和 Unicode 原样，不进行去代码块或 Unicode 归一化。若另有 verifier 判失败，哈希相同也不能覆盖失败。没有固定文本且未传入 verifier 结果时，输出为未验证状态。

## 失败、预热与汇总

所有尝试保留在 `samples`，包括失败、错误输出、未验证输出和预热。**速度汇总只采用非预热、正常完成、输出已验证、耗时为正的样本。** 有效输出检查是速度测量的资格检查，不代表已经实现能力评分。

- `attempts` 不含显式标记的预热；`completed` 表示流正常完成。
- `failed` / `failure_rate` 统计未正常完成的尝试；输出错误与未验证分别列为 `invalid_outputs` / `unverified_outputs`。不能把流完成率解释成答案正确率。
- `samples` 是速度可用数；`valid_completions` 是通过输出验证的正常完成数。零耗时单列，避免除零。
- token/s 只使用有最终 token 用量的速度样本：`Σ output_tokens / Σ 对应耗时`，并报告 `token_usage_samples`。缺失用量不当作零，不把无用量样本的时间放进这个分母。
- 字符/s 使用全部可用速度样本；首 chunk 后字符/s 仅使用正文跨度为正的样本：`Σ 首 chunk 后字符 / Σ 对应正文跨度`，同时报告覆盖数。
- 报告保留上述公式的分子与分母：`known_output_tokens` / `token_usage_elapsed_seconds`、`visible_characters` / `elapsed_seconds`、`after_first_chunk_characters` / `answer_span_seconds`。可以直接复算，无需从小数速度反推总量。
- 耗时和首正文等待报告中位数、经验 P95；chunk 间隔 P95 汇集各样本的正文 chunk 间隔，单独报告间隔数。
- P95 使用 nearest-rank：排序后第 `ceil(0.95 × n)` 个值。它是描述统计，不是置信区间；小样本 P95 常等于最大值。3 次仍只适合流程检查。
- 相同 attempt ID 的同内容副本去重；内容冲突时报错。不同请求 fingerprint、任务、模型及配置分别汇总，不自动生成“最快模型”排名。

`Σ token / Σ 请求耗时` 是请求加权的有效速度。并发情况下它不是 `总 token / 整轮墙钟时长` 的系统吞吐；本组件不做服务端吞吐或负载压测。

## 可重复实验的要求

先冻结题目、参考输出、提示词、上下文、生成限制和评分依据，再采样。使用同一 harness 和明确的网络路径；同名 effort 不代表同等思考量。固定预算、预先决定轮次，串行执行并用固定种子打乱模型顺序，保留所有失败和重试。预热单列，不根据结果删除慢样本。

原生 input/cache token 逐次记录，不推断未知缓存状态。固定文本和长上下文可能产生缓存复用；明确报告缓存条件，不能把缓存实验当作无缓存性能。vLLM 的官方指南同样提醒，重复提示词可能因前缀缓存让吞吐看起来更高。[缓存注意事项](https://github.com/vllm-project/vllm/blob/main/docs/benchmarking/cli.md#-online-benchmark)

速度与能力应分别解释：固定文本输出用于吞吐；短答案任务主要看正确性和完整任务耗时。能力/工具任务的总耗时由原有 benchmark 记录，不从中扣估算的工具时间，也不混入本项目无工具历史日志的汇总。

## 无费用的验证方式

```bash
mkdir -p private-data
python3 -m examples.stream_demo > private-data/synthetic-stream.jsonl
python3 speed.py --stream private-data/synthetic-stream.jsonl \
  --output reports/synthetic-stream.json
python3 -m unittest -v
```

示例使用合成文本和模拟时钟，**数字不是任何模型的实测速度**。测试不调用模型。

CLI 的 `--stream` 不与日志参数或 `--since` 混用。退出码保持 `0` 有可用速度样本、`1` 无可用速度样本但仍写报告、`2` 输入/解析/写入错误。报告先完整写入同目录临时文件，再替换目标；失败时保留旧报告。输出不能覆盖输入文件或其符号链接指向的源文件。读取时检查退出码，避免误用旧报告。

原始会话和生成报告默认不入 Git。组件不保存正文、思考文本、原始 provider message ID、请求 payload 或完整异常信息。输出哈希、任务标识、模型配置、时间窗口和线路标签仍是元数据，分享前需要检查。历史日志缺少起点/终点或模型时不能形成样本；其跳过计数不是所有请求的失败率。
