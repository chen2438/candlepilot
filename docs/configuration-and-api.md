# 配置、CLI 与 API

> 本专题是 [DOCS.md](../DOCS.md) 索引的权威文档之一。新增、删除或修改环境变量、CLI 命令、
> HTTP API 或 WebSocket 协议时必须完整阅读并同步更新；实现细节仍应同时更新对应领域专题。

## 5. 配置

`candlepilot` 启动时自动读取当前目录 `.env`（`doctor`/`serve` 均适用）；
已在 shell 中 `export` 的变量优先级更高。

| 变量 | 说明 |
|---|---|
| `CANDLEPILOT_HOST` / `CANDLEPILOT_PORT` | 绑定地址（仅 localhost）与端口（默认 8000，范围 1–65535）|
| `CANDLEPILOT_DATABASE_URL` | 仅支持指向普通持久文件路径的 `sqlite+aiosqlite` 异步 SQLite 连接串；不接受 `:memory:` 或 `file:` URI，前端保存与启动时均先校验 |
| `CANDLEPILOT_DATA_DIR` | 数据目录（Parquet 行情缓存、models.dev 定价缓存）|
| `CANDLEPILOT_AUTH_ENABLED` | 是否启用控制台鉴权；localhost 开发默认 `false`，VPS 远程访问必须为 `true` |
| `CANDLEPILOT_AUTH_USERNAME` | 鉴权用户名，3–64 个安全字符 |
| `CANDLEPILOT_AUTH_PASSWORD_HASH` | `python -m candlepilot.auth` 生成的 scrypt 哈希；禁止填写明文密码 |
| `CANDLEPILOT_AUTH_SESSION_SECRET` | 至少 32 字符的随机 HMAC 密钥；修改后所有已有会话失效 |
| `CANDLEPILOT_AUTH_SESSION_TTL_SECONDS` | 会话有效期，默认 604800 秒（7 天），范围 300–604800；可在设置页修改，保存并重启后生效 |
| `CANDLEPILOT_AUTH_COOKIE_SECURE` | 是否只允许 HTTPS 发送会话 Cookie；VPS 必须为 `true` |
| `CANDLEPILOT_LLM_TIMEOUT` | 外部 Provider 调用默认硬超时（秒，默认 45，必须为有限正数）；正式运行可在试跑时覆盖并固化本次值 |
| `CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS` | LLM 分析快照允许进入下单前行情刷新的最大年龄（秒，默认 75，必须为正整数）|
| `CANDLEPILOT_CADENCES` | 唯一分析周期，可选 `5m`/`15m`/`30m`/`1h`/`4h`，默认 `15m`；多个值会被拒绝 |
| `CANDLEPILOT_CANDIDATES_PER_CYCLE` | 每周期分析候选池前 N 个标的，默认 5（范围 1–20）|
| `CANDLEPILOT_MAX_RUN_SECONDS` | 单次运行时长上限（秒）；留空=不限，非正数或格式错误会拒绝启动/保存 |
| `CANDLEPILOT_MAX_RUN_COST_USD` | 单次运行等效成本预算（USD）；留空=不限，必须为有限正数，否则拒绝启动/保存 |
| `CANDLEPILOT_TRAILING_STOP_MODE` | 确定性移动止损模式：`off` / `shadow` / `live`，默认 `shadow`；shadow 并行审计五组固定参数并冻结各组首次模拟成交，live 只执行 +2R 激活、距最有利标记价 1R，只有 live 修改交易所止损 |
| `CANDLEPILOT_STRUCTURE_GATE_MODE` | 结构入场门槛：`off` / `shadow` / `enforce`，默认 `shadow`；shadow 只记录逐项检查而不改变订单，enforce 才拒绝不合格开仓/加仓 |
| `CANDLEPILOT_DAILY_LOSS_PERCENT` | 滚动 24 小时亏损熔断百分比，范围 0.1–50，默认 5；前端设置页按百分数填写，保存后重启生效 |
| `CANDLEPILOT_PROVIDER_CHAIN` | 启动时唯一使用的 Provider，例如 `local`、`codex`、`claude-code` 或 `custom:main`；保留该变量名用于兼容现有部署，但值必须恰好一个，不得用逗号配置主备，Custom API ID 必须存在；状态/API 返回的 `local-rule`、`codex-auth`、`claude-code-auth`、`openai-compatible:<id>` 是内部注册名，不允许写回该变量 |
| `CANDLEPILOT_CODEX_MODEL` / `CANDLEPILOT_CODEX_REASONING_EFFORT` | Codex 模型 / 推理强度（minimal/low/medium/high）|
| `CANDLEPILOT_CLAUDE_MODEL` / `CANDLEPILOT_CLAUDE_EFFORT` | Claude 模型 / 强度（low/medium/high/xhigh/max）|
| `CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON` | **全部** Custom API 端点的 JSON 数组（最多 8 个），每项需唯一 `id` 与 `base_url`，注册为 `openai-compatible:<id>` |
| `CANDLEPILOT_ENV_FILE` | `.env` 路径（默认工作目录下 `.env`），加载器与前端设置页共用 |
| `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` | 必需；系统唯一运行模式使用的币安 Demo 测试网凭据 |

