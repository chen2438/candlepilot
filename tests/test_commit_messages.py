from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_commit_messages.py"
SPEC = spec_from_file_location("check_commit_messages", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CHECKER = module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


def test_accepts_gpt_model_and_claude_trailers() -> None:
    gpt = """feat: add guard

Explain the behavior and why it is needed.

Co-authored-by: GPT-5.6 Sol <noreply@openai.com>
"""
    claude = """fix(core): repair guard

Explain the repair and why it is needed.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
"""

    assert CHECKER.validate_message(gpt) == []
    assert CHECKER.validate_message(claude) == []


def test_accepts_explicit_human_authorship() -> None:
    message = """docs: clarify setup

Explain the setup change and why it is needed.

Human-authored: true
"""

    assert CHECKER.validate_message(message) == []


def test_rejects_literal_newlines_and_unparseable_trailer() -> None:
    message = (
        "feat: add guard\n\n"
        "Explain the behavior.\\n\\n"
        "Co-authored-by: GPT-5.6 Sol <noreply@openai.com>\n"
    )

    errors = CHECKER.validate_message(message)

    assert any("literal" in error for error in errors)
    assert any("final line" in error for error in errors)


def test_rejects_missing_description() -> None:
    message = "feat: add guard\n\nCo-authored-by: GPT-5.6 Sol <noreply@openai.com>\n"

    assert "commit message must include a meaningful description" in CHECKER.validate_message(
        message
    )


def test_rejects_non_conventional_title_and_unknown_identity() -> None:
    message = """Add guard

Explain the behavior.

Co-authored-by: Assistant <noreply@example.com>
"""

    errors = CHECKER.validate_message(message)

    assert "title must use Conventional Commit format" in errors
    assert "Co-authored-by must identify the GPT model or Claude Code" in errors


def test_rejects_generic_codex_product_name() -> None:
    message = """chore: update policy

Explain the policy change and why it is needed.

Co-authored-by: Codex <noreply@openai.com>
"""

    assert "Co-authored-by must identify the GPT model or Claude Code" in (
        CHECKER.validate_message(message)
    )
