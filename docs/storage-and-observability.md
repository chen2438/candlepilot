# 存储、审计、可观测性与成本

> 本专题是 [DOCS.md](../DOCS.md) 索引的权威文档之一。修改数据库、迁移、审计、日志、指标、
> 告警或成本核算时必须完整阅读并同步更新。

## 4.7 审计、存储与溯源

- SQLite 表：`live_runs`（正式运行边界、配置快照与停止原因）、`inferences`（模型推理， nullable
  `live_run_id` 归属正式运行）、`inference_details`（逐次输入与 Prompt）、
  `risk_decisions`、`executions`（实际订单报告）、`execution_attempts`（推理对应的执行结论、失败阶段与损失）、
  `user_stream_events`、`alert_events`、`trailing_stop_events`（影子候选、首次模拟成交、实盘替换与失败）、
  `partial_take_profit_events`（1R 部分止盈、剩余保本与实仓结束影子事件）、
  `runtime_state`、`schema_migrations`。
- 独立研究使用 `market_analyses`：保存状态、标的、Provider/模型/推理强度、Prompt/数据版本、冻结
  输入、实际 Prompt、原始输出、校验后的分析、Token/耗时和安全错误文本。它不关联 `inferences`、
  `risk_decisions` 或订单表，避免研究结果被误当成正式决策；列表默认只返回结果摘要，按 ID 详情才
  返回完整本地审计输入。服务重启会把遗留 pending/running 记录标为 failed；数据管理可用独立
  `market_analyses` 类别删除整类研究历史。
- 旧版 `book_captures` 表为现有数据库兼容而保留，但应用不再写入或把它用于回测；遗留行只能通过
  数据管理清理。含完整盘口的回放数据由正式引擎自动写入 `live_decision_snapshots`。
- 溯源：SHA-256 数据版本、显式 Prompt 版本、模型标识、CLI Provider 版本。
- 每次正式运行创建时把当前仓库 `HEAD` 的 7 位 Git 提交号写入 `live_runs.config_json` 的
  `software_version`，与该次唯一 Provider、周期和运行边界一起永久保留；软件更新不会用新版本
  回填历史运行。无法确认 Git 仓库时该字段省略，前端显示“版本未记录”。这是现有 JSON 配置
  快照的新增字段，不改变 `live_runs` 表结构。
- 实时风控记录可选保存止盈后重入 shadow 评估：最近止盈时间、已过秒数及会命中的 15/30/60 分钟
  候选窗口；它是审计证据，不是拒单条件。
- 正式运行表现把交易所 `rp` 作为价格已实现毛利，按持仓 lot 归属入场与退出的 USDT 手续费，并与
  当前未实现盈亏分列；`net_trading_pnl = gross_price_pnl + unrealized_pnl - commissions`。
  旧事件缺手续费或手续费资产不是 USDT 时 `commission_complete=false`；资金费当前不能从账户级
  事件可靠归属到单次运行，因此 `funding_pnl=null`、`funding_complete=false`，绝不混入
  `total_pnl` 冒充策略收益。兼容字段 `realized_pnl` 继续返回价格已实现毛利，`total_pnl` 现在等于
  可核对的交易净盈亏。
- `market_analysis_outcomes` 以分析 ID 一对一保存最近一次按需计算的计划结果及更新时间；独立表通过
  级联外键随分析历史删除，避免给已部署的 v17 主表做不兼容列重建。
- 数据库基线：历史迁移链已在历史数据清空后压缩，当前 schema v18 在 v17 上新增独立分析结果表；
  v17 在 v16 上新增独立 AI 行情分析审计表；
  v16 在 v15 上新增部分止盈影子审计表；
  v15 在 v14 上新增正式决策快照表；
  v14 在 v13 上新增移动止损审计表；
  现有 v12 数据库先写入 v13 基线标记再创建该表。低于 v12 的数据库明确拒绝启动。新数据库直接由当前 ORM schema 创建，不再重放
  v1–v12 的表重建与字段补丁。`runtime_state` 不随历史清理删除，紧急锁安全状态不受影响。

