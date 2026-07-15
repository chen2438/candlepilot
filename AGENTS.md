# CandlePilot Agent 协作约定

- `DOCS.md` 是唯一权威的功能与架构文档。任何影响系统行为、接口、配置、验证方式或安全边界的
  变更，都必须在同一个提交中同步更新 `DOCS.md`。
- 每个可独立验收的功能使用一个单独的 Git 提交。
- 每条提交信息必须包含：
  1. 简洁的 Conventional Commit 风格标题；
  2. 一个空行，以及说明“改了什么、为什么改”的有意义 description；
  3. 实现该变更的 Agent 共同作者 trailer。
- Codex 提交必须以 `Co-authored-by: Codex <noreply@openai.com>` 结尾。
- Claude Code 提交必须以其当前适用的 Anthropic 共同作者 trailer 结尾。
- 完全由用户本人实现且没有 Agent 参与的提交可以改以 `Human-authored: true` 结尾；Agent 禁止
  使用或建议冒用该标记来绕过共同作者要求。
- 本地仓库必须使用 `git config core.hooksPath .githooks` 启用版本化 `commit-msg` hook；提交后、
  推送前必须执行 `python scripts/check_commit_messages.py --commit HEAD` 再次验证 Git 实际解析的
  message。不得用字面量 `\\n` 拼接提交正文或 trailer。
- 禁止只有标题、没有正文的提交。当安全行为、兼容性影响或验证结果对后续维护有实际帮助时，
  必须在 description 中记录。
- 提交前执行 `DOCS.md` 要求的检查；控制台变更还必须完成浏览器验证。
