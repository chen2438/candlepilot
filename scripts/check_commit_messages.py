#!/usr/bin/env python3
"""Validate CandlePilot commit messages locally and in CI."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


CONVENTIONAL_TITLE = re.compile(
    r"^(build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)"
    r"(?:\([a-z0-9._/-]+\))?!?: .+"
)
COAUTHOR = re.compile(r"^Co-authored-by: (.+) <([^<>]+)>$", re.IGNORECASE)
OPENAI_EMAIL = "noreply@openai.com"
CLAUDE_EMAIL = "noreply@anthropic.com"
HUMAN_TRAILER = "Human-authored: true"


def validate_message(message: str) -> list[str]:
    lines = [line.rstrip() for line in message.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()

    errors: list[str] = []
    if not lines:
        return ["commit message is empty"]
    if "\\n" in message:
        errors.append("commit message contains a literal \\n instead of a real newline")
    if not CONVENTIONAL_TITLE.fullmatch(lines[0]):
        errors.append("title must use Conventional Commit format")
    if len(lines) < 2 or lines[1] != "":
        errors.append("title must be followed by a blank line")

    trailer = COAUTHOR.fullmatch(lines[-1])
    if lines[-1] == HUMAN_TRAILER:
        pass
    elif trailer is not None:
        name, email = trailer.groups()
        is_openai_model = name.lower().startswith("gpt-") and email.lower() == OPENAI_EMAIL
        is_claude = name.lower().startswith("claude") and email.lower() == CLAUDE_EMAIL
        if not (is_openai_model or is_claude):
            errors.append("Co-authored-by must identify the GPT model or Claude Code")
    else:
        errors.append(
            "the final line must be a recognized Co-authored-by or Human-authored trailer"
        )

    body = lines[2:-1] if len(lines) >= 3 else []
    if not any(line.strip() for line in body):
        errors.append("commit message must include a meaningful description")
    return errors


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout


def _messages_for_range(base: str, head: str) -> list[tuple[str, str]]:
    if not base or set(base) == {"0"}:
        commits = [head]
    else:
        commits = _git("rev-list", "--reverse", f"{base}..{head}").splitlines()
    return [(commit, _git("show", "-s", "--format=%B", commit)) for commit in commits]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message-file", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--base")
    parser.add_argument("--head")
    args = parser.parse_args()

    if args.message_file is not None:
        messages = [(str(args.message_file), args.message_file.read_text(encoding="utf-8"))]
    elif args.commit is not None:
        messages = [(args.commit, _git("show", "-s", "--format=%B", args.commit))]
    elif args.base is not None and args.head is not None:
        messages = _messages_for_range(args.base, args.head)
    else:
        parser.error("use --message-file, --commit, or both --base and --head")

    failed = False
    for label, message in messages:
        errors = validate_message(message)
        if errors:
            failed = True
            print(f"Invalid commit message: {label}", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
