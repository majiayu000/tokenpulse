# TokenPulse

一个可被现有 benchmark 导入的速度测量组件，同时提供只读的历史日志分析。支持 Codex rollout JSONL，以及通过 Desktop 会话元数据确认来源的 Claude transcript JSONL。Python 3.10+，仅使用标准库，不调用模型，不开启新对话。

## 接入已有 benchmark

`measurement.StreamMeasurement` 围绕现有 runner 的单次请求记录单调时钟；调用方喂入正文/可见思考增量，流结束取得最终 usage 后立即完成计时，再运行慢速输出验证，避免把评分时间计入请求耗时。可以测端到端耗时、首可观察输出、首正文等待、字符/token 吞吐及首 chunk 后字符速度。模型调用、题库与能力评分继续由原有 benchmark 负责。

所有尝试保留；速度汇总只使用非预热、正常完成、输出验证通过、耗时为正的样本。失败、错误输出和未验证输出分别报告，缺失数据保持未知。不同请求条件单独汇总，历史落盘与流式计时不会混合。

无模型调用的合成演示：

```bash
mkdir -p private-data
python3 -m examples.stream_demo > private-data/synthetic-stream.jsonl
python3 speed.py --stream private-data/synthetic-stream.jsonl \\
  --output reports/synthetic-stream.json
```

示例使用模拟时钟，数字不是模型实测。完整公式、接入代码、失败统计及限制见[速度测量约定](docs/speed-methodology.md)。`--stream` 不能与历史日志参数或 `--since` 混用。

## 先明确测量对象

现有日志可以回答「这些历史任务，在这个客户端里的实际输出吞吐是多少」。它们不能单独回答「同一个任务，哪个模型更快」。不同提示词、上下文、推理配置、缓存、客户端版本和服务档位，都会影响结果。

历史日志分析的指标是 **输入记录到最终输出记录的有效吞吐**：

```text
T = 最后一条完整 assistant 输出记录时间 − 当前用户输入记录时间
native token/s = Σ usage.output_tokens ÷ Σ T
visible character/s = Σ 可见正文 Unicode 字符数 ÷ Σ T
```

统一的分母包含输入记录之后的思考、请求等待与输出；排除输入记录之前的客户端准备，以及最终输出之后的 hook/收尾。这个边界与 Codex 原生 `task_complete.duration_ms` 不同，不能把两种结果混在一张榜单里。

客户端落盘和缓冲策略可能不同。日志无法证明所有隐藏思考都被覆盖；输入时间戳之前的工作在这个口径下不可观测，因此这是客户端有效吞吐估计，不是精确的模型生成速度。

默认只接受已完成的**人类问答且无工具调用**。自动任务通知、子代理、工具轮次、不完整轮次、非正耗时不进入默认报告。不尝试从总时间里减去估算的工具时间；直接选择没有工具的样本，避免并发工具与输出重叠导致分母过小。

两个指标的含义不同：原生 output token 包含思考，可见字符只包含最终正文。字符数为 Python `len(text)`，不是字节数、词数或屏幕字形数。使用同一份非重复文本输出时，可见字符/秒可避免不同模型原生 tokenizer 的单位差异；在不同历史任务之间，它仍不能成为严格排名。

报告不生成“最快模型”结论，也不把字符换算成所谓通用 token。

## 使用

```bash
git clone https://github.com/majiayu000/tokenpulse.git
cd tokenpulse
python3 -m unittest -v
python3 speed.py --help
```

分析选定的 Codex rollout 文件或目录：

```bash
python3 speed.py \
  --codex /path/to/codex/rollouts \
  --since 2026-10-01T00:00:00+08:00 \
  --output reports/codex.json
```

分析 Claude Desktop：

```bash
python3 speed.py \
  --claude "$HOME/.claude/projects" \
  --desktop "$HOME/Library/Application Support/Claude/claude-code-sessions" \
  --since 2026-10-01T00:00:00+08:00 \
  --output reports/claude.json
```

也可以同时提供两种来源，生成同一份报告。`--since` 依据**输入时间**筛选，必须带时区。跨过午夜的轮次按开始时间归入范围。

远程机器的记录先经 SSH/SCP 复制到本地 `private-data/`，再运行上述命令。只复制所需会话和 Desktop 元数据；不需要登录凭据或 auth 文件。工具本身不访问网络、不管理 SSH，也不修改原始记录。

Desktop 元数据只用于将 `cliSessionId` 对应到真实 transcript，防止混入独立 CLI 会话。CLI 日志目录可能是 `.claude` 或用户指定的配置目录；显式传入实际路径。

## 去重与边界

**Codex**：当前格式必须有 `token_usage_record.payload.response_id`。每个 response ID 的 usage 只保留最后一次记录。起点是 `item_completed / UserMessage` 的记录时间，终点是最后一条 `response_item / assistant message` 的记录时间；只有后续出现 `task_complete` 才算完成。模型、effort 和服务档位来自 `turn_context`，版本来自 `session_meta`。

**Claude**：每个 `message.id` 的累计输出 token 取最大值，不能把 thinking/text/tool 多个内容块携带的 usage 重复相加。可见正文按 `(message.id, apiBlockIndex)` 去重；没有 block index 时使用同条记录的 UUID。起点是明确标为 `origin.kind=human` 的输入记录，终点是最后一个 `stop_reason=end_turn` 回复的最后内容块时间。`stop_hook_summary` 用来分轮，但不计入耗时。缺少历史 effort 时报告 `null`，不拿当前 Desktop 设置冒充历史配置。

这是当前已验证日志格式的解析器，没有旧版本迁移或兼容层。缺少起点/终点或模型的记录无法形成样本；格式损坏、usage 缺少去重 ID 等错误会中止，不输出部分结果。跳过计数只统计已形成的样本，不覆盖每条被忽略的记录。样本的匿名 ID 由 response ID 哈希生成；报告不保留聊天、思考文本、原始 message ID 或来源路径。

