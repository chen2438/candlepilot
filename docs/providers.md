# 决策 Provider

> 本专题是 [DOCS.md](../DOCS.md) 索引的权威文档之一。修改 Provider、模型路由、Prompt、
> 调用隔离、重试、试跑、Token 或成本行为时必须完整阅读并同步更新。

## 4.1 决策 Provider 接入

- **本地规则（`local-rule` / `trend-v2`）**：不调用外部 LLM，不新增行情采集，直接读取正式决策
  与普通历史回测都具备的多周期 K 线派生特征。5m/15m/30m/1h/4h EMA 方向按
  10%/15%/20%/25%/30% 加权；绝对分数至少 0.45，5m 必须触发、15m/30m 至少一个确认，
  1h/4h 不得同时反向，5m `return_1` 与 `return_5` 必须都和入场方向一致，5m `quote_volume_ratio`
  至少 0.8，且顺势 `ema20_distance_atr` 不得达到 2.5，才产生开仓。开仓固定请求 0.5% 风险、
  3 倍杠杆，以决策周期 ATR 的 1.5 倍止损、1.5R 止盈；已有仓位只在完整反向入场条件成立时
  `CLOSE`，否则 `HOLD`，当前版本不 `ADD/REDUCE`。缺少任一必需特征时明确 `HOLD` 并列出缺项。
  该策略忽略仅实盘存在的盘口、主动成交、基差和持仓量，保证普通回测与正式决策语义一致。
  每次计算仍完整审计输入、规则版本、结构化输出和理由；Token/成本均为 0。
  v2 只收紧双周期 5m 动量确认：现有正式运行回放的小样本中，四笔短长动量分歧的入场全部亏损；
  其他成交量、趋势分数、延伸、止损与止盈参数保持 v1，不把同一小样本内的额外筛选直接固化。

- **Codex Auth**：分别检测当前 ChatGPT App 的内置二进制
  （`/Applications/ChatGPT.app/...`）和 `PATH`/`~/.local/bin` 中的独立 `codex` CLI；默认优先
  ChatGPT App，但前端可在所有已安装来源之间明确切换，后续健康检查、测试和正式推理都只调用
  当前选中的二进制。两者均用 `codex exec --json --output-schema` 复用 ChatGPT/Codex 登录态。
  `codex login status` 只负责验证认证有效；登录邮箱从 `~/.codex/auth.json` 的本地 ID token 中仅
  解码 `email` 声明后返回，access token、refresh token、原始 ID token 和其他声明均不进入 API。
  独立 CLI 来源还可由控制台启动 `codex login --device-auth`、取消等待或二次确认后执行
  `codex logout`；命令始终由 CandlePilot 服务用户以固定参数、白名单环境和隔离临时目录执行，
  不经过 Shell。登录 API 仅从输出提取 OpenAI/ChatGPT HTTPS 授权地址和一次性代码，绝不返回
  原始输出或凭据；引擎、试跑、回测或其他登录流程活动期间禁止改变登录态。
- **Claude Code Auth**：依次检测 `PATH` 与 `~/.local/bin` 中的独立 `claude` CLI，
  复用 Claude.ai Pro/Max 登录态（`claude -p --output-format json --permission-mode default
  --max-turns 4 --disallowedTools …`，Prompt 走 stdin）。**不使用 plan 模式**（plan 模式会让
  模型调用 `ExitPlanMode` 或改为解释计划流程而非直接作答，耗尽单轮导致 `error_max_turns`）；
  Prompt 内联完整 `TradeIntent` JSON Schema（Claude 无 `--output-schema`，否则会臆造字段名）；
  Prompt 经 stdin 传入而非命令行参数（`--disallowedTools` 会贪婪吞掉后随的位置参数）。
- **Custom API（可多个）**：全部端点统一由 `CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON` 定义
  （JSON 数组，最多 8 个）。每项需唯一小写 `id` 与 `base_url`，可选 `api_key` / `model` /
  `reasoning_effort` / `wire_api` / `require_api_key` / `extra_headers` / `pricing`；各端点互相独立
  （各自的地址、密钥、模型与协议）。注册名 `openai-compatible:<id>`，主备路由中写
  `custom:<id>`（大小写不敏感）。**不存在"单个端点"的特例配置**：扁平的
  `CANDLEPILOT_CUSTOM_LLM_*` 变量已移除，若 `.env` 中仍存在非空值，启动会**直接报错**并提示
  改用 JSON 数组——静默忽略会让用户以为 Provider 还在。未知键、非法/重复 `id`、非法
  wire_api 或受保护请求头同样在启动时报错。
