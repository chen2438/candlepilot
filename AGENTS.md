# CandlePilot Agent 协作约定

- `DOCS.md` 是权威文档入口；它索引的 `docs/*.md` 专题与入口共同构成权威功能文档集。开始任务时
  必须完整阅读本文件和 `DOCS.md`，再按其中的“文档路由”完整阅读所有受影响专题；跨领域变更不得
  只读其中一个专题。
- 任何影响系统行为、接口、配置、验证方式或安全边界的变更，都必须在同一个提交中同步更新
  `DOCS.md` 的相关核心摘要及所有受影响专题。实现细节只保留在最合适的专题，其他文档使用链接，
  避免重复维护同一长段说明。
- 每个可独立验收的功能使用一个单独的 Git 提交。
- 每条提交信息必须包含：
  1. 简洁的 Conventional Commit 风格标题；
  2. 一个空行，以及说明“改了什么、为什么改”的有意义 description；
  3. 实现该变更的 Agent 共同作者 trailer。
- Codex Agent 提交必须使用当前实际模型名而非产品名；当前模型的提交必须以
  `Co-authored-by: GPT-5.6 Sol <noreply@openai.com>` 结尾。
- Claude Code 提交必须以其当前适用的 Anthropic 共同作者 trailer 结尾。
- 完全由用户本人实现且没有 Agent 参与的提交可以改以 `Human-authored: true` 结尾；Agent 禁止
  使用或建议冒用该标记来绕过共同作者要求。
- 本地仓库必须使用 `git config core.hooksPath .githooks` 启用版本化 `commit-msg` hook；提交后、
  推送前必须执行 `.venv/bin/python scripts/check_commit_messages.py --commit HEAD` 再次验证 Git 实际解析的
  message。不得用字面量 `\\n` 拼接提交正文或 trailer。
- 禁止只有标题、没有正文的提交。当安全行为、兼容性影响或验证结果对后续维护有实际帮助时，
  必须在 description 中记录。
- 修改 `.env.example` 时，必须在同一次工作中把新增变量同步补入本地 `.env`，但不得覆盖
  `.env` 中任何已有值；不得读取、输出、暂存或提交 `.env` 及其中的凭据。
- 提交前执行 `DOCS.md` 要求的检查；前端变更还必须完成浏览器验证。
- 执行校验命令前必须为每个命令明确核对工作目录：仓库级 Python 命令（包括
  `.venv/bin/ruff`、`.venv/bin/pytest` 和 `scripts/` 下的检查脚本）必须从仓库根目录运行；仅
  `pnpm` 等前端命令进入 `frontend/` 目录。并行执行前后端检查时，每个命令必须分别显式设置
  正确的工作目录，禁止在 `frontend/` 等子目录中调用根目录相对路径 `.venv/bin/...`。
- 每次提交并通过提交信息校验后，必须立即将当前分支推送到其远端上游；若推送失败，必须明确
  告知用户失败原因，不得把仅存在于本地的提交描述为已交付。