同一来源、模型、effort、服务档位、客户端版本分别汇总。总量加权速度是 `Σ token / Σ 秒`，不是单次 token/s 的算术平均。报告同时给出样本数、耗时中位数/范围、可见字符/秒、输入 token 中位数及逐样本统计，便于发现少量异常样本。缺失的输入/缓存用量为 `null`，输入中位数附带已知样本数；重复 response ID 的内容冲突会报错。

退出码：`0` 有可用速度样本；`1` 没有符合条件的速度样本（仍写报告，保留流式尝试统计）；`2` 输入、解析或写入错误。报告原子写入，失败保留已有报告，输出不能覆盖输入；使用结果前应检查退出码。原始格式未知的记录不会被猜测成首 token 事件。

## 为什么不报告历史日志的“首 token 后速度”

Claude transcript 首条 assistant 记录通常对应一个完成的内容块，不能证明它是首 token 到达时刻。先生成的思考也可能没有文本呈现。若分子包含此前思考 token，而分母从可见正文才开始，就会夸大速度。

因此这个工具的 `first_token_speed` 始终是 `null`。同样，不会用 Codex 的 TTFT 字段对比 Claude 的内容块落盘时间。原来的首输出近似值和扣工具近似值只能用于探索，不再作为默认可比结果。

Claude 流式 usage 是累计值，思考计入 output token 总量；这也是去重与禁止重复添加思考 token 的依据。[Streaming messages](https://platform.claude.com/docs/en/build-with-claude/streaming)、[Steering thinking](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost)。公开榜单可能采用收到首 token 之后的 Output Speed，与本项目的全等待时间指标不同。[Artificial Analysis 方法说明](https://artificialanalysis.ai/methodology)。

## 改进后的受控实验方案

下面是实验操作协议。仓库提供历史分析和流式计时组件，**没有自动发请求的 runner、题库、能力评分或成本模块**。计时组件可以嵌入现有 benchmark，采样后的汇总可反复离线执行。

### 1. 固定任务与输出，再比较速度

至少分开两类任务，不混合汇总：

* **长文本输出**：生成并冻结一份固定种子的非重复文本，要求各模型原样输出同一内容。输出长度按字符数固定，核对完整文本/哈希，不用重复 32 遍同一句话代表普遍速度。报告实际生成内容的字符数和每个模型原生 token 数。
* **短答案推理**：同一道可核对答案的问题，输出格式和最大长度相同。主要比较完整回复耗时、首正文等待与正确率；极短答案的 token/s 不应主导结论。

同一份 prompt、system 指令、上下文和工具设置；禁用工具、子代理和自动追加消息。模型名称、推理设置、服务档位、max output、客户端/SDK 版本原样保存。不同供应商的 `medium` 不一定表示同等推理量，不把名称相同当成实验控制已经完成。

CLI 与 Desktop 可以比较使用体验；要测模型接口性能，应使用同一实验程序分别调用供应商流式 API，并披露接口和网络路径。不能把一个有缓存的长对话与另一个空上下文新会话混为受控试验。缓存状态与输入长度需要记录。

### 2. 正确记录流式边界

在同一进程中使用 monotonic 时钟：

| 事件 | 定义 |
|---|---|
| request start | 即将发送本次请求，计入网络/等待 |
| first observable output | 首次收到非空文本或可见思考 delta；空 message_start、签名和心跳不算 |
| first answer text | 首次收到非空正文 delta |
| last answer text | 最后一次非空正文 delta 到达 |
| response complete | 流结束且最终 usage 已收到 |

保留端到端时间为主指标。首正文等待可以单独报告；隐藏思考的真实首 token 不可见时标记未知。

若报告“首 chunk 后正文字符/秒”，分子必须只包含首 chunk 之后收到的正文字符，分母使用 first/last answer text 边界。只有一个 chunk 或零时间跨度时报告未知。网络 chunk 并非单个 token，不把这个指标命名为精确模型解码速度。

思考输出总量与首正文之后的时间不能交叉使用。流式 token 总量以最终 usage 为准。

### 3. 控制顺序、失败与不确定性

每轮每个模型各做一次，固定随机种子打乱顺序，串行执行，减少时段漂移与并发限流。预热请求单独列出，不偷偷混入或剔除。记录每次尝试：超时、拒绝、限流、截断、输出不符都计入失败数，不能只发布成功率高的一组。

3 次适合连接/流程检查；要作稳定性判断，需要更多轮次和不同时间段。不给 3 次数据制造有意义的置信区间或“已提速”结论。固定样本预算先完成；观察结果之后不挑选最好的一次、也不不停补测直到得到想要的排名。

报告至少列出：样本数/失败率、端到端耗时中位数与范围、总量加权原生 token/s、字符/s、首正文等待、thinking token、上下文和缓存信息。若输出内容未通过核对，速度与失败原因一起保留，但不纳入正确输出的速度汇总。

## 验证与数据范围

```bash
python3 -m compileall -q speed.py measurement.py test_speed.py test_measurement.py examples
python3 -m unittest -v
```

测试用合成日志与时钟覆盖累计 usage 去重、思考包含关系、正文块去重、工具排除、自动通知、未完成轮次、流式计时边界、输出校验、失败/预热、加权速度、报告隐私及原子写入错误。GitHub Actions 执行同样的命令和合成 CLI 示例，不访问模型服务。

仓库只包含代码、说明与合成测试。`private-data/`、`reports/`、JSONL、数据库和环境文件均被忽略；发布结果前仍应检查模型名、时间戳和可见配置是否适合披露。本项目不会自动收集或上传数据。