## 4.8 运维与可观测性

- 应用关闭按“停止调度与引擎 → 取消并等待探测/回测 → 关闭行情、Broker 与数据库”
  的顺序执行；回测会在数据库仍可用时落为 `cancelled`，Provider 有机会终止 CLI/HTTP 调用，
  不允许后台任务在资源关闭后继续运行。

- 健康检查：`/api/health/live`（存活）、`/api/health/ready`（就绪，覆盖迁移版本与
  测试网 Broker 配置）。任一检查不满足时 `/ready` 返回 503；Broker 检查只确认进程已构造交易
  客户端，不向币安发起额外网络请求。
- 结构化日志：HTTP 请求 JSON 日志 + request ID。`httpx` 的成功请求日志提升到 WARNING 门槛，
  避免高频账户轮询把带签名查询串的完整 Binance URL 写入日志；应用自身仍记录必要的请求摘要、
  告警与错误。
- VPS 上服务输出使用 `LogNamespace=candlepilot` 写入独立 systemd journal。清理只轮转并 vacuum
  该命名空间，不影响默认 journal 中的 SSH、Nginx 或其他服务记录；首次从旧部署启用隔离时需要
  root worker 重启一次已停止活动任务的主服务。
- 运行指标：`/api/metrics/runtime` 提供请求量、错误率、并发数、平均/P95 延迟、状态码分布。
- 告警：`/api/alerts` 覆盖紧急锁定、测试网配置/保护/用户流、API 错误率、模型错误率/P95 延迟；
  本地通知渠道对告警首次触发/解除去重后写入 JSON 日志与 `alert_events` 表，
  可经 `/api/alerts/history` 查询。**对外发送到第三方服务（Webhook/邮件/IM）刻意不实现**。
- 测试网账户状态：`/api/testnet/account-status` 提供余额摘要、非零持仓、启动对账与
  用户流状态且不暴露凭据；「交易权限」指标基于**可用保证金**（`availableBalance > 0`），
  因为币安期货账户接口无 `canTrade` 字段。账户摘要 `/fapi/v3/account` 不提供可用标记价，
  因此非零持仓的均价、标记价与未实现盈亏由签名接口 `/fapi/v3/positionRisk` 补全；V3 又按
  Binance 的接口约定移除了杠杆与保证金模式等配置字段，所以这些字段必须由专用签名接口
  `/fapi/v1/symbolConfig` 补全。账户页不再把缺失杠杆默认为 `1×`，也不把缺失保证金模式误报
  为全仓。

## 4.9 成本与用量核算

- 每次模型调用记录 Token 分项与模型名：Codex 从 `--json` 事件流解析
  input/cached/output token，模型名取自 `~/.codex/config.toml`；Claude 从输出解析
  token 与 `total_cost_usd`。
- **等效成本**：Claude 直接用 CLI 自带 `total_cost_usd`；Codex 经 **models.dev** 逐 token
  折算管线（`https://models.dev/api.json`，本地缓存 24h、离线回退，缓存读为输入子集、
  支持长上下文分层）；Custom API 仅在服务响应的 usage 明确提供 `cost` / `cost_usd` 时记录，
  不根据未知后端的模型名猜测价格。
- 订阅计划实际不按次计费，成本仅为**折算估算**；无法定价的模型显示为空。
- `/api/metrics/providers` 聚合 1–720 小时窗口：调用量、错误率、平均/P95 延迟、
  模型分布、Token 用量、等效成本。一次批量推理仍为一次物理 Provider 调用：各标的审计行共享
  `physical_call_id`，调用量、错误数、模型分布和耗时按该 ID 去重，Token 与成本则汇总各行的守恒
  分摊值。旧记录没有该 ID 时按连续的 `batch_index` / `batch_size` 恢复同一口径。只有窗口内全部
  物理调用都可定价时才返回成本总额；否则返回空值并同时给出可定价调用数，避免把部分小计误报为
  完整总成本。
