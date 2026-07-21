# 运行、验证与 VPS 运维

> 本专题是 [DOCS.md](../DOCS.md) 索引的权威文档之一。修改依赖、验证流程、CI、VPS 安装、
> 更新、回滚、卸载或远程登录流程时必须完整阅读并同步更新。

## 8. 运行与验证

安装与启动见 [README.md](../README.md)。验证命令：

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
pnpm --dir frontend run test
pnpm --dir frontend run build
.venv/bin/python scripts/check_commit_messages.py --commit HEAD
```

首次克隆后执行 `git config core.hooksPath .githooks`，启用版本化 `commit-msg` hook。该 hook
会在提交创建前要求 Conventional Commit 标题、空行后的 description，以及位于最后一行的
归属 trailer。Agent 实现的提交使用 GitHub 可识别的实际模型名或 Claude Code `Co-authored-by`；
完全由用户本人实现、没有 Agent 参与的提交使用 `Human-authored: true`，Agent 不得冒用该标记。
包含字面量 `\\n` 的错误消息会被拒绝。GitHub Actions CI（`.github/workflows/ci.yml`）会对每次
push/PR 的所有新增提交重复执行同一校验，即使本地 hook 被绕过也会失败；其余 CI 检查同样
运行上述 Ruff、Pytest、前端 Vitest 和构建。CI 使用 Node.js 24，并采用以 Node.js 24 为
执行运行时的 `actions/checkout@v7`、`actions/setup-node@v7`、`actions/setup-python@v6` 与
`pnpm/action-setup@v6`，不再依赖 GitHub 已弃用的 Action Node.js 20 运行时。后端 CI 使用
Pytest 逐测试详细输出，便于定位托管 Runner 上的停滞点；整个后端任务最长运行 10 分钟，超过
上限会明确超时结束，避免无进度任务长期占用 Runner。
Python 依赖锁定于 `requirements.lock`，前端锁定于 `frontend/pnpm-lock.yaml`。

### 8.1 Linux VPS 一键安装

仓库提供 `scripts/install_vps.sh`，支持 Ubuntu 24.04、Debian 12 或 Debian 13 VPS。Ubuntu 使用
系统 Python 3.12，Debian 13 使用系统 Python 3.13；Debian 12 的系统 Python 3.11 不满足项目
要求，因此脚本下载并校验固定版本的 `uv`，由其在应用目录内安装隔离的 CPython 3.12.13，既不
替换 `/usr/bin/python3`，也不依赖或修改 VPS 上已有的 Conda 环境。三种系统安装相同的锁定项目
依赖。`CANDLEPILOT_UV_VERSION` 与 `CANDLEPILOT_MANAGED_PYTHON_VERSION` 可覆盖固定版本，但通常
不应修改。Debian 12 的 `uv` 调用固定以应用目录为工作目录并禁用外部配置发现，root 家目录或
系统中的 `uv.toml` 不会介入应用用户的安装。脚本必须由 root 执行，会创建独立 `candlepilot`
用户，把仓库安装到
`/opt/candlepilot`，安装 Node.js 24、pnpm 与 Codex CLI，构建前端，并创建 systemd 服务。后端仅
绑定 loopback：优先使用 8000，已占用时从 18000–18099 选择第一个空闲端口；显式设置
`CANDLEPILOT_BACKEND_PORT` 时要求端口有效且未被占用。Nginx 使用实际选定端口转发 REST 与
WebSocket，并在用户指定的公网端口提供 HTTPS。脚本生成包含 VPS IP SAN 的自签名证书，完成后输出 SHA-256
指纹；首次访问只有在核对该指纹后才能接受浏览器警告。安装目录先以应用用户身份创建并保留，
Git 直接克隆到该空目录，应用用户不需要也不能在 root 所有的 `/opt` 下自行创建目录。其他发行版
或版本会在修改系统前被拒绝。脚本写入站点配置后会重载已由系统包启动的 Nginx（未运行时则
启动），并通过本机 HTTPS 反向代理检查 `/api/health/ready`；只有公网监听配置实际生效后才会
报告安装成功。

同一脚本同时是已安装实例的更新入口。检测到完整的 `/opt/candlepilot` Git 安装后，脚本不再进入
首次安装流程，也不会重新询问、生成或覆盖管理员密码、Binance 凭据、`.env`、TLS 与 Codex 登录
状态。更新模式要求应用用户、虚拟环境、systemd 服务和前端工程均完整，且服务用户、工作目录与
Git origin 必须和安装参数一致；只允许当前安装分支到 `origin/<branch>` 的快进更新。已跟踪文件
存在本地修改、分支不一致、安装不完整或 `.env` 当前指向的 SQLite 数据库仍有 `running` 运行记录时直接拒绝。
未跟踪和被忽略的 `.env`、数据库、`data/`、隔离
Python 与缓存不由 Git 改写。

确认更新后，脚本先在 `/var/backups/candlepilot/<UTC 时间>-<原提交>` 保存权限为 0600 的 `.env`、
SQLite 在线一致性备份和原提交号，再停止原先处于 active 状态的服务，快进代码，按锁文件更新
Python/前端依赖并重新构建前端。原服务此前 active 时，更新后必须重新启动并通过 loopback
`/api/health/ready`；此前 inactive 时则保持 inactive。Git 更新、依赖、构建、启动或健康检查任一
阶段失败，会恢复原提交和该配置路径的 SQLite 备份、重装原锁定依赖、重建原前端，并按更新前状态恢复
服务；备份不会自动删除。数据库必须配置为普通持久文件路径，`:memory:` 与 `file:` URI 会在更新
修改任何文件前被拒绝，避免遗漏数据库备份或锁错文件。更新模式不重写 Nginx、TLS、主应用 systemd unit 或操作系统包，但会
安装/刷新网页更新专用的 root-owned launcher、worker、安装器副本、配置、systemd tmpfiles 请求
目录、`candlepilot-update.path` 和 `candlepilot-update.service`；同时删除旧版本遗留的 updater
sudoers 授权。这些文件从当前 Git 提交读取，worker 只执行
`/usr/local/libexec/candlepilot-install-vps`，不会执行 `candlepilot` 用户可修改的工作树脚本。
可通过 `CANDLEPILOT_UPDATE_CONFIRM=UPDATE` 跳过交互确认，通过
`CANDLEPILOT_UPDATE_BACKUP_ROOT` 修改备份根目录。

```bash
curl -fsSL https://raw.githubusercontent.com/chen2438/candlepilot/main/scripts/install_vps.sh \
  | sudo bash
