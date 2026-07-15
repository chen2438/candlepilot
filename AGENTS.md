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
- 禁止只有标题、没有正文的提交。当安全行为、兼容性影响或验证结果对后续维护有实际帮助时，
  必须在 description 中记录。
- 提交前执行 `DOCS.md` 要求的检查；控制台变更还必须完成浏览器验证。
