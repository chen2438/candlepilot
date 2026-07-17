"""The console's CSS fails silently: a wrong selector still builds and ships.

These guard the layout invariants that no type checker or bundler can see.
"""

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "src"

# A panel class is a top-level card: `.hero`, `.universe-panel`, `.signals-panel`.
# Every one of them is a child of a `.grid` section and must stay in flow.
PANEL_SELECTOR = re.compile(r"^\.[a-z][a-z0-9-]*panel$")

# The two panels that must fill their row of the two-column `.grid`.
FULL_WIDTH_PANELS = (".universe-panel", ".signals-panel")


def _rules() -> list[tuple[str, str]]:
    """Yield (selector, body) for every innermost rule, @media blocks included.

    The body pattern forbids braces, so a match can only be an innermost rule
    and a selector can never swallow an enclosing `@media(...)`.
    """

    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    # `@import ...;` ends in a semicolon, not a block, so leaving it in makes the
    # selector that follows it read as `@import url(...) :root`. Match the url by
    # its parentheses: the font query itself contains semicolons.
    css = re.sub(r"@import\s+url\([^)]*\)[^;]*;", "", css)
    return [
        (selector, match.group(2))
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)
        # An `@media(...)` prelude only matches here when its block is empty;
        # a real one wraps rules that the pattern reaches on their own.
        if not (selector := match.group(1).strip()).startswith("@")
    ]


def _declares(body: str, property_value: str) -> bool:
    return property_value in body.replace(" ", "")


def _bodies(selector: str) -> str:
    """Every body declared for `selector`, joined.

    A selector appears more than once -- a base rule plus a @media override --
    so keying a dict on it would silently keep only the last one.
    """

    return ";".join(
        body
        for rule_selector, body in _rules()
        if selector in [part.strip() for part in rule_selector.split(",")]
    )


def test_the_css_parses_into_the_rules_we_expect() -> None:
    """Guard the parser itself: a silent mismatch would make every test vacuous."""

    # A plain top-level rule.
    assert _declares(_bodies(".hero"), "display:grid")
    # ...which also carries a @media override, so both bodies must be collected.
    assert _declares(_bodies(".hero"), "padding:24px")
    # A rule nested inside @media, reached without dragging the @media along.
    assert _declares(_bodies(".settings-panel"), "grid-column:auto")
    # The rule right after the `@import` statement stays addressable.
    assert _declares(_bodies(":root"), "--lime:#65951a")


def test_no_panel_is_lifted_out_of_the_grid() -> None:
    """This exact bug shipped: a comma list meant for a button hit the panels.

    `.universe-panel,.signals-panel,.universe-panel button.compact{position:absolute}`
    pinned both panels to the top-right corner, where they covered the hero and
    its engine controls.
    """

    for selector, body in _rules():
        if not _declares(body, "position:absolute"):
            continue
        for part in selector.split(","):
            part = part.strip()
            assert not PANEL_SELECTOR.match(part), (
                f"{part!r} is a grid child, so position:absolute drops it out of "
                f"flow and it lands on top of the hero. Scope the rule to the "
                f"element inside the panel instead."
            )


@pytest.mark.parametrize("panel", FULL_WIDTH_PANELS)
def test_the_wide_panels_fill_their_row(panel: str) -> None:
    """A six-column table in one half of a two-column grid is unreadable."""

    assert _declares(_bodies(panel), "grid-column:span2"), (
        f"{panel} must declare grid-column:span 2"
    )