## 6. CLI 命令

- `candlepilot doctor` —— 检查 LLM 登录状态与币安只读公共接口，不下单。
- `candlepilot serve` —— 启动本地 API 与已构建的 Web 前端。

## 7. HTTP / WebSocket API 参考

**认证**：`GET /api/auth/status`、`POST /api/auth/login`、`POST /api/auth/logout`。启用鉴权时，
登录与状态接口强制 `Cache-Control: no-store`；登录成功签发有期限的 HttpOnly 会话，退出会立即
清除浏览器 Cookie。健康检查保持匿名可用，供本机进程看护使用；不返回账户、配置或交易信息。
会话是无状态签名 Cookie，后端不保存会话表；失败登录按客户端在滚动 5 分钟窗口内限速，窗口过期的
客户端桶会自动回收，避免长期运行时因历史来源持续占用内存。

**引擎与 Provider**：`GET /api/status`、`GET /api/providers`、
`POST /api/providers/select`、`POST /api/providers/config`、`POST /api/providers/test`、
`GET /api/providers/codex-auth/session`、`POST /api/providers/codex-auth/login`、
`GET /api/providers/codex-auth/usage`、
`POST /api/providers/codex-auth/login/cancel`、`POST /api/providers/codex-auth/logout`、
`POST /api/cadences`、`POST /api/analysis-decision-mode`、
`POST /api/candidates-per-cycle`、`POST /api/run-limits`、
`GET /api/settings`、`POST /api/settings`、`POST /api/engine/probe`、
`POST /api/engine/start`、`POST /api/engine/probe-and-start`、`POST /api/engine/run-once`、
`GET /api/decision-events`（支持逐条 `limit`/`before_id`，以及按完整运行分页的
`run_limit`/`before_run_id` 游标；两种模式均支持 `symbol`/`cadence`/`provider`/`outcome` 筛选）、
`POST /api/engine/stop`、`POST /api/engine/emergency-stop`、
`POST /api/engine/clear-emergency-lock`。

`POST /api/analysis-decision-mode` 接受 `{"mode":"off"}` 或 `{"mode":"shadow"}`，仅允许停机时
切换；`shadow` 要求当前唯一 Provider 是外部模型，选择本地规则返回 422。状态接口返回
`analysis_decision_mode`；切换模式会使现有启动试跑失效。`shadow` 的启动试跑与正式周期使用相同
分析辅助批量路径，正式周期落库分析及决策审计、执行全部硬风控，但不会提交订单。进程启动默认
回到 `off`，不会因重启自动恢复实验模式。

**独立 AI 行情分析**：`POST /api/market-analyses` 接受唯一字段
`{"symbol":"BTCUSDT"}` 并返回 202 与记录 ID；`POST /api/market-analyses/batch` 无请求体，读取正式
引擎当前候选池前 `candidates_per_cycle` 个标的，按排名返回 `analyses: [{id, symbol}]` 并排队串行
分析；候选池为空时先刷新，批量不追加已有持仓。`GET /api/market-analyses?limit=30&symbol=BTCUSDT`
返回最近摘要，`GET /api/market-analyses/{id}` 返回包含冻结输入、Prompt 和原始输出的本地审计详情，
`POST /api/market-analyses/{id}/cancel` 取消当前单项或其所属的整个活动批次。一次只允许一个单项或
批量分析队列；正式引擎、启动试跑、
回测、登录或其他 Provider 任务活动时返回 409，分析活动也反向阻止这些操作。必须已经唯一选择一个
外部 Provider；选中 `local-rule` 返回 422。创建接口仅排队，前端按 ID 轮询终态
`succeeded/failed/cancelled`；批量中单项失败不阻断后续标的，批次取消会把尚未开始的记录一并标为
`cancelled`。分析不调用 Broker 执行接口，也不改变紧急锁、亏损熔断或运行状态。
`POST /api/market-analyses/{id}/outcome` 对已完成记录按需拉取分析后的 5m K 线并保存最新计划结果；
仅当某个 5m 窗口因多个计划价位触发而无法确定顺序时，接口才补取该窗口完整连续的五根 1m K 线
重放同一结果状态机。缺少任一分钟或冲突仍在同一根 1m 内时保持 `ambiguous`。1m 只用于确定性事后
判定，不发送给 Provider；接口不调用模型或 Broker，未完成记录返回 409。详情与列表均返回
`outcome` 和更新时间。
`POST /api/market-analyses/outcomes` 接受 `{"analysis_ids":[1,2]}`，一次最多 30 个正整数 ID，按首次
出现顺序去重并串行执行同一结果判定；响应分别返回 `updated_ids` 与逐项 `errors`。单条不存在、
未完成或行情读取失败不会阻断其余项目，也不会把失败记录冒充已更新。

