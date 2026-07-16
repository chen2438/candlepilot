# CandlePilot 功能文档

> 本文件是 CandlePilot 的**唯一权威功能文档**，记录系统当前的全部能力、接口与边界。
> `STATUS.md` 与 `PLAN.md` 已弃用，后续变更只同步更新本文件。
> 最后更新：2026-07-16（LLM 输入数据审计与修正）

---

## 1. 概述

CandlePilot 是一个**本地优先、可审计**的 LLM 日内交易系统，当前专注于币安
USDⓈ-M USDT 永续合约。LLM 分析市场并提出结构化 `TradeIntent`，**确定性风控拥有最终否决权**。

- 仅支持：历史回测、生产行情模拟成交、币安官方 Demo 测试网。
- **没有真钱实盘入口。**
- 工程验收标准是无前视偏差、可复现、可审计、风险边界有效、故障可恢复——不以盈利为验收条件。

## 2. 运行模式与安全边界

三种互斥模式（`CANDLEPILOT_MODE`）：

| 模式 | 值 | 说明 |
|---|---|---|
| 历史回测 | `backtest` | 事件驱动回放，无实时下单 |
| 模拟成交 | `paper-production-data`（默认）| 真实生产行情 + 本地模拟撮合 |
| 测试网 | `binance-testnet` | 币安官方期货测试网签名下单 |

安全边界：

- 只监听 localhost（非 `127.0.0.1/localhost/::1` 的绑定地址会被拒绝启动）。
- 币安密钥、任何敏感环境变量**永不传入 LLM 子进程**。
- LLM 子进程禁用工具、文件、Shell、网络；单轮、硬超时、单并发。
- 测试网 Broker 硬性拒绝生产交易地址；凭证不写入数据库和日志。
- 真钱实盘、美股、新闻/社交/链上数据均**不实施**。

## 3. 架构

- 后端：Python 3.12、FastAPI、Pydantic、SQLAlchemy、SQLite（WAL）。
- 前端：React + TypeScript + Vite，白色浅色主题；REST + WebSocket。
- 存储：SQLite 保存业务与审计数据；Parquet 保存大容量历史行情。
- 适配器接口预留扩展点：`MarketDataProvider`、`BrokerAdapter`、`LLMProvider`、
  `FeaturePipeline`、`DecisionEngine`、`RiskPolicy`。
- 单进程异步，不依赖 Redis/Celery。

## 4. 功能详解

### 4.1 LLM 接入

- **Codex Auth**：优先检测当前 ChatGPT App 与旧版 Codex App 的内置二进制
  （`/Applications/ChatGPT.app/...`、`/Applications/Codex.app/...`），不可用时依次回退到
  `PATH` 和 `~/.local/bin` 中的独立 `codex` CLI。用 `codex exec --json --output-schema`
  复用 ChatGPT/Codex 登录态。
- **Claude Code Auth**：依次检测 `PATH` 与 `~/.local/bin` 中的独立 `claude` CLI，
  复用 Claude.ai Pro/Max 登录态（`claude -p --output-format json --permission-mode default
  --max-turns 4 --disallowedTools …`，Prompt 走 stdin）。**不使用 plan 模式**（plan 模式会让
  模型调用 `ExitPlanMode` 或改为解释计划流程而非直接作答，耗尽单轮导致 `error_max_turns`）；
  Prompt 内联完整 `TradeIntent` JSON Schema（Claude 无 `--output-schema`，否则会臆造字段名）；
  Prompt 经 stdin 传入而非命令行参数（`--disallowedTools` 会贪婪吞掉后随的位置参数）。
- **Custom API（可多个）**：全部端点统一由 `CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON` 定义
  （JSON 数组，最多 8 个）。每项需唯一小写 `id` 与 `base_url`，可选 `api_key` / `model` /
  `reasoning_effort` / `wire_api` / `require_api_key` / `extra_headers`；各端点互相独立
  （各自的地址、密钥、模型与协议）。注册名 `openai-compatible:<id>`，主备路由中写
  `custom:<id>`（大小写不敏感）。**不存在"单个端点"的特例配置**：扁平的
  `CANDLEPILOT_CUSTOM_LLM_*` 变量已移除，若 `.env` 中仍存在非空值，启动会**直接报错**并提示
  改用 JSON 数组——静默忽略会让用户以为 Provider 还在。未知键、非法/重复 `id`、非法
  wire_api 或受保护请求头同样在启动时报错。
- 接入实现 OpenAI-compatible `/chat/completions` 或 `/responses` 的服务，默认 Chat
  Completions。Responses 请求使用 `input`、`store=false` 与嵌套 `reasoning.effort`，并从
  message 的 `output_text` 提取结果。两种协议都只发送统一 Prompt、不启用工具，并在本地严格
  校验返回的 `TradeIntent`；支持标准 token usage、缓存 token 与服务端可选返回的单次成本。
  外部地址必须 HTTPS，仅 `localhost`/`127.0.0.1`/`::1` 可用 HTTP；禁止 URL 内嵌凭据、query、
  fragment 和 HTTP 重定向。该 Provider 不主动探测 `/models`，连通性由控制台「测试」验证。