- 接入实现 OpenAI-compatible `/chat/completions` 或 `/responses` 的服务，默认 Chat
  Completions。Responses 请求使用 `input`、`store=false` 与嵌套 `reasoning.effort`，并从
  message 的 `output_text` 提取结果。两种协议都只发送统一 Prompt、不启用工具，并在本地严格
  校验返回的 `TradeIntent`；支持标准 token usage、缓存 token 与服务端可选返回的单次成本。
  Custom API 的可选推理强度为 `low` / `medium` / `high` / `xhigh` / `max`，所选值在 Chat
  Completions 中原样写入 `reasoning_effort`，在 Responses 中写入 `reasoning.effort`。通用适配器
  不注入厂商专属的 `thinking` 或 Anthropic 风格 `output_config`；例如 DeepSeek Chat Completions
  可接收上述强度，但显式启停思考仍由端点默认行为或厂商侧配置决定。
  外部地址必须 HTTPS，仅 `localhost`/`127.0.0.1`/`::1` 可用 HTTP；禁止 URL 内嵌凭据、query、
  fragment 和 HTTP 重定向。该 Provider 不主动探测 `/models`，连通性由前端「测试」验证。
- 统一交易 Prompt 明确说明 `PortfolioState.stop_loss_cooldown_until` 是最近 90 分钟发生手续费后
  净亏保护退出的兼容字段，而不再误写成仅包含固定止损；模型对映射内标的必须保持 `HOLD`，硬风控
  仍会独立复核。该语义变更对应 Prompt 版本 `trade-intent-v16`。
- **隔离与安全**：LLM 子进程运行在独立空临时目录，环境变量白名单
  （含 `USER`/`LOGNAME` 以支持 macOS 钥匙串读取 Claude 登录态），移除所有币安/API Key
  变量；禁用工具、MCP、网络；单 Provider 并发 1、统一取消。外部 Provider 的代码默认超时为
  45 秒，但正式运行由用户在每次启动时显式确认 1–600 秒的本次硬超时；该值在运行期间固化，
  停止后恢复 Provider 原配置。超时覆盖整次调用的绝对墙钟时间，不再只是 HTTP 连接/读写各阶段
  的空闲超时，因此端点即使间歇发送字节也不能无限占住调度周期。
- **API Key 边界**：Custom API Key 与额外请求头值只从启动环境读取并以 `SecretStr` 留在后端
  内存；不通过 REST/WebSocket 返回，不写入数据库、审计详情或日志。默认发送 Bearer Key；
  对 `requires_openai_auth=false` 一类服务可显式关闭并配置 JSON 自定义头。自定义头最多 16 个，
  禁止覆盖 Authorization、Host、Content-Type、Content-Length，且拒绝换行注入。Custom API
  作为用户显式配置的外部接收方会收到行情特征、组合状态和 Prompt，但不会收到币安凭据或
  其他环境变量。
- **Custom API 计费厂商（`pricing`）**：填 models.dev 的厂商 ID（如 `xai`、`openrouter`），
  前端端点表单提供候选列表（来自目录，也允许填目录未收录的值）。**这个值无法推断，只能声明**：
  同一模型被多家厂商转售，价格与结构都不同（`grok-4.5` 在 models.dev 下有十余家，
  xai 收 $2/$6 而 venice 收 $2.27/$6.8，缓存价与分层也不一致），而 OpenAI 兼容端点
  恰恰就是聚合器的典型场景，所以模型名和 base_url 都不能确定该按谁的价算。
  **留空则成本保持未知**（显示「—」）而不是猜一个看似合理的错数。
  注意：留空同时意味着**预算自动停止对该端点失效**（等效成本永远算不出，
  按"成本未知时绝不触发停止"的规则不会停）。
