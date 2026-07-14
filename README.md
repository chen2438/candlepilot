# CandlePilot

CandlePilot 是一个本地优先、可审计的 LLM 日内交易系统，当前专注于币安
USDⓈ-M USDT 永续合约。首版只支持历史回测、生产行情模拟成交和币安测试网；
**没有真钱实盘入口**。

完整设计与风险边界见 [PLAN.md](PLAN.md)。

## 已实现

- 动态扫描币安全市场 USDT 永续，按成交额、价差、波动和趋势选出候选池。
- 同时支持 1m、5m、15m 决策周期，统一生成 EMA、RSI、ATR、收益率和成交量特征。
- Codex App Auth：优先检测 `/Applications/Codex.app/Contents/Resources/codex`，
  不可用时回退到 `PATH` 中的 `codex`。
- Claude Code Auth：检测独立 `claude` CLI；不读取或复制 OAuth 凭证。
- LLM 输出严格 `TradeIntent`，随后经过不可绕过的仓位、止损、杠杆和账户级风控。
- SQLite 审计、模拟账户、事件驱动回测、测试网签名下单和中文 React 控制台。

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

## 验证

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest -q
cd frontend && pnpm run build
```

这是实验性交易软件，不构成投资建议，也不承诺盈利。项目使用 GPL-3.0。