- **隔离与安全**：LLM 子进程运行在独立空临时目录，环境变量白名单
  （含 `USER`/`LOGNAME` 以支持 macOS 钥匙串读取 Claude 登录态），移除所有币安/API Key
  变量；禁用工具、MCP、网络；45 秒硬超时、单 Provider 并发 1、统一取消。
- **API Key 边界**：Custom API Key 与额外请求头值只从启动环境读取并以 `SecretStr` 留在后端
  内存；不通过 REST/WebSocket 返回，不写入数据库、审计详情或日志。默认发送 Bearer Key；
  对 `requires_openai_auth=false` 一类服务可显式关闭并配置 JSON 自定义头。自定义头最多 16 个，
  禁止覆盖 Authorization、Host、Content-Type、Content-Length，且拒绝换行注入。Custom API
  作为用户显式配置的外部接收方会收到行情特征、组合状态和 Prompt，但不会收到币安凭据或
  其他环境变量。
- **严格 Schema**：输出必须通过统一 `TradeIntent` Pydantic 校验，否则降级为 `HOLD`。
  `rationale` 是非交易关键解释字段，模型被要求尽量控制在 800 字符内，数据模型硬上限为
  1000 字符；若模型只违反该长度限制，Provider 会确定性截断到 1000 字符并在 usage 中写入
  `rationale_truncated=true`，同时完整原始输出仍留在本地审计。方向、杠杆、风险、价格与保护单
  等交易关键字段不做自动修正，任何不合规仍安全降级。
- **有序主备路由**：控制台或 `CANDLEPILOT_PROVIDER_CHAIN` 可配置任意长度且不重复的 Provider
  顺序，例如 `codex → claude-code → openai-compatible`。启动时并行检查整条路由，只要至少一个
  节点已就绪即可启动，并选择顺序最靠前的就绪节点承载。
- **故障冷却与恢复**：一次分析按顺序尝试未冷却节点；调用失败的节点立即冷却 60 秒，本次继续
  尝试后续节点。冷却到期后自动回到优先顺序参与下一次调用，成功即恢复为承载节点；如果所有
  节点都在冷却，系统会尝试最早到期的一个节点，避免整个决策周期静默丢失。路由在引擎运行时
  锁定，防止并发分析期间改变顺序。
- **切换审计**：每个实际发起但失败的 Provider 调用均单独写入推理审计，记录路由位置、是否继续
  切换、错误、原始输出和可获得的 Token/耗时；最终成功结果再独立进入硬风控。所有节点均失败时
  最后一次失败生成 `HOLD`，不会下单。
- **可选模型与推理强度**：Codex 传 `-m` / `-c model_reasoning_effort`，Claude 传
  `--model` / `--effort`。默认取自环境变量，也可在控制台运行前经 `/api/providers/config`
  修改；控制台模型为下拉选择（选项来自 models.dev 目录、按 Provider 过滤、含 CLI 别名），
  并保留「自定义」输入以支持目录外模型。
- **配置连通性测试**：每个 Provider 可经控制台「测试」按钮或 `POST /api/providers/test`
  用当前已应用的模型与推理强度发起一次合成快照调用，验证认证与配置能否返回 schema 合法的
  `TradeIntent`，并返回耗时与结果动作。测试调用**不写入审计**（不污染决策/用量），引擎运行时
  锁定（返回 409）。
- **失败调用审计**：Provider 已发起调用但在进程、网络、响应解析或 `TradeIntent` 校验阶段失败时，
  引擎仍降级为 `HOLD`，同时保留失败前已知的实际 Prompt、结构化输入、模型、真实耗时、token
  usage、版本指纹与安全原始输出，并单独记录错误信息。历史上只写入部分详情的失败记录在控制台
  标为「部分输入审计」，只有完全不存在详情行的旧记录才显示「输入审计启用前」。

### 4.2 行情与数据

- 币安 REST：合约发现、交易规则、K 线、资金费率、盘口、24h ticker、持仓量。
- 币安 WebSocket：K 线、mark price、盘口、ticker、账户/订单用户数据流；
  自动重连、心跳、去重、乱序处理、断线后 REST 回补。
- 历史下载：K 线与资金费率分页拉取；资金费率按实际结算 K 线映射。
- 本地缓存：历史 K 线以精确区间存为 Parquet（可在数据管理中清除）。

### 4.3 选币与特征

- 动态全市场扫描 USDT 永续，按上市时间、完整度、价差、成交额、波动率、趋势过滤排名；
  启动即扫，之后每分钟自动刷新轮换，每周期最多向 LLM 提交 5 个候选。
- 特征：1m/5m/15m/30m 共享 EMA、RSI、ATR、收益率、成交量，以及 20/50 根区间高低点与
  区间内位置（`range_position_50`，0 为区间底、1 为区间顶）；另有基差、持仓量、
  20 档盘口与近期成交失衡等微观结构特征。每个周期获取最近 200 根 K 线、排除未收盘 K 线
  后计算合计 60 个快照特征；LLM 接收特征值而不是原始 K 线数组。
- 区间高低点是模型判断"价格是否已延伸""是否收复/拒绝参考位"的**唯一依据**——均线只说方向、
  不说位置。Prompt 逐条点名哪个字段回答哪个入场条件，并要求：若某形态所需证据不在 payload 中，
  该形态即视为不成立。