- 每次成功启动引擎都会建立新的运行会话，并以推理审计 ID 记录当前进程内的用量统计边界，同时
  以持久化 `live_run_id` 保存跨重启可追溯的决策归属、启动配置、终态和停止原因。前端每 2 秒通过
  `GET /api/metrics/run-session` 更新本次运行的时长、调用/错误数、未缓存输入/缓存读取/缓存写入/输出/总
  Token、等效成本，以及按调用次数计算的平均模型调用耗时、平均总 Token 和平均等效成本；仅当
  会话内全部调用均可定价时才显示平均成本。优雅停止会先结束调度任务再封存边界，紧急熔断也会
  封存边界。停止后继续
  显示刚结束的会话，边界外的新推理不会混入。当前/上次运行的聚合用量仍是进程内视图；重启后
  不从历史推理猜测该聚合边界，但历史列表仍可依据数据库中的 `live_run_id` 准确分组。
- AI 分析辅助正式周期同时写入 `market_analyses` 与正式推理/风控审计，不新增重复策略表。分析
  `usage` 标记 `analysis_decision_mode=shadow`、同一物理调用 ID 与仅供结果比较的 T2；正式运行配置
  固化同名模式。`RiskDecision.shadow_only` 是最终“不执行”证据，即使确定性风控放行也不会出现
  execution 行。启动试跑不写这些业务审计，避免把容量检查混入正式样本。
  结构化结果校验失败时，历史记录、运行状态和前端只保存/显示去掉输入片段与 Pydantic 文档 URL 的
  有限错误摘要；正式推理失败审计仍保留 Provider 原始输出、Prompt、冻结输入、Token、模型和耗时，
  便于本地复盘而不把整份模型响应塞进错误横幅。
- Provider 的 `input_tokens` 按接口原义显示为“未缓存输入”；例如 Claude 命中提示词缓存时该值
  可能只有个位数，其余输入分别出现在“缓存输入”和“缓存写入”。总 Token 仍包含这些分项，不能
  把很小的未缓存输入误解为只向模型发送了少量上下文。
- `GET /api/live-runs/performance` 按开仓成交把仓位归属到正式运行，总盈亏为该运行仓位的已实现
  盈亏加当前剩余数量按 Binance 标记价计算的未实现盈亏。运行停止不冻结仓位统计：之后发生的
  止损、止盈、模型减仓/平仓或手动平仓仍追溯到开仓运行，把相应数量的未实现盈亏转成交易所返回
  的已实现盈亏；部分平仓后剩余数量继续实时估值。同一标的跨运行形成的同向仓位在 Binance
  单向模式下只有一个加权均价，不能声称某次减仓按 FIFO/LIFO 关闭了特定运行。因此退出数量按各
  运行当时剩余数量同比例消耗，再用每个运行自己的实际开仓价与退出均价分配盈亏；全部数量都可
  归属时仅把交易所报告与理论值之间的精度尾差确定性归入贡献最大的运行。若退出还包含无法归属的
  外部仓位，只计本地已知数量的理论盈亏，不把外部收益赠给某次运行。响应同时按运行返回当前未平仓标的数，同一运行
  对同一标的的多次开仓或加仓去重计为 1，停止后手动平仓会实时减少。前端将胜率指标明确标为
  “已平仓胜率”，按
  “盈利平仓笔数 / 已完成平仓笔数”计算；手动平仓盈利计胜、亏损计负，零笔平仓显示为空。
  前端与账户数据一起每 5 秒刷新。
- 会话内所有调用均可定价时才显示成本总额；若存在未知价格，只显示可定价调用数并将总成本
  留空，避免把部分成本误报为完整成本。零调用会话的成本为 `$0.000000`。
