# CandlePilot 功能文档

> 本文件是 CandlePilot 的**唯一权威功能文档**，记录系统当前的全部能力、接口与边界。
> `STATUS.md` 与 `PLAN.md` 已弃用，后续变更只同步更新本文件。
> 最后更新：2026-07-15

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

### 4.1 LLM 接入（订阅认证，无 API Key）

- **Codex Auth**：优先检测 Codex App 内置二进制（`/Applications/Codex.app/...`），
  不可用时回退到 `PATH` 中的独立 `codex` CLI。用 `codex exec --json --output-schema`
  复用 ChatGPT/Codex 登录态。
- **Claude Code Auth**：检测独立 `claude` CLI，复用 Claude.ai Pro/Max 登录态
  （`claude -p --output-format json --permission-mode plan --max-turns 1`）。
- **隔离与安全**：LLM 子进程运行在独立空临时目录，环境变量白名单
  （含 `USER`/`LOGNAME` 以支持 macOS 钥匙串读取 Claude 登录态），移除所有币安/API Key
  变量；禁用工具、MCP、网络；45 秒硬超时、单 Provider 并发 1、统一取消。
- **严格 Schema**：输出必须通过统一 `TradeIntent` Pydantic 校验，否则降级为 `HOLD`。
- **主备切换**：控制台手动选择当前 Provider；可显式配置单次主备故障切换（默认关闭）。
- **可选模型与推理强度**：Codex 传 `-m` / `-c model_reasoning_effort`，Claude 传
  `--model` / `--effort`。默认取自环境变量，也可在控制台运行前经 `/api/providers/config`
  修改；控制台模型为下拉选择（选项来自 models.dev 目录、按 Provider 过滤、含 CLI 别名），
  并保留「自定义」输入以支持目录外模型。

### 4.2 行情与数据

- 币安 REST：合约发现、交易规则、K 线、资金费率、盘口、24h ticker、持仓量。
- 币安 WebSocket：K 线、mark price、盘口、ticker、账户/订单用户数据流；
  自动重连、心跳、去重、乱序处理、断线后 REST 回补。
- 历史下载：K 线与资金费率分页拉取；资金费率按实际结算 K 线映射。
- 本地缓存：历史 K 线以精确区间存为 Parquet（可在数据管理中清除）。

### 4.3 选币与特征

- 动态全市场扫描 USDT 永续，按上市时间、完整度、价差、成交额、波动率、趋势过滤排名；
  启动即扫，之后每分钟自动刷新轮换，每周期最多向 LLM 提交 5 个候选。
- 特征：1m/5m/15m 共享 EMA、RSI、ATR、收益率、成交量，以及基差、持仓量、
  20 档盘口与近期成交失衡等微观结构特征。
- 1m/5m/15m 对齐调度；同标的跨周期串行评估，禁止相反方向并发开仓，同向增仓须显式 `ADD`。

### 4.4 决策与风控

- `TradeIntent`：标的、周期、`HOLD/OPEN_LONG/OPEN_SHORT/ADD/REDUCE/CLOSE`、置信度、
  杠杆、风险比例、订单类型、入场价、止损、止盈、有效期、理由。
- 硬风控（不可由模型修改）：逐仓、单向净仓、最高 10x、单笔计划亏损 ≤ 权益 2%、
  最多 8 个仓位、总保证金占用 ≤ 60%；日内净亏损 8% 触发熔断。
- 开仓必须带交易所侧止损；仓位按止损距离、费用、保守滑点反推。
- 拒绝数据过期、余额不足、无止损、强平缓冲不足、不符合交易所精度的意图。

### 4.5 执行（模拟 / 测试网）

- **模拟撮合**：行情驱动的限价单、止损、止盈与持续盯市；现金/仓位/挂单/成交持久化，
  重启后恢复并继续实时保护；紧急平仓。
- **测试网**：签名下单、逐仓、杠杆设置、保护止损（`closePosition` 覆盖全部仓位）、
  部分成交后撤销入场余量；状态未知订单不重复下单；429/418 退避重试；时间戳错误自动重同步。
- **启动对账**：测试网启动时对账户、持仓和订单对账，存在无保护仓位则阻止启动。
- **紧急停止**：禁止新单、撤单并 reduce-only 平掉全部仓位；锁定持久化、重启保持、
  下一 UTC 日自动解锁。

### 4.6 回测

- 单标的事件驱动：下一根 K 线成交、手续费、滑点、资金费率、止损止盈，无前视。
- 多标的组合：等权资金子账户、时间对齐组合权益、组合回撤与分标的结果。
- 已审计决策历史重放（不重复调用模型）；显式独立 LLM 历史回测（滚动无前视快照、
  Provider 认证、调用预算、推理审计）。