- 每周期特征只带一次前缀（`5m_rsi_14`），不再同时给出无前缀副本，避免同一读数被当成两份独立证据。
- `recent_trade_imbalance` 取自定量条数的成交流水，覆盖时长随标的活跃度变化，
  因此同时给出 `recent_trade_seconds`；Prompt 要求把过短的窗口当噪声而非订单流。
- EMA 以前 `period` 个值的均值做种子并跑满全部已获取历史，避免窗口首值的噪声残留在读数里。
- 5m/15m/30m 对齐调度；每周期分析候选池前 N 个标的并额外包含全部已有持仓，确保掉出候选池的
  仓位仍会获得主动 `HOLD/ADD/REDUCE/CLOSE` 决策；同标的跨周期串行评估，禁止相反方向
  并发开仓，同向增仓须显式 `ADD`。
- **可选分析周期**：用户可自由选择分析 5m/15m/30m 的任意子集（默认全部）；1m 不触发
  LLM 决策，但继续作为多周期特征输入和模拟持仓实时盯市周期；
  默认取自 `CANDLEPILOT_CADENCES`，也可在控制台运行前经 `POST /api/cadences` 修改，
  运行时锁定。只有被选中的周期会启动调度任务。
- **每周期标的数**：每个周期只分析候选池排名前 N 的标的，N 可配置（默认 5，范围 1–20）；
  默认取自 `CANDLEPILOT_CANDIDATES_PER_CYCLE`，也可在控制台运行前经
  `POST /api/candidates-per-cycle` 修改，运行时锁定。

### 4.3.1 运行看护与自动停止

调度器有一个独立于决策周期的看护任务（默认每 5 秒轮询），命中任一条件即**优雅停止**引擎
（停止产生新决策、保留现有持仓；测试网持仓仍由交易所侧止盈止损括号单保护），并把原因写入
`/api/status` 的 `auto_stop_reason`，控制台顶部横幅展示。独立轮询是必要的：否则 30m 周期会把
一次到期停止拖延半小时。停止原因不会阻止重新启动，`start` 时自动清空。

- **运行上限**（启动前设定，运行时锁定；`POST /api/run-limits`，默认取自
  `CANDLEPILOT_MAX_RUN_SECONDS` / `CANDLEPILOT_MAX_RUN_COST_USD`）：
  - **时长**：本次运行超过 N 秒即停。
  - **预算**：本次运行的**等效成本**（复用 `/api/metrics/run-session` 的
    `equivalent_cost_usd`）达到 N 美元即停。订阅制（Codex/Claude）实际不按次计费，此处为按
    API 标准价的折算估算，仅作护栏；成本未知时**绝不**触发停止。
  - 两者均可留空表示不限；先到者触发。
- **路由耗尽**：当主备池中每个 provider 都失败（`PROVIDER_FAIL` / 路由耗尽）时开始计时，
  持续 `ROUTE_EXHAUSTION_STOP_AFTER`（默认 180 秒，约 3 个冷却周期）仍未恢复即停止，避免
  在全部 provider 不可用时空转烧调用。任一次成功即清零计时。

### 4.4 决策与风控

- `TradeIntent`：标的、周期、`HOLD/OPEN_LONG/OPEN_SHORT/ADD/REDUCE/CLOSE`、置信度、
  杠杆、风险比例、订单类型、入场价、止损、止盈、有效期、理由。
- `confidence` 表示模型认为当前快照下，所提交的**非 HOLD 动作具备可执行交易优势**的估计，
  不是盈利概率、回答正确率或风控放行权。`HOLD` 时它只表示残余机会强度，通常低于 0.55；
  模型决策策略只在 `confidence >= 0.55` 且存在明确失效价时提交开仓/加仓，但该阈值不替代、
  不削弱下游硬风控。`HOLD` 必须输出 `leverage=1`、`risk_fraction=0`、`order_type=MARKET`，
  并将入场价、止损和止盈设为空。
- 模型入场策略要求满足以下一种明确形态：5m 与至少一个 15m/30m 周期同向且价格未明显延伸、
  成交量/订单流不显著冲突；高周期趋势未破坏的回调企稳或关键参考位收复，并有参与度恢复；
  或结构收复/拒绝与动量/订单流共同确认的反转。单独超买/超卖不构成反转确认。已有持仓只有
  在同等入场条件下才能 `ADD`，触及失效条件或反向证据确认时使用 `REDUCE/CLOSE`；其他情况
  保持 `HOLD`，不得为了提高交易频率虚增置信度。
- **持仓保护价可见**：控制台账户页持仓表的「保护」列在测试网模式下显示**真实的**止损/止盈
  触发价（来自 `openAlgoOrders` 回读，1 秒 memo 与账户查询共用刷新节奏），某一腿缺失时该腿
  显示「缺失」；两腿都读不到时回退为 `protection_source` 状态词（交易所侧/缺失/待确认）。
  `protection_source` 仍是**对账**信号而非价格信号——它额外统计 reduce-only 止损，
  而价格回读只认 `closePosition` 括号单（即本系统自己下的那种）。
- **持仓上下文**：`portfolio.positions` 逐标的给出方向、数量、入场价、未实现盈亏、杠杆，
  以及当前**挂在交易所上的**止损/止盈价——该止损即这笔仓位的失效价。测试网侧的保护单价格
  每次都从 `openAlgoOrders` 回读而非沿用下单时的记忆值，因为括号单可能在本进程之外被成交、
  撤销或改价。没有 `REDUCE/CLOSE` 判断所需的失效价，模型只能靠猜。
