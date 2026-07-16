# CandlePilot 功能文档

> 本文件是 CandlePilot 的**唯一权威功能文档**，记录系统当前的全部能力、接口与边界。
> `STATUS.md` 与 `PLAN.md` 已弃用，后续变更只同步更新本文件。
> 最后更新：2026-07-16（增加自带 Base URL / API Key 的模型接入）

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
- **Custom API**：可通过 `.env` 自带 Base URL、API Key 与模型名，接入实现 OpenAI-compatible
  `/chat/completions` 或 `/responses` 的服务，默认保持 Chat Completions。Responses 请求使用
  `input`、`store=false` 与嵌套 `reasoning.effort`，并从 message 的 `output_text` 内容提取结果。
  两种协议都只发送统一 Prompt、不启用工具，并在本地严格校验返回的 `TradeIntent`；支持标准
  token usage、缓存 token，以及服务端可选返回的单次成本。
  外部地址必须使用 HTTPS，仅 `localhost` / `127.0.0.1` / `::1` 可使用 HTTP；禁止 URL 内嵌
  凭据、query、fragment 和 HTTP 重定向，避免 Key 被明文传输或转发。该 Provider 不主动探测
  `/models`，`doctor` 只报告配置是否完整，实际连通性由控制台「测试」显式验证。
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
- **主备切换**：控制台手动选择当前 Provider；可显式配置单次主备故障切换（默认关闭）。
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
- 特征：1m/5m/15m/30m 共享 EMA、RSI、ATR、收益率、成交量，以及基差、持仓量、
  20 档盘口与近期成交失衡等微观结构特征。每个周期获取最近 200 根 K 线、排除未收盘 K 线
  后计算合计 49 个快照特征；LLM 接收特征值而不是原始 K 线数组。
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

### 4.4 决策与风控

- `TradeIntent`：标的、周期、`HOLD/OPEN_LONG/OPEN_SHORT/ADD/REDUCE/CLOSE`、置信度、
  杠杆、风险比例、订单类型、入场价、止损、止盈、有效期、理由。
- 硬风控（不可由模型修改）：逐仓、单向净仓、最高 10x、单笔计划亏损 ≤ 权益 2%、
  最多 8 个仓位、总保证金占用 ≤ 60%；日内净亏损 8% 触发熔断。
- 开仓必须带交易所侧止损；仓位按止损距离、费用、保守滑点反推。
- **测试网模式额外要求开仓/加仓同时带止盈**（缺止盈直接否决）；无论何种模式，只要给了
  止盈都会校验方向（多单止盈须高于入场、空单须低于入场）。
- LLM 使用周期开始时的完整特征快照分析；非 `HOLD` 意图返回后，执行路径必须重新获取最新
  行情并刷新账户/模拟持仓，再用新标记价完成硬风控、定量和执行。市价单忽略模型建议的入场价，
  始终按刷新行情定量；若刷新失败或最新价已越过止损/止盈，统一审计为风控否决且不下单。
  限价单因行情变化已可立即成交时仍可在全部最新风控通过后执行，放行原因会追加
  `limit entry is immediately marketable after refresh` 审计标记；保证金数量使用刷新后的买一/卖一
  与限价中更保守的价格计算。`HOLD` 无订单，不因分析快照变旧产生伪否决。
- 分析快照从采集到 LLM 返回的最大年龄默认 30 秒，可通过
  `CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS` 调整；超过上限会在刷新前否决。刷新后的行情仍须通过
  同一有效期检查。另拒绝余额不足、无止损、强平缓冲不足、不符合交易所精度的意图。
- 每次新推理都审计结构化行情/组合输入、实际 Prompt、Provider 原始输出和 token usage；控制台
  在单条决策展开后按需加载“AI 分析详情”，显示输入/缓存输入/输出/总 token、按标准 API
  定价折算的单次等效成本，并允许复制输入、Prompt 和输出。升级前的旧记录仍可查看已有输出与
  usage，但精确输入不会被推测或补造；所有详情只经 localhost API 提供且不包含 Provider 或币安凭据。

### 4.5 执行（模拟 / 测试网）

- **模拟撮合**：行情驱动的限价单、止损、止盈与持续盯市；现金/仓位/挂单/成交持久化，
  重启后恢复并继续实时保护；紧急平仓。
- **测试网**：签名下单、逐仓、杠杆设置、入场成交后同时挂**保护止损 `STOP_MARKET` 与
  止盈 `TAKE_PROFIT_MARKET`**（均 `closePosition`、按标记价触发）；任一保护单挂单失败即紧急
  减仓平掉并报错；部分成交后撤销入场余量；状态未知订单不重复下单；429/418 退避重试；
  时间戳错误自动重同步。`ADD` 增仓采用两阶段保护替换：先读取旧 CandlePilot 括号，增仓成交后
  先挂妥新的止损和止盈，再逐单撤销旧括号；人工订单不会被删除，替换期间始终至少有一组保护。
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
  `risk_decisions`、`executions`、`backtests`、
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
  Token 与等效成本；优雅停止会先结束调度任务再封存边界，紧急熔断也会封存边界。停止后继续
  显示刚结束的会话，边界外的新推理不会混入。运行会话只保留在当前服务进程内，重启服务后
  不尝试从历史记录猜测会话边界。
