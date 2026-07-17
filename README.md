# CandlePilot

CandlePilot 是一个本地优先、可审计的 LLM 日内交易系统，当前专注于币安
USDⓈ-M USDT 永续合约。**币安测试网是唯一交易的账户，没有真钱实盘入口。**

完整功能、接口、设计与风险边界见 [DOCS.md](DOCS.md)。

## 已实现

**行情与决策**

- 动态扫描币安全市场 USDT 永续，按成交额、价差、波动和趋势选出候选池。
- 决策周期 5m / 15m / 30m（可任选子集），统一生成 EMA、RSI、ATR、收益率、成交量、
  日线结构位，以及基差、持仓量、盘口与近期成交失衡等微观结构特征。
- Codex App Auth：优先检测 ChatGPT App 与旧版 Codex App 的内置 `codex`，
  不可用时回退到 `PATH` 或 `~/.local/bin` 中的独立 CLI。
- Claude Code Auth：检测 `PATH` 或 `~/.local/bin` 中的独立 `claude` CLI；不读取或复制
  OAuth 凭证。
- 自带 Key 的 OpenAI-compatible 服务：最多 8 个端点，每个成为独立可选的 Provider。
- LLM 输出严格 `TradeIntent`，随后经过不可绕过的仓位、止损、杠杆和账户级风控。
- Provider 显式主备切换、统一取消、超时和单并发限制；币安密钥永不进入 LLM 子进程。

**执行、回测与审计**

- 测试网安全下单：开仓强制止损+止盈，成交后自动挂交易所侧保护括号单；启动对账、
  部分成交保护和用户数据流审计。
- 回测重放实盘同一条决策链路（同一套特征、Prompt、风控），只有撮合是仿真的：
  1～3 天窗口、最多 5 个标的、最多 4 个模型并行对比盈亏/胜率/回撤/交易数。
- 盘口采集器：币安不提供历史盘口，所以订单流只能在它发生时录下来（每 5 分钟一次，
  最多 8 个标的，不调模型、不下单）。录过的窗口可跑「真实回测」，payload 与实盘同构；
  未录过的只能跑普通回测，此时 Prompt 会明确告知模型订单流缺失。
- SQLite 审计、中文 React 控制台、数据/Prompt/模型/CLI 版本记录，顺序数据库迁移。
- `candlepilot acceptance` 按发布不变量审计测试网软运行，证据不足绝不虚假放行。

**控制台与运维**

- 决策与风控面板：将 LLM 意图和对应硬风控结果合并展示、按条件筛选、向前翻页并展开
  审计详情；账户页专注于权益、持仓和订单成交。
- 模型与测试网运维面板：Provider 延迟/调用量/错误率、脱敏测试网账户状态。
- 存活/就绪健康检查、JSON 结构化日志、运行指标，以及告警规则与本地去重通知/历史。
- 锁定的 Python/前端依赖与 GitHub Actions CI（Ruff、Pytest、前端构建）。

## 本地安装

需要 Python 3.12+ 和 Node.js 20+。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

cd frontend
pnpm install
pnpm run build
cd ..
```

如需可复现的锁定版本环境，改用 `requirements.lock`（前端对应 `frontend/pnpm-lock.yaml`）：

```bash
pip install -r requirements.lock
pip install -e . --no-deps
```

Codex App 已安装时通常不需要单独安装 Codex CLI。运行以下命令确认：

```bash
/Applications/ChatGPT.app/Contents/Resources/codex --version 2>/dev/null \
  || /Applications/Codex.app/Contents/Resources/codex --version 2>/dev/null \
  || ~/.local/bin/codex --version 2>/dev/null \
  || codex --version