```

现有 VPS 从不含网页更新助手的旧版本升级时，必须**最后执行一次**上面的终端命令，让 root 安装
受限助手；即使代码已经是最新版本，安装器也会补装/刷新助手。此后在前端「设置 → 软件更新」中
停止所有活动任务后即可检查并安装后续 `main` 快进更新，无需再次登录终端。网页会显示更新结果、
提交变化和备份目录。更新日志只对 root 开放：

```bash
sudo journalctl -u candlepilot-update.service -n 100 --no-pager
sudo tail -n 100 /var/log/candlepilot-update.log
```

状态摘要保存在 root 所有、只读公开的 `/var/lib/candlepilot/update-status.json`；完整安装输出不会
通过 API 返回。更新服务最长运行 45 分钟，使用文件锁和 systemd unit 状态防止并发执行。

交互过程要求填写公网 IPv4、控制台密码和 Binance Demo Key/Secret；也可预先设置
`CANDLEPILOT_PUBLIC_IP`、`CANDLEPILOT_PUBLIC_PORT`、`CANDLEPILOT_BACKEND_PORT`、`CANDLEPILOT_ADMIN_USERNAME`、
`CANDLEPILOT_ADMIN_PASSWORD`、`BINANCE_TESTNET_API_KEY`、`BINANCE_TESTNET_API_SECRET`，用于可信的
自动化安装。脚本拒绝覆盖已有 `/opt/candlepilot`，不会把密码或密钥打印到日志；生成的 `.env`
权限为 0600。默认路由为 `local`，保证未登录外部模型时服务仍可启动；使用 Codex Auth 前执行：

```bash
sudo -iu candlepilot codex login --device-auth
sudo -iu candlepilot codex login status
sudo systemctl restart candlepilot
```

VPS 是无图形界面的远程设备，`--device-auth` 会在 SSH 终端显示登录网址和一次性
设备码。用户在自己电脑的浏览器中打开该网址，登录要供 CandlePilot 使用的 ChatGPT
账号并输入设备码，然后回到 SSH 终端等待命令完成。必须通过 `sudo -iu candlepilot`
登录，不得以 root 身份直接运行 `codex login`；否则凭据会保存到 root 的 home，
`candlepilot` systemd 服务无法读取。登录成功后用 `codex login status` 核对认证状态，
并重启服务使 Provider 状态立即刷新。

访问地址为 `https://<VPS-IP>:8443`（端口可覆盖）。只开放 Nginx 公网端口，不得开放实际选择的
loopback 后端端口。
服务日志用 `journalctl -u candlepilot -f` 查看。若 UFW 已经启用，脚本只增加所选 TCP 端口规则，
不会擅自启用或重置防火墙。重置密码时运行
`sudo -u candlepilot /opt/candlepilot/.venv/bin/python -m candlepilot.auth` 生成新哈希，替换 `.env`
中的 `CANDLEPILOT_AUTH_PASSWORD_HASH` 后重启服务；密码哈希参与会话签名，因此旧会话自动失效。