- 硬风控（不可由模型修改）：逐仓、单向净仓、最高 10x、单笔计划亏损 ≤ 权益 2%、
  最多 8 个仓位、总保证金占用 ≤ 60%；日内净亏损 8% 触发熔断。
- 开仓必须带交易所侧止损；仓位按止损距离、费用、保守滑点反推。
- **价格对齐**：模型自定的止损/止盈/限价均不知道交易所精度，因此由风控统一按 `PRICE_FILTER`
  的 `tickSize` 吸附到价格网格上（`tickSize` 经 `exchangeInfo` 进入 `SymbolRules`）。
  保护价一律**远离入场**取整、限价一律朝**自己一侧**取整，因此吸附只会放宽而不会把某个价位
  收紧穿过它刚通过校验的那个价格。若某价格向下取整到 0，直接否决该笔交易而不是发出无意义的括号单。
- **测试网模式额外要求开仓/加仓同时带止盈**（缺止盈直接否决）；无论何种模式，只要给了
  止盈都会校验方向（多单止盈须高于入场、空单须低于入场）。
- LLM 使用周期开始时的完整特征快照分析；非 `HOLD` 意图返回后，执行路径必须重新获取最新
  行情并刷新账户/模拟持仓，再用新标记价完成硬风控、定量和执行。市价单忽略模型建议的入场价，
  始终按刷新行情定量；若刷新失败或最新价已越过止损/止盈，统一审计为风控否决且不下单。
  限价单因行情变化已可立即成交时仍可在全部最新风控通过后执行，放行原因会追加
  `limit entry is immediately marketable after refresh` 审计标记；保证金数量使用刷新后的买一/卖一
  与限价中更保守的价格计算。`HOLD` 无订单，不因分析快照变旧产生伪否决。
- 分析快照从采集到 LLM 返回的最大年龄默认 75 秒，可通过
  `CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS` 调整；超过上限会在刷新前否决。刷新后的行情仍须通过
  同一有效期检查。另拒绝余额不足、无止损、强平缓冲不足、不符合交易所精度的意图。
- 每次新推理都审计结构化行情/组合输入、实际 Prompt、Provider 原始输出和 token usage；控制台
  在单条决策展开后按需加载“AI 分析详情”，显示输入/缓存输入/输出/总 token、按标准 API
  定价折算的单次等效成本，并允许复制输入、Prompt 和输出。升级前的旧记录仍可查看已有输出与
  usage，但精确输入不会被推测或补造；所有详情只经 localhost API 提供且不包含 Provider 或币安凭据。

### 4.5 执行（模拟 / 测试网）

- **模拟撮合**：行情驱动的限价单、止损、止盈与持续盯市；现金/仓位/挂单/成交持久化，
  重启后恢复并继续实时保护；紧急平仓。
- **测试网**：签名下单、逐仓、杠杆设置、入场成交后通过 Binance Algo Order API
  (`POST /fapi/v1/algoOrder`) 同时挂**保护止损 `STOP_MARKET` 与止盈
  `TAKE_PROFIT_MARKET`**（`algoType=CONDITIONAL`、均 `closePosition`、按标记价触发）；任一保护单
  挂单失败即紧急减仓平掉并记录执行失败；部分成交后撤销入场余量；状态未知订单不重复下单；429/418 退避重试；
  时间戳错误自动重同步。`ADD` 增仓采用两阶段保护替换：先读取旧 CandlePilot 括号，增仓成交后
  先挂妥新的止损和止盈，再通过 Algo Order API 逐单撤销旧括号；人工订单不会被删除，替换期间
  始终至少有一组保护。紧急停止同时撤销普通挂单与 Algo 条件单。
- **执行审计**：风控放行不等于下单成功。每次需要下单的放行决策都会写入独立执行尝试，区分
  `SUCCEEDED`、`FAILED`、`RESCUED`、`UNKNOWN`，并记录失败阶段、交易所错误码、入场成交与紧急
  回补报告。保护失败并成功回补时，以入场/回补成交均价和共同成交数量计算非负“失败损失估算”；
  该值只衡量不利价差、以 USDT 计价且不含手续费，成交价或数量不可确认时保持为空而不猜测。
  若紧急回补也失败，或入场请求超时后无法确认订单状态，系统会先保留失败审计，再立即停止引擎、
  执行账户级紧急平仓并锁定至下一个 UTC 日，避免在持仓状态不明时继续交易。
- **启动对账**：测试网启动时对账户、持仓和订单对账，存在无保护仓位则阻止启动。
- **紧急停止**：禁止新单、撤单并 reduce-only 平掉全部仓位；锁定持久化、重启保持、
  下一 UTC 日自动解锁。

### 4.6 回测

- 单标的事件驱动：下一根 K 线成交、手续费、滑点、资金费率、止损止盈，无前视；`ADD` 按当时
  权益、已有保证金和新止损重新定量并更新加权均价与保护价，`REDUCE` 平掉一半净仓且按比例
  分摊入场手续费与累计资金费，`CLOSE` 平掉剩余全部仓位。