```

Claude Desktop App 不提供本项目所需的无人值守结构化接口；若要使用 Claude Code
Auth，需要另行安装 `claude` CLI，并用与 Desktop 相同的 Claude.ai 账号登录。

## 配置与启动

```bash
cp .env.example .env
candlepilot doctor
candlepilot serve
```

`candlepilot` 启动时会自动读取当前目录的 `.env`（`doctor`、`serve`、`acceptance` 都适用），
无需手动 `source`。已在 shell 中 `export` 的变量优先级更高，会覆盖 `.env`。

浏览器访问 [http://127.0.0.1:8000](http://127.0.0.1:8000)。在控制台选择 Provider 并调整
主备顺序，按需选择要分析的决策周期（5m/15m/30m 的任意子集，默认全部）和每周期
分析的候选标的数（默认前 5，范围 1–20），刷新候选池，然后启动引擎。启动后调度器
只对齐并运行被选中的 K 线周期，且每周期只评估排名靠前的 N 个标的。控制台按
总览 / 账户 / 回测 / 运维 / 数据 / 设置 六个标签页组织，实时状态推送在切换标签时保持不断。

控制台的提示层依赖 CSS 锚点定位，因此需要 Chrome 131+ 或 Safari 26+。Firefox 尚未实现该特性，
提示会回落到静态位置且不跟随滚动；交易、风控与数据均不受影响。

`doctor` 会检查 LLM 登录状态和币安只读公共接口，不会下单。

除 Codex/Claude 订阅认证外，也可在被 Git 忽略的 `.env` 中配置自带 Key 的
OpenAI-compatible Chat Completions 或 Responses 服务。所有端点写在一个 JSON 数组里
（最多 8 个），每个条目需要唯一的小写 `id`，并成为 Provider `openai-compatible:<id>`：

```bash
CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON=[{"id":"main","base_url":"https://provider.example/v1","api_key":"...","model":"your-model-id","wire_api":"responses"}]
CANDLEPILOT_PROVIDER_CHAIN=codex,claude-code,custom:main
```

外部 Base URL 必须使用 HTTPS；本机回环服务可用 HTTP。Key 只由后端从环境变量读取，
不会通过控制台/API 回传或写入日志、数据库。每个条目的可选字段：`model`、
`reasoning_effort`、`wire_api`（`chat-completions` 默认 / `responses`）、`pricing`、
`require_api_key`、`extra_headers`。不使用 Bearer Key 的服务设 `"require_api_key":false`，
并用 `extra_headers` 配置服务商要求的自定义请求头。配置后可在控制台点击「测试」验证实际调用。

早期的单端点变量（`CANDLEPILOT_CUSTOM_LLM_BASE_URL` 等）已移除。它们若仍留在 `.env` 中，
启动会直接报错并指向上面的替代写法，而不是静默丢掉你以为已配置好的 Provider。

## 测试网

**必填。** 测试网是唯一被交易的账户，缺少凭证时后端拒绝启动。在
[testnet.binancefuture.com](https://testnet.binancefuture.com) 申请，写入 `.env` 或在
shell 中 `export` 均可：

```bash
BINANCE_TESTNET_API_KEY=...
BINANCE_TESTNET_API_SECRET=...
```

测试网 Broker 硬性拒绝生产交易地址。凭证不会传入 Codex/Claude 子进程，也不会写入
数据库和日志。绝不要在此处填入生产 Binance 凭证。

早期的 `CANDLEPILOT_MODE` 已移除：模拟成交与回测运行模式都已删除，回测改为按需分析而非
运行模式。该变量若仍留在 `.env` 中，启动会直接报错——把它删掉即可。

连续运行测试网后，可用审计工具检查发布不变量（连续运行 ≥24 小时、订单号唯一、持仓已
对账且带保护、每笔成交可追溯到模型与风控）：

```bash
candlepilot acceptance --required-hours 24
```

证据不足时该命令返回非零退出码，绝不虚假标记验收通过。

## 验证

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
cd frontend && pnpm run build
```

GitHub Actions（`.github/workflows/ci.yml`）在每次 push 和 PR 上运行相同的检查。

这是实验性交易软件，不构成投资建议，也不承诺盈利。项目使用 GPL-3.0。