`POST /api/engine/clear-emergency-lock` 会先执行交易所账户对账；仅停止状态且无持仓、无普通或 Algo
挂单时删除紧急锁，否则返回 409 并保留锁定。

`POST /api/engine/run-once` 接受与启动相同的 `timeout_seconds` 请求体，无需预先调用试跑；它会先完成
账户对账与 Provider 健康检查，再执行一轮正式分析、风控和交易并停止。若此前已有成功试跑，该状态会因
账户可能发生变化而失效。`POST /api/engine/start` 仍强制要求当前参数下尚未消费的成功试跑。
`POST /api/engine/probe-and-start` 也接受同一请求体，并在一个启动锁内依次执行真实批量试跑和持续启动；
只有试跑完整成功才会启动，成功后该试跑立即标记为已消费。

`POST /api/providers/select` 的格式为 `{"providers":["codex-auth"]}`；为兼容既有客户端保留
`providers` 数组，但数组必须恰好包含一个内部注册名，零个或多个均返回 422；`name/backup` 旧结构
同样返回 422。引擎运行时修改返回 409。
`GET /api/providers` 对 Codex 返回当前 `auth_source`、已安装的 `auth_source_options` 和通过认证后
取得的 `account_email`；`POST /api/providers/config` 可附带
`{"name":"codex-auth","auth_source":"chatgpt-app"}` 或 `codex-cli`。来源切换只影响当前服务进程，
与模型或推理强度修改一样会使已有正式运行试跑失效；运行中、试跑中或回测中禁止切换。
Codex CLI 登录接口异步启动固定的 `codex login --device-auth`，session 接口返回
`starting/pending/succeeded/failed/cancelled/idle` 状态、经域名校验的 OpenAI/ChatGPT HTTPS
授权地址、一次性代码和安全摘要；不返回命令原始输出、token 或 `auth.json`。取消接口终止当前
进程组；登出接口固定执行 `codex logout`，前端要求二次确认。登录态改变会使已有正式运行试跑
失效；引擎、试跑、回测或另一登录任务活动时返回 409。登录态的四个接口都受控制台会话与同源写请求保护，
额度读取接口受控制台会话保护。
`GET /api/providers/codex-auth/usage` 通过独立 CLI 的 stdio app-server 读取当前额度，响应带
`Cache-Control: no-store`，只包含安全消息、检查时间，以及额度桶的套餐、名称和实际存在的窗口
（类型、已用/剩余百分比、时长、重置时间）。不返回认证凭据或原始协议字段；CLI 不支持实验接口、
未登录、超时或响应异常时仍返回 200、`available=false` 和空桶，使展示失败不影响 Provider。
`GET /api/status` 为兼容既有客户端继续通过单元素 `provider_chain` / `provider_routes` 以及
`active_provider` 返回所选 Provider、当前承载、重试冷却截止时间与最近成功/失败时间；不返回任何凭据。

`POST /api/engine/probe` 接受 `{"timeout_seconds":60}`；所选外部 Provider 使用该次绝对超时，
省略时继承当前 Provider 配置，本地规则可传 `null`。接口同步完成上述 1 次真实批量试跑和容量校验，
但不创建运行会话或启动调度；失败时等待调用完整退出后恢复超时，不能遗留继续计费或占用单并发锁的
后台调用。`POST /api/engine/start` 接受相同请求结构，只消费当前参数下尚未使用的成功
试跑并执行账户启动对账；缺少试跑、试跑已使用、试跑后修改参数或请求超时不匹配均返回 409。

