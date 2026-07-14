import sys

import pytest

from candlepilot.cli import _parser, main


def test_cli_parser_requires_known_command() -> None:
    assert _parser().parse_args(["doctor"]).command == "doctor"
    with pytest.raises(SystemExit):
        _parser().parse_args(["unknown"])


def test_serve_refuses_non_local_bind(monkeypatch) -> None:
    monkeypatch.setenv("CANDLEPILOT_HOST", "0.0.0.0")
    monkeypatch.setattr(sys, "argv", ["candlepilot", "serve"])
    with pytest.raises(SystemExit, match="only permits a localhost"):
        main()