- 指标：收益、最大回撤、Sharpe、Sortino、胜率、盈亏比、profit factor、换手、时间敞口；
  按方向、退出原因、**市场状态**（趋势/震荡/高波动，无前视趋尾窗口分类）分组统计。
- 控制台展示回测列表、按 ID 详情、交易明细、权益曲线与分组统计表。

### 4.7 审计、存储与溯源

- SQLite 表：`inferences`（模型推理）、`risk_decisions`、`executions`、`backtests`、
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
  支持长上下文分层）。
- 订阅计划实际不按次计费，成本仅为**折算估算**；无法定价的模型显示为空。
- `/api/metrics/providers` 聚合 1–720 小时窗口：调用量、错误率、平均/P95 延迟、
  模型分布、Token 用量、等效成本。

### 4.10 控制台（白色浅色主题）

| 面板 | 内容 |
|---|---|
| 01 模型认证 | Provider 选择、登录状态、模型与推理强度选择器 |
| 02 硬风控边界 | 只读展示不可修改的风控参数 |
| 03 动态候选池 | 全市场扫描结果，可手动刷新 |
| 04 最近决策 | 结构化交易意图审计流 |
| 05 回测运行 | 重放表单、回测列表、详情（权益曲线、交易明细、分组统计）|
| 06 账户与风险 | 权益、持仓、订单成交、风控决策，每 5 秒刷新 |
| 07 模型与测试网 | Provider 延迟/调用量/错误率/Token/等效成本、脱敏测试网账户状态 |
| 08 数据管理 | 按类别删除历史数据（见 4.11）|

### 4.11 数据管理

- 控制台「数据管理」面板与 `POST /api/history/clear` 可按类别删除历史数据：
  模型调用与决策、风控决策、订单成交、回测、测试网事件、告警历史、行情缓存、定价缓存。
- 删除需**两步确认**，写入结构化日志。
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
| `CANDLEPILOT_CODEX_MODEL` / `CANDLEPILOT_CODEX_REASONING_EFFORT` | Codex 模型 / 推理强度（minimal/low/medium/high）|
| `CANDLEPILOT_CLAUDE_MODEL` / `CANDLEPILOT_CLAUDE_EFFORT` | Claude 模型 / 强度（low/medium/high/xhigh/max）|
| `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` | 仅测试网模式需要 |

## 6. CLI 命令

- `candlepilot doctor` —— 检查 LLM 登录状态与币安只读公共接口，不下单。
- `candlepilot serve` —— 启动本地 API 与已构建的 Web 控制台。
- `candlepilot acceptance [--required-hours 24] [--lookback-hours 168]` ——
  按发布不变量审计测试网软运行（连续运行时长、订单号唯一、持仓已对账且带保护、
  每笔成交可追溯到模型与风控）；证据不足返回非零退出码，绝不虚假放行。

## 7. HTTP / WebSocket API 参考

**引擎与 Provider**：`GET /api/status`、`GET /api/providers`、
`POST /api/providers/select`、`POST /api/providers/config`、`POST /api/engine/start`、
`POST /api/engine/stop`、`POST /api/engine/emergency-stop`、
`POST /api/engine/clear-emergency-lock`。

**行情与选币**：`GET /api/universe`、`POST /api/universe/refresh`、
`GET /api/market/klines`、`GET /api/market/funding-rates`、
`GET /api/market/backtest-candles`。

**决策与信号**：`POST /api/decisions/evaluate`、`GET /api/signals`。

**账户与风险**：`GET /api/account/portfolio`、`GET /api/account/positions`、
`GET /api/orders`、`GET /api/fills`、`GET /api/risk-events`。

**测试网**：`GET /api/testnet/events`、`GET /api/testnet/account-status`。

**回测**：`GET /api/backtests`、`GET /api/backtests/{id}`、`POST /api/backtests`、
`POST /api/backtests/replay`、`POST /api/backtests/llm`、`POST /api/backtests/portfolio`。

**运维**：`GET /api/health/live`、`GET /api/health/ready`、`GET /api/metrics/runtime`、
`GET /api/metrics/providers`、`GET /api/alerts`、`GET /api/alerts/history`。

**数据管理**：`POST /api/history/clear`。

**实时**：`WS /ws/events`（每 2 秒推送引擎状态）。

## 8. 运行与验证

安装与启动见 [README.md](README.md)。验证命令：

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
cd frontend && pnpm run build
```

GitHub Actions CI（`.github/workflows/ci.yml`）在每次 push/PR 上运行相同检查。
Python 依赖锁定于 `requirements.lock`，前端锁定于 `frontend/pnpm-lock.yaml`。

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
