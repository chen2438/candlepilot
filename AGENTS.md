# CandlePilot agent conventions

- `DOCS.md` is the single authoritative feature and architecture document. Update it in the same
  commit whenever a change affects behavior, interfaces, configuration, validation, or boundaries.
- Keep each independently verifiable feature in its own Git commit.
- Every commit message must contain:
  1. a concise Conventional Commit-style subject;
  2. a blank line followed by a meaningful description explaining what changed and why;
  3. the co-author trailer for the agent that implemented the change.
- Codex commits must end with `Co-authored-by: Codex <noreply@openai.com>`.
- Claude Code commits must end with its applicable Anthropic co-author trailer.
- Do not use a subject-only commit. Record relevant safety behavior, compatibility implications,
  and verification in the description when they materially help future maintainers.
- Run the checks required by `DOCS.md` before committing. UI changes also require browser
  validation.