卸载前可先预览将被删除的资源：

```bash
curl -fsSL https://raw.githubusercontent.com/chen2438/candlepilot/main/scripts/uninstall_vps.sh \
  | sudo bash -s -- --dry-run
```

去掉 `--dry-run` 后，脚本会单独询问是否删除 `candlepilot` Linux 用户及其 home（其中可能包含
Codex 登录状态），并要求输入 `REMOVE` 才执行。卸载会停止并移除 CandlePilot 主服务和网页更新
服务、root 更新助手、systemd path/tmpfiles 请求通道、旧版 sudoers 授权、更新状态/日志、Nginx
站点、TLS 配置和应用目录
（Debian 12 的隔离 Python 与 `uv` 也在其中）；不会卸载共享的
Nginx、系统 Python、Node.js、pnpm、Codex CLI 或 Git，也不会删除可能与其他服务共用的防火墙
规则。无人值守卸载可设置
`CANDLEPILOT_UNINSTALL_CONFIRM=REMOVE` 与 `CANDLEPILOT_REMOVE_APP_USER=true|false`。

API 回归测试按职责拆分：`tests/test_api_runtime.py` 覆盖正式运行、账户、Provider、配置与
通用控制接口，`tests/test_api_backtest.py` 覆盖回测、试跑、采集与回测历史接口；两者只复用
`tests/api_fixtures.py` 中不被 Pytest 单独收集的假 Provider、行情和账户适配器。新增 API 用例
必须进入对应领域文件，禁止重新堆回单个综合测试文件，也不得为缩短耗时删除安全与并发回归。
前端使用 Vitest、jsdom 与 Testing Library 执行运行时交互测试；第一组用例覆盖账户页市价平仓的
二次确认、回调参数以及引擎运行时禁用边界。新交互不能只靠 TypeScript 构建验证，必须在对应
`*.test.tsx` 中覆盖用户可观察的状态变化，并继续完成真实浏览器检查。

纯人工提交示例（只有确实没有 Agent 参与时使用）：

```bash
git commit \
  -m "fix: correct account label" \
  -m "Correct the label so it matches the underlying account source." \
  -m "Human-authored: true"
```