- **严格 Schema**：输出必须通过统一 `TradeIntent` Pydantic 校验，否则降级为 `HOLD`。
  批量结构化输出将 `TradeAction`、`OrderType` 等公共 `$defs` 固定放在整份 JSON Schema 根节点，
  确保 `intents.items` 内的根路径 `$ref` 可被 Codex/OpenAI 严格解析。
  `rationale` 是非交易关键解释字段，模型被要求尽量控制在 800 字符内，数据模型硬上限为
  1000 字符；若模型只违反该长度限制，Provider 会确定性截断到 1000 字符并在 usage 中写入
  `rationale_truncated=true`，同时完整原始输出仍留在本地审计。方向、杠杆、风险、价格与保护单
  等交易关键字段不做自动修正，任何不合规仍安全降级。
- **有序主备路由**：前端或 `CANDLEPILOT_PROVIDER_CHAIN` 可配置任意长度且不重复的 Provider
  顺序，例如 `local → codex → custom:main`。启动时并行检查整条路由，只要至少一个节点已
  就绪即可启动，并选择顺序最靠前的就绪节点承载。每个配置名必须对应实际注册的 Provider；失效
  引用会在保存或启动时明确拒绝，不会跳过节点或静默改选另一条实际路由。
- **周期批量分析**：正式运行按唯一选定的 cadence 收集全部候选与已有持仓的完整行情快照，
  再读取一份共同账户状态；外部 Provider 用**一次物理调用**接收 `markets` 数组与单份
  `portfolio`，并按输入顺序返回等长的 `intents` 数组。数量、顺序、标的或周期任一不匹配都视为
  整批协议失败，不能把意图错配给其他合约。本地规则仍逐项确定性计算但服从同一批量接口。
  模型返回后，每个意图仍独立落推理记录并逐标的执行硬风控；非 `HOLD` 在真正下单前继续重取
  该标的最新行情与最新账户，因此批量共享的是分析输入和模型调用，不是风控额度或执行状态。
  单次运行不再并行调度多个 cadence，避免同一 UTC 边界对相同标的重复调用和冲突决策。
- **故障冷却、同批次重试与恢复**：一轮分析按顺序尝试未冷却节点；调用失败的节点立即冷却
  60 秒，本轮继续尝试后续节点。整条可用路由都失败时，不等待下一个 K 线周期，而是在同一决策
  内按 5 秒、15 秒退避再试两轮；重试轮重新遍历完整主备路由，不让 60 秒的跨决策冷却吞掉本次
  恢复机会。每个新重试轮在退避结束后重新获取本批全部标的的完整行情快照和当前测试网组合，后续
  Provider 调用与最终时效检查都使用这份新输入，不能把新旧快照混进第二次模型调用，也不能让
  前一轮的模型耗时消耗下一轮的快照寿命；刷新失败则保留已发生的失败调用审计、中止本批决策且
  不下单。任一节点成功即结束重试、清零
  连续路由失败计数并恢复为承载节点；三轮仍全部失败
  才产生最终 `HOLD`。如果新决策开始时所有节点都在冷却，首轮先尝试最早到期的一个节点，后两轮
  仍遍历完整路由。路由调用用全局锁串行，达到失败阈值后排队中的其他标的不会继续烧调用；路由
  在引擎运行时也锁定，防止并发分析期间改变顺序。
- **切换审计**：每个实际发起但失败的 Provider 调用均单独写入推理审计，记录路由位置、当前是
  第几轮、是否继续切换/重试、错误、原始输出和可获得的 Token/耗时；最终成功结果再独立进入
  硬风控。三轮所有节点均失败时最后一次失败生成 `HOLD`，不会下单。
- **可选模型与推理强度**：Codex 传 `-m` / `-c model_reasoning_effort`，Claude 传
  `--model` / `--effort`。默认取自环境变量，也可在前端运行前经 `/api/providers/config`
  修改；前端模型为下拉选择（选项来自 models.dev 目录、按 Provider 过滤、含 CLI 别名），
  并保留「自定义」输入以支持目录外模型。前端选择「默认强度」会把运行时值清为 `None`：
  Codex/Claude 调用分别不追加 `-c model_reasoning_effort` / `--effort`，Custom API 的 Responses /
  Chat Completions 请求分别不发送 `reasoning` / `reasoning_effort`，最终默认值由 CLI、模型或端点决定，
  CandlePilot 不暗中代填某个档位。
