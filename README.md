# CandlePilot

CandlePilot 是一个本地优先、可审计的 LLM 日内交易系统，当前专注于币安
USDⓈ-M USDT 永续合约。首版只支持历史回测、生产行情模拟成交和币安测试网；
**没有真钱实盘入口**。

完整设计与风险边界见 [PLAN.md](PLAN.md)。

## 已实现

**行情与决策**

- 动态扫描币安全市场 USDT 永续，按成交额、价差、波动和趋势选出候选池。
- 同时支持 1m、5m、15m 决策周期，统一生成 EMA、RSI、ATR、收益率、成交量以及基差、
  持仓量、盘口与近期成交失衡等微观结构特征。
- Codex App Auth：优先检测 `/Applications/Codex.app/Contents/Resources/codex`，
  不可用时回退到 `PATH` 中的 `codex`。
- Claude Code Auth：检测独立 `claude` CLI；不读取或复制 OAuth 凭证。
- LLM 输出严格 `TradeIntent`，随后经过不可绕过的仓位、止损、杠杆和账户级风控。
- Provider 显式主备切换、统一取消、超时和单并发限制；币安密钥永不进入 LLM 子进程。

**执行、回测与审计**

- SQLite 审计、模拟账户、事件驱动回测、测试网签名下单和中文 React 控制台。
- 测试网安全下单、启动对账、部分成交保护和用户数据流审计。
- 单标的与等权组合回测，含 Sharpe/Sortino、换手、敞口，以及方向、退出原因和市场状态
  （趋势/震荡/高波动）分组统计与权益曲线。
- 数据、Prompt、模型与 CLI 版本记录，顺序数据库迁移。
- `candlepilot acceptance` 按发布不变量审计测试网软运行，证据不足绝不虚假放行。

**控制台与运维**

- 账户与风险面板：权益、持仓、订单成交与风控决策查询接口及页面。
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
/Applications/Codex.app/Contents/Resources/codex --version 2>/dev/null || codex --version
```

Claude Desktop App 不提供本项目所需的无人值守结构化接口；若要使用 Claude Code
Auth，需要另行安装 `claude` CLI，并用与 Desktop 相同的 Claude.ai 账号登录。

## 配置与启动

```bash
cp .env.example .env
set -a; source .env; set +a
candlepilot doctor
candlepilot serve
```

浏览器访问 [http://127.0.0.1:8000](http://127.0.0.1:8000)。在控制台选择已认证
Provider，刷新候选池，然后启动引擎。启动后调度器会对齐 1m/5m/15m K 线边界。

`doctor` 会检查 LLM 登录状态和币安只读公共接口，不会下单。

## 测试网

仅在需要测试网下单时设置：

```bash
export CANDLEPILOT_MODE=binance-testnet
export BINANCE_TESTNET_API_KEY='...'
export BINANCE_TESTNET_API_SECRET='...'
```

测试网 Broker 硬性拒绝生产交易地址。凭证不会传入 Codex/Claude 子进程，也不会写入
数据库和日志。

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