- 多标的组合：等权资金子账户、时间对齐组合权益、组合回撤与分标的结果。
- 已审计决策历史重放（不重复调用模型）；显式独立 LLM 历史回测（滚动无前视快照、
  Provider 认证、调用预算、推理审计）。
- 指标：收益、最大回撤、Sharpe、Sortino、胜率、盈亏比、profit factor、换手、时间敞口；
  按方向、退出原因、**市场状态**（趋势/震荡/高波动，无前视趋尾窗口分类）分组统计。
- 控制台展示回测列表、按 ID 详情、交易明细、权益曲线与分组统计表。

### 4.7 审计、存储与溯源

- SQLite 表：`inferences`（模型推理）、`inference_details`（逐次输入与 Prompt）、
  `risk_decisions`、`executions`（实际订单报告）、`execution_attempts`（推理对应的执行结论、失败阶段与损失）、`backtests`、
  `user_stream_events`、`alert_events`、`runtime_state`、`schema_migrations`。
- 溯源：SHA-256 数据版本、显式 Prompt 版本、模型标识、CLI Provider 版本。
- 迁移：顺序数据库迁移、版本记录、幂等升级。

### 4.8 运维与可观测性

- 健康检查：`/api/health/live`（存活）、`/api/health/ready`（就绪，覆盖迁移版本与
  测试网 Broker 配置）。
- 结构化日志：HTTP 请求 JSON 日志 + request ID。
- 运行指标：`/api/metrics/runtime` 提供请求量、错误率、并发数、平均/P95 延迟、状态码分布。
- 告警：`/api/alerts` 覆盖紧急锁定、测试网配置/保护/用户流、API 错误率、模型错误率/P95 延迟；
  本地通知渠道对告警首次触发/解除去重后写入 JSON 日志与 `alert_events` 表，
  可经 `/api/alerts/history` 查询。**对外发送到第三方服务（Webhook/邮件/IM）刻意不实现**。
- 测试网账户状态：`/api/testnet/account-status` 提供余额摘要、非零持仓、启动对账与
  用户流状态且不暴露凭据；「交易权限」指标基于**可用保证金**（`availableBalance > 0`），
  因为币安期货账户接口无 `canTrade` 字段。

### 4.9 成本与用量核算

- 每次模型调用记录 Token 分项与模型名：Codex 从 `--json` 事件流解析
  input/cached/output token，模型名取自 `~/.codex/config.toml`；Claude 从输出解析
  token 与 `total_cost_usd`。
- **等效成本**：Claude 直接用 CLI 自带 `total_cost_usd`；Codex 经 **models.dev** 逐 token
  折算管线（`https://models.dev/api.json`，本地缓存 24h、离线回退，缓存读为输入子集、
  支持长上下文分层）；Custom API 仅在服务响应的 usage 明确提供 `cost` / `cost_usd` 时记录，
  不根据未知后端的模型名猜测价格。
- 订阅计划实际不按次计费，成本仅为**折算估算**；无法定价的模型显示为空。
- `/api/metrics/providers` 聚合 1–720 小时窗口：调用量、错误率、平均/P95 延迟、
  模型分布、Token 用量、等效成本。
- 每次成功启动引擎都会建立新的运行会话，并以推理审计 ID 记录起止边界。控制台每 2 秒通过
  `GET /api/metrics/run-session` 更新本次运行的时长、调用/错误数、输入/缓存/缓存写入/输出/总
  Token、等效成本，以及按调用次数计算的平均模型调用耗时、平均总 Token 和平均等效成本；仅当
  会话内全部调用均可定价时才显示平均成本。优雅停止会先结束调度任务再封存边界，紧急熔断也会
  封存边界。停止后继续
  显示刚结束的会话，边界外的新推理不会混入。运行会话只保留在当前服务进程内，重启服务后
  不尝试从历史记录猜测会话边界。
- 会话内所有调用均可定价时才显示成本总额；若存在未知价格，只显示可定价调用数并将总成本
  留空，避免把部分成本误报为完整成本。零调用会话的成本为 `$0.000000`。

### 4.10 控制台（白色浅色主题）

顶部标签栏将功能分为五页，避免单页过长；WebSocket 状态/决策推送与账户轮询在所有标签页
间共享，不随切换中断。决策记录变化后通过 WebSocket 自动更新最近 50 条；连接断开后每 2 秒
自动重连，并以每 15 秒 REST 轮询作为漏消息兜底，无需手动刷新页面。

总览、运行用量、硬风控、候选池、决策置信度、账户、24 小时 Provider 运维指标、测试网余额
与回测摘要均提供悬浮指标定义；带点状下划线的名称或数值会在光标停留约 80 ms 后显示统计窗口、分母、
计算口径或关键限制。指标说明使用控制台自有提示层，悬浮时光标保持普通箭头，不再等待浏览器原生提示。
Token 与成本定义同时注明 Provider 计量差异和订阅账单边界，避免把折算值
理解为实际扣费。