- 会话内所有调用均可定价时才显示成本总额；若存在未知价格，只显示可定价调用数并将总成本
  留空，避免把部分成本误报为完整成本。零调用会话的成本为 `$0.000000`。

### 4.10 控制台（白色浅色主题）

顶部标签栏将功能分为五页，避免单页过长；WebSocket 状态/决策推送与账户轮询在所有标签页
间共享，不随切换中断。决策记录变化后通过 WebSocket 自动更新最近 50 条；连接断开后每 2 秒
自动重连，并以每 15 秒 REST 轮询作为漏消息兜底，无需手动刷新页面。

| 标签页 | 面板 | 内容 |
|---|---|---|
| 总览 | 引擎控制（hero）| 系统状态、分析周期、每周期标的数、启动/停止/紧急熔断；下方实时显示本次或上次运行的 Token、等效成本、调用数与时长 |
| 总览 | 01 模型接入 | Codex/Claude/Custom API 选择、就绪状态、模型与推理强度选择器、配置连通性测试 |
| 总览 | 02 硬风控边界 | 只读展示不可修改的风控参数 |
| 总览 | 03 动态候选池 | 全市场扫描结果，可手动刷新 |
| 总览 | 04 决策与风控 | 将 LLM 意图与对应硬风控结果合并为一条审计事件；可按放行、否决、HOLD、仅推理筛选并展开参数与原因 |
| 账户 | 06 账户与订单 | 按运行模式展示模拟账户或币安测试网账户的权益与持仓，以及本地审计的订单成交；每 5 秒刷新 |
| 回测 | 05 回测运行 | 重放表单、回测列表、详情（权益曲线、交易明细、分组统计）|
| 运维 | 07 模型与测试网 | Provider 延迟/调用量/错误率/Token/等效成本、脱敏测试网账户状态 |
| 数据 | 08 数据管理 | 按类别删除历史数据（见 4.11）|

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
| `CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS` | LLM 分析快照允许进入下单前行情刷新的最大年龄（秒，默认 30，必须为正整数）|
| `CANDLEPILOT_CADENCES` | 逗号分隔的分析周期子集，默认 `5m,15m,30m` |
| `CANDLEPILOT_CANDIDATES_PER_CYCLE` | 每周期分析候选池前 N 个标的，默认 5（范围 1–20）|
| `CANDLEPILOT_DEFAULT_PROVIDER` | 启动时默认选中的 LLM Provider；支持 `codex` / `claude-code` / `openai-compatible`（也接受相应内部名），留空则在控制台手动选择 |
| `CANDLEPILOT_CODEX_MODEL` / `CANDLEPILOT_CODEX_REASONING_EFFORT` | Codex 模型 / 推理强度（minimal/low/medium/high）|
| `CANDLEPILOT_CLAUDE_MODEL` / `CANDLEPILOT_CLAUDE_EFFORT` | Claude 模型 / 强度（low/medium/high/xhigh/max）|
| `CANDLEPILOT_CUSTOM_LLM_BASE_URL` | OpenAI-compatible API 根地址；系统按协议追加 `/chat/completions` 或 `/responses`，外部仅 HTTPS，回环地址可 HTTP |
| `CANDLEPILOT_CUSTOM_LLM_API_KEY` | Custom API Bearer Key；仅后端环境读取，绝不经 API、日志或数据库暴露 |
| `CANDLEPILOT_CUSTOM_LLM_MODEL` / `CANDLEPILOT_CUSTOM_LLM_REASONING_EFFORT` | Custom API 模型名 / 可选强度（low/medium/high/xhigh）|
| `CANDLEPILOT_CUSTOM_LLM_WIRE_API` | Custom API 协议：`chat-completions`（默认）或 `responses` |
| `CANDLEPILOT_CUSTOM_LLM_REQUIRE_API_KEY` | 是否要求并发送 Bearer Key，默认 `true`；无 Bearer 认证的兼容服务可设 `false` |
| `CANDLEPILOT_CUSTOM_LLM_EXTRA_HEADERS_JSON` | 服务商专用请求头 JSON；值按密钥保护，最多 16 个且不能覆盖受保护头 |
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
`POST /api/candidates-per-cycle`、`POST /api/engine/start`、
`POST /api/engine/stop`、`POST /api/engine/emergency-stop`、
`POST /api/engine/clear-emergency-lock`。

**行情与选币**：`GET /api/universe`、`POST /api/universe/refresh`、
`GET /api/market/klines`、`GET /api/market/funding-rates`、
`GET /api/market/backtest-candles`。

**决策与信号**：`POST /api/decisions/evaluate`、`GET /api/decision-events`、
`GET /api/decision-events/{inference_id}`、`GET /api/signals`。列表接口只返回轻量摘要；按 ID
详情接口返回该次推理的结构化输入、实际 Prompt、原始输出、token usage 和等效成本。
`decision-events` 以模型推理为主记录，关联对应硬风控结果并给出
`approved` / `rejected` / `hold` / `analysis_only` 展示状态；`signals` 保留为原始推理查询，
二者均不推断订单是否成交。

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
