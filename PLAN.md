# CandlePilot 实施方案

## 目标

CandlePilot 是一个本地优先、可审计的币安 USDⓈ-M 永续合约日内交易平台。首版支持历史回测、生产行情模拟交易和币安测试网，不提供真钱实盘入口。系统使用动态全市场选币，同时运行 1m、5m、15m 决策周期，由 LLM 自主提出交易参数，再由不可绕过的确定性风控验证和执行。

工程验收标准是系统稳定、无前视偏差、决策可复现且风险边界有效，不以盈利作为软件验收条件。

## 技术架构

- 后端：Python 3.12、FastAPI、Pydantic、SQLAlchemy、SQLite WAL。
- 前端：React、TypeScript、Vite，通过 REST 和 WebSocket 连接后端。
- 行情：SQLite 保存业务与审计数据，Parquet 保存大容量历史行情。
- 本地运行：异步单进程，不依赖 Redis/Celery；提供锁定依赖和可选 Docker Compose。
- 扩展边界：`MarketDataProvider`、`BrokerAdapter`、`LLMProvider`、`FeaturePipeline`、`DecisionEngine`、`RiskPolicy` 均为适配器接口，为云部署和美股接入预留扩展点。

## LLM Auth Provider

首版不要求 OpenAI 或 Anthropic API Key，提供以下本地订阅认证方式：

1. `CodexAuthProvider`
   - 优先检测 `/Applications/Codex.app/Contents/Resources/codex`。
   - App 内置二进制不可用或版本不兼容时，回退到 `PATH` 中的独立 `codex` CLI。
   - 使用 `codex exec --ephemeral --sandbox read-only --output-schema`，复用用户的 ChatGPT/Codex 登录态。
2. `ClaudeCodeAuthProvider`
   - 要求 `PATH` 中存在独立 `claude` CLI。
   - 使用 `claude -p --output-format json --permission-mode plan --max-turns 1`，复用 Claude Code 的 Claude.ai Pro/Max 登录态。

统一接口提供 `health_check`、`generate_trade_intent`、`cancel` 和 `capabilities`。Web 控制台由用户手动选择当前 Provider，故障回退默认关闭，可显式配置主备顺序。

系统不读取、复制或存储 CLI 的 OAuth 凭证。LLM 子进程在独立空临时目录运行，使用环境变量白名单并移除所有币安/API Key 变量；禁止项目文件、MCP 和交易工具，限制为单轮、45 秒硬超时和单 Provider 并发 1。若不能可靠禁用工具或输出不能通过统一 Pydantic Schema 校验，交易结果降级为 `HOLD`。

## 数据、决策和执行

- REST 初始化合约规则、历史 K 线、资金费率和持仓量；WebSocket 维护 K 线、mark price、ticker、盘口和账户状态。
- 只选择可交易的 USDT 永续；排除上市不足 30 天、数据不完整或价差超过 20bp 的标的。
- 每分钟按成交额、价差、波动率、趋势和流动性扫描全市场；先保留成交额前 50，再取综合排名前 20，每个周期最多向 LLM 提交 5 个候选。
- 1m、5m、15m 引擎并行，共享 1m/5m/15m/1h 特征、资金费率、基差、盘口失衡、近期成交和组合状态。
- `TradeIntent` 包含标的、周期、`HOLD/OPEN_LONG/OPEN_SHORT/ADD/REDUCE/CLOSE`、置信度、杠杆、风险比例、订单类型、入场价、止损、止盈、有效期和理由。
- 同一标的只能有一个净方向；已有仓位退出优先，反向开仓必须先平仓并等待下一决策周期。

执行管线固定为 `TradeIntent -> RiskDecision -> OrderPlan -> ExecutionReport`。测试网默认使用逐仓、单向持仓、最高 10x、单笔最大计划亏损为权益 2%、最多 8 个仓位、总保证金占用不超过 60%。日内净亏损达到 8% 后撤单、reduce-only 平仓并锁定到下一个 UTC 日。

开仓必须带交易所侧止损；仓位根据止损距离、费用和保守滑点反推。系统拒绝数据过期、余额不足、无止损、强平缓冲不足或不符合交易所精度的意图。订单使用唯一 client order ID，并处理部分成交、状态未知、断线重连、时间偏差、限频和启动对账。紧急停止会禁止新单、撤单并 reduce-only 平掉全部仓位。

## Web 控制台与回测

- 展示 Provider 登录与版本状态、动态候选池、多周期信号、持仓订单、权益曲线、风险额度、模型延迟和回测报告。
- 运行模式严格隔离为 `backtest`、`paper-production-data`、`binance-testnet`。
- 回测和前向交易共享扫描、特征、决策 Schema、风控和仓位计算，计入手续费、资金费率、滑点和延迟。
- 默认重放缓存的 LLM 输入输出；显式重新推理创建独立运行，不覆盖历史结果。
- 报告收益、最大回撤、Sharpe/Sortino、胜率、盈亏比、profit factor、换手、敞口、费用、资金费率以及 Provider/周期/标的拆分。

## 测试与验收

- 单元测试覆盖 CLI 检测与解析、Schema 校验、敏感环境清理、超时终止、无前视选币、仓位计算、精度取整和熔断。
- 使用假的 `codex`/`claude` 可执行文件测试缺失、版本不兼容、认证失效、限额、拒绝、非 JSON 输出、工具调用企图和回退。
- 集成测试覆盖行情断线、过期数据、429、部分成交、状态未知、重复事件和重启对账。
- 测试网契约测试验证账户、逐仓/杠杆、下单、止损、撤单和状态回补。
- 发布前连续运行测试网至少 24 小时，无重复订单、无未对账仓位，且每笔交易可追溯到模型输出与风控判定。

## 默认约束

- 首版仅使用币安市场与账户数据，不接入新闻、社交媒体或链上数据。
- 未选择或未登录 LLM Provider 时交易引擎不能启动。
- 本地中文界面只监听 localhost，密钥永不进入 LLM 子进程。
- 未来实盘能力必须作为独立里程碑增加显式解锁和更长时间模拟验证。
- 项目继续使用 GPL-3.0。