| 标签页 | 面板 | 内容 |
|---|---|---|
| 总览 | 引擎控制（hero）| 系统状态、分析周期、每周期标的数、启动/停止/紧急熔断；下方实时显示本次或上次运行的 Token、等效成本、调用数与时长 |
| 总览 | 01 模型接入 | Codex/Claude/Custom API 有序主备路由、当前承载/冷却状态、模型与推理强度选择器、配置连通性测试 |
| 总览 | 02 硬风控边界 | 只读展示不可修改的风控参数 |
| 总览 | 03 动态候选池 | 全市场扫描结果，可手动刷新；默认只显示评分前 5 个，可展开查看全部 |
| 总览 | 04 决策与风控 | 将 LLM 意图、硬风控与执行尝试合并为一条审计事件；可按放行、下单成功、下单失败、否决、HOLD、仅推理筛选，并展开失败阶段、交易所错误、紧急回补和损失估算 |
| 账户 | 06 账户与订单 | 按运行模式展示模拟账户或币安测试网账户的权益与持仓，以及本地审计的订单成交；每 5 秒刷新 |
| 回测 | 05 回测运行 | 重放表单、回测列表、详情（权益曲线、交易明细、分组统计）|
| 运维 | 07 模型与测试网 | Provider 延迟/调用量/错误率/Token/等效成本、脱敏测试网账户状态 |
| 数据 | 08 数据管理 | 按类别删除历史数据（见 4.11）|
| 设置 | 09 设置 | 表单方式增删改 Custom API 端点 + 按分区编辑本地 `.env`；保存后重启生效（见 4.12）|

决策列表对非 `HOLD` 动作显示「执行置信度」；对 `HOLD` 将同一字段弱化显示为「机会强度」，
表示没有可执行动作时残留的方向性机会，避免误解成模型对 HOLD 结论本身缺乏信心。
非 `HOLD` 决策将风控结论与执行结论分层显示：`executed` 表示下单及所需保护流程完成，
`execution_failed` 表示风控已放行但入场、保护或回补阶段失败；能够确认不利价差时，列表直接显示
失败损失估算，详情同时展示入场与回补数量/均价。`approved` 只用于尚无执行尝试的瞬时或旧记录。
每条决策在 Provider 旁直接显示该次调用实际使用的模型名和推理强度；这两个值随推理结果写入
审计，不读取当前配置回填。升级前没有强度快照的历史记录显示「推理强度未记录」，显式留空并
使用 Provider 默认值的新记录显示「默认推理强度」。

### 4.12 设置（编辑本地 .env）

- 控制台「设置」标签页按分区列出**全部 `.env` 配置项**（运行模式与服务、决策与运行、
  Provider 路由、Custom API 单个/多个、币安测试网），`GET /api/settings` 提供字段元数据与
  当前值，`POST /api/settings` 保存。
- **密钥只写不读**：币安测试网 Key/Secret、Custom API Key，以及内嵌 `api_key` 的
  `*_PROVIDERS_JSON` / `*_HEADERS_JSON` **永不以明文经 REST 返回**，只返回掩码尾号
  （如 `sup…abcd`）用于辨认。前端只发送**被改动过的键**，因此不触碰的密钥不会被其掩码覆盖；
  留空表示保持不变。这延续了 4.1 的 API Key 边界。
- **只写文件、不改运行中的进程**：保存只写 `.env`，**重启后生效**；界面明确提示。已在 shell
  中 `export` 的同名变量在运行时优先级仍然更高。
- **保存前整体校验**：用启动时同一套解析器校验候选配置（`Settings.from_mapping`，纯函数，
  不篡改 `os.environ`），并补充引擎/调度器构造期才会做的范围检查（周期取值、每周期标的数、
  仅 localhost 绑定）。**任何不合法的值都会在文件被修改前拒绝**，不会写出一个下次启动直接
  崩溃的 `.env`。
- **Custom API 端点用表单编辑，不写 JSON**：`GET/POST /api/custom-providers` 提供逐字段的增删改
  （ID、Base URL、API Key、模型、协议、推理强度、是否需要 Key），前端负责序列化成
  `CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON`。**每个端点的 `api_key` 仍只写不读**：GET 只返回
  `api_key_configured` 与掩码；POST 省略 `api_key` 表示保持原值、`""` 表示清除、给值表示替换。
  `extra_headers` 的值同样是密钥，GET 只返回**头名称**，保存时未提交则原样保留。保存前用启动
  解析器校验，并额外校验 Base URL（Provider 构造只把非法 URL 记为配置错误而不抛错，
  不校验就会存下一个"看起来保存成功、实际不可用"的端点）。
- **写入安全**：保留注释与键顺序，未知键不动，新键追加；原子替换（临时文件 + `os.replace`），
  文件权限 `0600`；值按原样写入不转义——读取端（`load_dotenv`）只剥离首尾引号且不做反转义，
  转义会让 JSON 配置在下次启动时损坏。
- **重启后端**（`POST /api/restart`，控制台设置页按钮）：用当前 `.env` 重新执行后端进程，让保存
  的设置生效。**引擎运行中会被拒绝（409）**，避免打断实盘/测试网运行；先响应再 `os.execve`
  重新执行自身（进程被替换，之后无法再发响应），前端轮询 `/api/health/live` 直到恢复再刷新。
  **重启前会剔除本进程当初由 `.env` 注入的环境变量**（`DOTENV_INJECTED_KEYS`）：`load_dotenv`
  不覆盖已有变量，若继承旧值则重写后的 `.env` 会被忽略、重启等于白做；shell 中显式 `export`
  的变量会保留（它本就优先于 `.env`）。重新执行统一走 `python -m candlepilot.cli`，
  因此 `candlepilot serve` 与 `python -m candlepilot.cli serve` 两种启动方式都能恢复。