**行情与选币**：`GET /api/universe`、`POST /api/universe/refresh`、
`GET /api/market/klines`、`GET /api/market/funding-rates`、
`GET /api/market/backtest-candles`。

**决策与信号**：`GET /api/decision-events`、`GET /api/decision-events/{inference_id}`、
`GET /api/signals`。不提供接受调用方行情、账户或合约规则的手动决策接口；正式执行只能由调度器
使用后端刚读取的数据发起，避免本地请求用伪造精度或快照绕过执行链。列表接口只返回轻量摘要；按 ID
详情接口返回该次推理的结构化输入、实际 Prompt、原始输出、token usage 和等效成本。
`decision-events` 以模型推理为主记录，关联硬风控和执行尝试并给出 `approved` / `executed` /
`execution_failed` / `rejected` / `hold` / `analysis_only` 展示状态；执行对象包含状态、失败阶段、
交易所错误、入场/回补报告与可用时的损失估算。归属正式运行时，`live_run.config.software_version`
返回运行创建时持久化的 7 位 Git 提交号；旧运行或非 Git 安装可省略，调用方不得以当前版本代填。
`signals` 保留为原始推理查询，不推断订单是否成交。

**账户与风险**：`GET /api/account/portfolio`、`GET /api/account/positions`、
`POST /api/account/positions/close`、
`GET /api/orders`、`GET /api/fills`、`GET /api/risk-events`。前两个账户接口读取币安测试网钱包、
保证金和非零持仓；系统将过去 24 小时交易流水与当前未实现盈亏合并为 `pnl_24h`，不再按 UTC 日期切换
清零。Broker 的统一账户快照把账户摘要与
Position Risk v3 及 Symbol Config 合并：持仓数量与保证金来自账户摘要，均价、标记价和未实现
盈亏取实时风险字段，杠杆与逐仓/全仓模式取专用配置字段；
前端账户接口与正式决策的组合输入必须读取同一份快照，不允许正式决策绕过补全后再直接索引
`entryPrice`。风险接口若确实缺少非零持仓均价，返回带标的名的账户对账错误而非裸 `KeyError`。
测试网保护单由交易所托管；账户持仓列表根据启动对账结果标明“交易所侧 / 缺失 / 待确认”，
并从 `openAlgoOrders` 回读 CandlePilot `closePosition` 括号单的真实止损/止盈触发价。测试网账户
相关接口共享 1 秒查询缓存，将同一轮前端并发轮询合并为一组账户摘要与持仓风险签名请求。
`GET /api/orders` 读取本地执行审计；`GET /api/fills` 优先读取本地持久化的币安用户流最终成交，
覆盖入场与交易所触发的保护性退出，并以执行审计补充尚无用户流事件的 `FILLED` 记录。成交响应
额外包含 `side`、`purpose`、`reduce_only`、`realized_pnl`、`notional_usdt`、
`realized_pnl_margin_usdt`、`realized_return_percent`、`related_client_order_id` 与 `source`；
其中 `purpose` 为 `entry`、`stop_loss`、`take_profit`、`manual_close`、`rescue_close`、
`model_close`、`model_reduce` 或 `other_close`。普通客户端订单号不能单独证明是开仓：有执行审计的
reduce-only 成交按关联决策区分模型平仓/减仓，无法关联的 reduce-only 成交显示为其他平仓；退出
成交的已实现盈亏与回报率不因订单号缺少保护单后缀而隐藏。前端同时以 `reduce_only` 兜底，旧版
或异常响应即使把退出用途写成 `entry`，也只能显示为其他平仓，不能伪装成开仓。缺少用户流
事件的手动平仓，以及用户流离线期间触发的 CandlePilot 止损、止盈或回补，会按上文规则从交易所
成交历史补录；`source` 由此增加 `exchange_rest_reconciliation`。
实时用户流与 REST 成交补录可能以相差数毫秒的时间戳描述同一笔最终成交；持久化与成交查询均按
交易所订单号（缺失时使用客户端订单号）、最终状态和累计成交量做语义去重，并优先保留信息更完整的
实时用户流事件。不同累计成交量的部分成交进度仍分别保留，不能因时间戳相同而合并。

