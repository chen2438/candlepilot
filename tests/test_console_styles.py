"""The frontend's CSS fails silently: a wrong selector still builds and ships.

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


def test_the_hero_uses_two_columns_and_keeps_six_actions_on_one_row() -> None:
    """The control column must leave room for six distinct live actions."""

    hero = _bodies(".hero")
    assert _declares(hero, "grid-template-columns:minmax(250px,.7fr)minmax(520px,1.7fr)")
    assert _declares(_bodies(".controls"), "display:grid")
    assert _declares(_bodies(".controls"), "grid-template-columns:repeat(6,1fr)")
    assert _declares(_bodies(".controls>.cadence-select"), "grid-column:1/-1")


def test_the_overview_stacks_before_provider_cards_can_overlap() -> None:
    """The zoomed two-column overview is too narrow for provider controls at 1200px."""

    css = (FRONTEND / "styles.css").read_text(encoding="utf-8").replace(" ", "")
    assert (
        "@media(max-width:1200px){.overview-grid{grid-template-columns:1fr}"
        ".overview-grid>.signals-panel{grid-column:auto}}"
    ) in css


@pytest.mark.parametrize("panel", FULL_WIDTH_PANELS)
def test_the_wide_panels_fill_their_row(panel: str) -> None:
    """A six-column table in one half of a two-column grid is unreadable."""

    assert _declares(_bodies(panel), "grid-column:span2"), (
        f"{panel} must declare grid-column:span 2"
    )


def test_the_backtest_form_stacks_its_labels_like_every_other_form() -> None:
    """An inline label sits beside its input and ragged-aligns the row."""

    # The house convention, set by the settings endpoint grid.
    assert _declares(_bodies(".endpoint-grid label"), "display:grid")
    assert _declares(_bodies(".backtest-form label"), "display:grid")
    assert _declares(_bodies(".backtest-form label"), "min-width:0")


def test_the_tooltip_layer_never_widens_the_page() -> None:
    """`visibility:hidden` hides a box; it does not take it out of layout.

    The tooltip was `position:absolute;left:0;width:max-content;max-width:320px`
    against a `position:relative` anchor. Hidden or not, it still contributed to
    the document's scrollable overflow, so the anchors in the right-most column
    of `.run-usage-metrics`, `.risk-grid` and the market table pushed their
    tooltips past the viewport and the overview tab scrolled 200px sideways with
    nothing hovered. Hovering those anchors showed a tooltip clipped off-screen.

    A fixed-position box is excluded from its ancestors' scrollable overflow, so
    `position:fixed` is what makes the phantom scrollbar structurally impossible
    -- `absolute` would bring it straight back. Anchor positioning is what keeps
    a fixed box pinned to its anchor, and the fallbacks flip it inline-ward when
    the default placement would cross the viewport edge.
    """

    for selector, body in _rules():
        if not selector.endswith("[data-tooltip]::after"):
            continue
        assert not _declares(body, "position:absolute"), (
            f"{selector!r} must not be position:absolute: a hidden absolute "
            f"tooltip still adds its width to the document's scrollable "
            f"overflow and scrolls the page sideways."
        )

    tooltip = _bodies("[data-tooltip]::after")
    assert _declares(tooltip, "position:fixed")
    # A fixed box does not follow its anchor on its own; position-area is what
    # replaces the `left`/`bottom` offsets the absolute version relied on.
    assert _declares(tooltip, "position-area:block-startspan-inline-end")
    # The anchor is named rather than left implicit because Chrome resolves no
    # implicit anchor for a pseudo-element's position-area; `anchor-scope` is
    # what stops every anchor's `--tip` from colliding into the last one.
    assert _declares(_bodies("[data-tooltip]"), "anchor-name:--tip")
    assert _declares(_bodies("[data-tooltip]"), "anchor-scope:--tip")
    assert _declares(tooltip, "position-anchor:--tip")
    # Without a fallback the fixed box keeps its placement and runs off the right
    # edge for the anchors in the last column of their container. The combined
    # flip is not redundant: Chrome (148) refuses a bare `flip-inline` for the
    # table-cell anchors placed block-end by `th[data-tooltip]::after`, so
    # dropping it silently puts the right-most header's tooltip off-screen again.
    assert _declares(tooltip, "position-try-fallbacks:flip-inline,flip-blockflip-inline"), (
        "[data-tooltip]::after must offer both an inline flip and a combined "
        "block+inline flip, or right-edge anchors render off-screen."
    )


def test_the_backtest_inputs_use_the_shared_field_style() -> None:
    """Unstyled inputs render as raw native widgets next to styled ones."""

    styled = [
        selector
        for selector, body in _rules()
        if _declares(body, "border-radius:5px") and _declares(body, "height:28px")
    ]
    parts = {part.strip() for selector in styled for part in selector.split(",")}
    assert ".backtest-form input" in parts
    assert ".probe-timeout input" in parts


def test_backtest_ui_has_no_manual_order_book_collector() -> None:
    source = (FRONTEND / "App.tsx").read_text(encoding="utf-8")

    assert "/api/collector" not in source
    assert "<CollectorPanel" not in source
    assert "use_recorded_book: useRecordedBook" not in source
    assert "正式运行回放" in source