- `.env` 路径默认为工作目录下的 `.env`，可用 `CANDLEPILOT_ENV_FILE` 指定。

### 4.11 数据管理

- 控制台「数据管理」面板与 `POST /api/history/clear` 可按类别删除历史数据：
  模型调用与决策、风控决策、订单成交、回测、测试网事件、告警历史、行情缓存、定价缓存。
- 可用「全选 / 取消全选」一次切换全部类别；删除仍需**两步确认**，写入结构化日志。
- **绝不触及** `runtime_state`（模拟账户、紧急锁定）与 `schema_migrations`，
  故清历史不会重置账户或解除熔断锁。

## 5. 配置

`candlepilot` 启动时自动读取当前目录 `.env`（`doctor`/`serve`/`acceptance` 均适用）；
已在 shell 中 `export` 的变量优先级更高。

| 变量 | 说明 |
|---|---|
| `CANDLEPILOT_MODE` | `backtest` / `paper-production-data` / `binance-testnet` |
| `CANDLEPILOT_HOST` / `CANDLEPILOT_PORT` | 绑定地址（仅 localhost）与端口（默认 8000）|
| `CANDLEPILOT_DATABASE_URL` | SQLite 连接串 |
| `CANDLEPILOT_DATA_DIR` | 数据目录（Parquet 行情缓存、models.dev 定价缓存）|
| `CANDLEPILOT_LLM_TIMEOUT` | LLM 子进程硬超时（秒，默认 45）|
| `CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS` | LLM 分析快照允许进入下单前行情刷新的最大年龄（秒，默认 75，必须为正整数）|
| `CANDLEPILOT_CADENCES` | 逗号分隔的分析周期子集，默认 `5m,15m,30m` |
| `CANDLEPILOT_CANDIDATES_PER_CYCLE` | 每周期分析候选池前 N 个标的，默认 5（范围 1–20）|
| `CANDLEPILOT_MAX_RUN_SECONDS` | 单次运行时长上限（秒）；留空/非正数=不限 |
| `CANDLEPILOT_MAX_RUN_COST_USD` | 单次运行等效成本预算（USD）；留空/非正数=不限 |
| `CANDLEPILOT_PROVIDER_CHAIN` | 启动时默认的逗号分隔有序 Provider 路由，例如 `codex,claude-code,openai-compatible`；不允许重复，优先级高于旧的单 Provider 配置 |
| `CANDLEPILOT_DEFAULT_PROVIDER` | 兼容旧配置的单 Provider 默认值；仅在 `CANDLEPILOT_PROVIDER_CHAIN` 留空时使用 |
| `CANDLEPILOT_CODEX_MODEL` / `CANDLEPILOT_CODEX_REASONING_EFFORT` | Codex 模型 / 推理强度（minimal/low/medium/high）|
| `CANDLEPILOT_CLAUDE_MODEL` / `CANDLEPILOT_CLAUDE_EFFORT` | Claude 模型 / 强度（low/medium/high/xhigh/max）|
| `CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON` | **全部** Custom API 端点的 JSON 数组（最多 8 个），每项需唯一 `id` 与 `base_url`，注册为 `openai-compatible:<id>` |
| `CANDLEPILOT_ENV_FILE` | `.env` 路径（默认工作目录下 `.env`），加载器与控制台设置页共用 |
| `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` | 仅测试网模式需要 |

## 6. CLI 命令

- `candlepilot doctor` —— 检查 LLM 登录状态与币安只读公共接口，不下单。
- `candlepilot serve` —— 启动本地 API 与已构建的 Web 控制台。
- `candlepilot acceptance [--required-hours 24] [--lookback-hours 168]` ——
  按发布不变量审计测试网软运行（连续运行时长、订单号唯一、持仓已对账且带保护、
  每笔成交可追溯到模型与风控）；证据不足返回非零退出码，绝不虚假放行。

## 7. HTTP / WebSocket API 参考

**引擎与 Provider**：`GET /api/status`、`GET /api/providers`、
`POST /api/providers/select`、`POST /api/providers/config`、`POST /api/providers/test`、
`POST /api/cadences`、
`POST /api/candidates-per-cycle`、`POST /api/run-limits`、
`GET /api/settings`、`POST /api/settings`、`POST /api/engine/start`、
`POST /api/engine/stop`、`POST /api/engine/emergency-stop`、
`POST /api/engine/clear-emergency-lock`。

`POST /api/providers/select` 的新格式为
`{"providers":["codex-auth","claude-code-auth","openai-compatible"]}`；旧的
`{"name":"codex-auth","backup":"claude-code-auth"}` 仍兼容。引擎运行时修改返回 409。
`GET /api/status` 通过 `provider_chain`、`active_provider` 和 `provider_routes` 返回顺序、当前承载、
冷却截止时间、最近错误与最近成功/失败时间；不返回任何凭据。

**行情与选币**：`GET /api/universe`、`POST /api/universe/refresh`、
`GET /api/market/klines`、`GET /api/market/funding-rates`、
`GET /api/market/backtest-candles`。