- **配置连通性测试**：每个 Provider 可经前端「测试」按钮或 `POST /api/providers/test`
  用当前已应用的模型与推理强度发起一次合成快照调用，验证认证与配置能否返回 schema 合法的
  `TradeIntent`，并返回耗时、结果动作、该次调用报告的 Token 与等效成本。成本优先采用端点返回值，
  否则按所选 models.dev 计费厂商折算；端点未报告 usage 或没有可靠计费映射时分别明确显示
  「Token 未报告」/「成本未知」，不以零冒充。测试调用**不写入审计**（不污染决策/运行用量），
  引擎运行时锁定（返回 409）。
- **正式运行启动试跑**：持续调度前，用户可以先通过独立的 `POST /api/engine/probe` 试跑，再通过
  `POST /api/engine/start` 启动，也可以调用原子的 `POST /api/engine/probe-and-start`，以当前参数完成
  同一次试跑并仅在成功后自动启动；试跑失败时不会启动。`POST /api/engine/run-once` 不创建持续调度，因此无需试跑即可立即运行一个交易周期。试跑使用
  候选前 N 与已有持仓的去重并集、唯一所选周期的完整
  实时行情快照、当前测试网组合及当前完整 Provider 路由，对每个 Provider 执行 1 次真实的
  **全标的批量意图**。试跑不经过风控、不下单、不写入正式决策审计，但外部
  调用会产生真实 Token/计费。任一调用失败或超时即拒绝启动。系统取所有 Provider 中最慢的
  “全部行情 + 一份账户 + 一次批量意图生成”真实墙钟耗时；超过所选周期即返回
  422，要求减少标的或扩大周期。响应与 `/api/status.startup_probe` 保存本次耗时、最慢值、
  批量周期耗时和总负载。批量耗时不会按标的数线性外推，因此不伪造“最大安全标的数”。后端从读取首份真实快照开始即通过
  `/api/status.startup_probe` 发布已完成 Provider 数、批量标的列表/数量及每个 Provider 的本次
  具体结果：模型、推理强度、耗时、动作分布、输入/缓存/输出/总 Token、等效成本，以及逐标的
  `symbol/action/confidence`。端点未报告 Token 或无法可靠计价时明确返回空值，前端显示“Token
  未报告”或“成本未知”。首页将配置项明确标为“每周期候选标的数”，试跑进度按“候选 + 额外持仓
  = 分析标的 · 周期”显示三项数量，悬浮可查看完整标的列表，不再用批次
  首个标的冒充当前单标的；试跑请求尚未返回时通过状态 WebSocket 实时显示各 Provider 的
  等待/完成状态，完成后保留同一份详情并追加容量摘要；摘要将关键路径记为“批量分析耗时”。
  试跑成功只解锁一次持续启动，不会自动交易；周期、每周期候选标的数、运行上限、Provider 路由、模型配置、
  正式决策硬超时或候选池变化后立即失效，必须按新参数重新试跑。成功启动会消费本次试跑，停止后
  再次运行同样需要重新试跑。后端也强制检查该状态，不能绕过前端直接启动。
- **运行一次**：`POST /api/engine/run-once` 不等待下一个 K 线边界，立即使用唯一所选周期执行一轮
  完整流程：使用当前候选池（为空时先刷新）并重新读取账户、批量调用当前主备路由、逐标的硬风控，
  并对放行意图正常提交测试网订单
  与交易所保护单。该轮产生正式运行和决策审计，完成后自动停止，不创建后续 cadence 定时任务；
  它不依赖也不消费启动试跑，但仍执行 Provider 健康检查、启动账户对账、紧急锁检查，并采用请求中的
  正式决策硬超时。由于单次交易可能改变账户，已有的成功启动试跑会在单次运行开始后失效；
  已成交仓位保持测试网止损/止盈保护。仍未到价的本地限价意图会随本次运行停止而取消，不能在没有
  运行看护的状态下继续等待触发。运行期间测试网账户用户流会启动，结束后关闭；紧急熔断按钮保持
  可用，并会先取消仍在执行的单次模型/交易任务，再执行账户级紧急平仓。
- **失败调用审计**：Provider 已发起调用但在进程、网络、响应解析或 `TradeIntent` 校验阶段失败时，
  引擎仍降级为 `HOLD`，同时保留失败前已知的实际 Prompt、结构化输入、模型、真实耗时、token
  usage、版本指纹与安全原始输出，并单独记录错误信息。调用在 Prompt 完成前失败时允许显示
  「部分输入审计」，不伪造尚未形成的内容。