`POST /api/account/positions/close` 接受 `{"symbol":"BTCUSDT"}`，仅在引擎停止时执行该标的
全部当前仓位的 reduce-only 市价平仓。成功返回交易所成交状态、成交数量、均价、客户端订单号与
时间；无持仓/引擎运行返回 409，成交、归零验证或 CandlePilot 保护单清理不完整返回 502。
`GET /api/trailing-stops/history` 返回最近 1–500 条移动止损影子候选、首次模拟成交、实盘应用、错过与失败审计，
每条事件包含参数组 ID、激活 R 与回撤 R；首次模拟成交还返回候选触发价及 5 秒看护观察到的穿越价。
账户页自动读取最近 100 条并每 5 秒刷新，无需手工打开 API。
`GET /api/status` 的 `scheduler.trailing_stop` 返回当前模式、参与计算的参数组、管理/已激活仓位及
参数组数量和最近事件。
`GET /api/partial-take-profits/history` 返回最近 1–500 条固定影子实验事件，包括 1R 部分模拟成交、
剩余仓位保本模拟成交、数量不可执行及实仓先结束；事件包含参数组、交易所步长对齐后的部分数量、
目标价、首次观察价、模拟成交价和未扣费价格毛利。`GET /api/status` 的
`scheduler.partial_take_profit` 返回两组固定参数与当前活动生命周期计数；该实验没有实盘开关，
不会修改真实订单。
`GET /api/live-runs/performance` 对每次正式运行返回 `gross_price_pnl`、`unrealized_pnl`、
`commissions`、`commission_complete`、`funding_pnl`、`funding_complete`、`net_trading_pnl` 与
兼容字段 `total_pnl`；无法可靠归属的资金费返回未知，不计入交易净盈亏。

**测试网**：`GET /api/testnet/events`、`GET /api/testnet/account-status`。

**回测**：`GET /api/backtests/probe`、`POST /api/backtests/probe`、
`POST /api/backtests/probe/cancel`、`POST /api/backtests/estimate`、`POST /api/backtests`（202）、
`GET /api/backtests`、`GET /api/backtests/{id}`、`GET /api/backtests/{id}/decisions`、
`POST /api/backtests/{id}/cancel`。自选历史窗口没有订单流；完整盘口输入只来自正式引擎自动保存的
运行数据集并通过 `replay_live_run_id` 回放。采集器 API 与 `use_recorded_book` 请求字段已移除，
旧客户端继续发送该字段会返回 422。

**运维**：`GET /api/health/live`、`GET /api/health/ready`、`GET /api/metrics/runtime`、
`GET /api/metrics/providers`、`GET /api/metrics/run-session`、`GET /api/alerts`、
`GET /api/alerts/history`、`GET /api/trailing-stops/history`、`GET /api/update/status`、
`POST /api/update/check`、`POST /api/update`、`GET /api/backups`、`POST /api/backups/refresh`、
`POST /api/backups/{id}/delete`、`GET /api/logs`、`POST /api/logs/clear`、`POST /api/restart`。

`POST /api/update/check` 使用固定 Git 参数读取当前分支与提交，从无内嵌凭据的 GitHub HTTPS
`origin` 获取该分支并验证远端是当前提交的快进后继；响应返回检查时间、分支、新旧提交和
`update_available`，不启动 root 更新器或改动工作树。`POST /api/update` 仍是独立安装动作，
继续由受限 root 助手执行备份、安装、健康检查与失败回滚。

`GET /api/backups` 只读取 root worker 写入的脱敏清单和维护状态，不遍历 root-only 备份目录。
`POST /api/backups/refresh` 请求 worker 重建清单；`POST /api/backups/{id}/delete` 只接受清单中严格
格式且未受保护的 ID，并与更新动作互斥。API 预检不是唯一安全边界：root worker 会再次解析实际
目录，拒绝符号链接、根目录越界、最新备份和仅存的一份备份。

`GET /api/logs` 只读取 root worker 写入的阶段、时间及清理前后分配字节数；
`POST /api/logs/clear` 没有请求体，只能排队固定的 `--clear-logs` 动作。活动交易或模型任务以及
更新、备份、重启期间拒绝该动作；root worker 只操作 `candlepilot` systemd journal 命名空间，
应用进程不能指定日志路径、systemd unit 或任意命令。

**数据管理**：`POST /api/history/clear`。

**实时**：`WS /ws/events`（连接成功后依次发送当前 `status` 与最近 10 次运行的 `decisions`
快照；此后每 2 秒推送引擎状态，仅在决策发生变化时再次推送 `decisions` 事件）。