**决策与信号**：`POST /api/decisions/evaluate`、`GET /api/decision-events`、
`GET /api/decision-events/{inference_id}`、`GET /api/signals`。列表接口只返回轻量摘要；按 ID
详情接口返回该次推理的结构化输入、实际 Prompt、原始输出、token usage 和等效成本。
`decision-events` 以模型推理为主记录，关联硬风控和执行尝试并给出 `approved` / `executed` /
`execution_failed` / `rejected` / `hold` / `analysis_only` 展示状态；执行对象包含状态、失败阶段、
交易所错误、入场/回补报告与可用时的损失估算。`signals` 保留为原始推理查询，不推断订单是否成交。

**账户与风险**：`GET /api/account/portfolio`、`GET /api/account/positions`、
`GET /api/orders`、`GET /api/fills`、`GET /api/risk-events`。前两个账户接口按当前运行模式
返回归一化数据：模拟模式读取本地 `PaperExecutor`，测试网模式读取币安测试网钱包、保证金和
非零持仓；测试网显示未实现盈亏且不伪装成当日盈亏。测试网保护单由交易所托管，账户持仓
列表根据启动对账结果标明“交易所侧 / 缺失 / 待确认”，不重复查询或推断触发价。测试网账户
相关接口共享 1 秒查询缓存，将同一轮控制台并发轮询合并为一次币安签名请求。订单与成交接口
始终读取本地执行审计记录。

**测试网**：`GET /api/testnet/events`、`GET /api/testnet/account-status`。

**回测**：`GET /api/backtests`、`GET /api/backtests/{id}`、`POST /api/backtests`、
`POST /api/backtests/replay`、`POST /api/backtests/llm`、`POST /api/backtests/portfolio`。

**运维**：`GET /api/health/live`、`GET /api/health/ready`、`GET /api/metrics/runtime`、
`GET /api/metrics/providers`、`GET /api/metrics/run-session`、`GET /api/alerts`、
`GET /api/alerts/history`。

**数据管理**：`POST /api/history/clear`。

**实时**：`WS /ws/events`（每 2 秒推送引擎状态；仅在最近 50 条决策发生变化时推送
`decisions` 事件）。

## 8. 运行与验证

安装与启动见 [README.md](README.md)。验证命令：

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
cd frontend && pnpm run build
python scripts/check_commit_messages.py --commit HEAD
```

首次克隆后执行 `git config core.hooksPath .githooks`，启用版本化 `commit-msg` hook。该 hook
会在提交创建前要求 Conventional Commit 标题、空行后的 description，以及位于最后一行的
归属 trailer。Agent 实现的提交使用 GitHub 可识别的 Codex 或 Claude Code `Co-authored-by`；
完全由用户本人实现、没有 Agent 参与的提交使用 `Human-authored: true`，Agent 不得冒用该标记。
包含字面量 `\\n` 的错误消息会被拒绝。GitHub Actions CI（`.github/workflows/ci.yml`）会对每次
push/PR 的所有新增提交重复执行同一校验，即使本地 hook 被绕过也会失败；其余 CI 检查同样
运行上述 Ruff、Pytest 和构建。
Python 依赖锁定于 `requirements.lock`，前端锁定于 `frontend/pnpm-lock.yaml`。

纯人工提交示例（只有确实没有 Agent 参与时使用）：

```bash
git commit \
  -m "fix: correct account label" \
  -m "Correct the label so it matches the underlying account source." \
  -m "Human-authored: true"
```

## 9. 尚未实施 / 路线图

- [ ] **测试网连续运行 24 小时验收**：`candlepilot acceptance` 工具已就绪，
  但尚未记录真实的 24 小时测试网软运行。
- [ ] **决策历史作为模型输入**：每次 LLM 调用当前完全无状态——模型看不到自己上一周期对
  同一标的说过什么，因此同标的跨周期容易反复翻转，且无法表达"维持上次判断"。
  **刻意暂缓**：这不是补数据就能解决的，需要先定清楚三件事：喂多少历史、按什么维度筛选
  （同标的？同周期？只喂非 HOLD？），以及**如何避免模型锚定自己此前的错误判断**——
  把上次的错误结论当作证据回灌，比没有历史更糟。设计确定前不实现。
- [ ] Docker 镜像与发布流程。
- [ ] 外部告警通知渠道（刻意暂缓，避免本地优先系统产生未授权外发）。

**永不实施**：币安真钱实盘入口、美股/券商适配、新闻/社交/链上数据。

## 10. 文档维护约定

1. 本文件是唯一权威功能文档；`STATUS.md`、`PLAN.md` 已弃用。
2. 每个可独立验收的功能：改代码 + 加/改测试 + **同步更新本文件** + 单独 Git 提交。
3. 只有测试通过并完成必要实际检查，才在文档中描述为已实现。
4. 控制台改动必须执行 TypeScript/Vite 构建并在浏览器验证。
5. 不以盈利为验收标准，以无前视偏差、可审计、风险边界、故障恢复为准。
6. 每个 Git 提交必须使用清晰的 Conventional Commit 风格标题，并在空行后提供有意义的
   description，说明改了什么、为什么改；不得只提交标题。
7. AI 实现的提交必须追加 GitHub 可识别的共同作者 trailer，以显示共同作者及头像：
   Codex 使用 `Co-authored-by: Codex <noreply@openai.com>`；Claude Code 使用当前对应的
   Anthropic 共同作者身份。具体执行规则同时记录在根目录 `AGENTS.md`。
