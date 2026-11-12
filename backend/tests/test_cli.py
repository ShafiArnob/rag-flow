"""The flowbuilder command's arguments and its failures before it reaches the database. The rotation
itself is run against the database in tests/crypto/test_rotation.py."""

from importlib.metadata import entry_points

import pytest

from app.cli import build_parser, main
from support.master_keys import (
    ALL_ZERO_PLACEHOLDER,
    encoded,
    in_typographic_quotes,
    new_master_key,
)

ARGPARSE_USAGE_ERROR = 2


def test_the_parser_accepts_keys_rotate() -> None:
    arguments = build_parser().parse_args(["keys", "rotate"])

    assert (arguments.command, arguments.keys_command) == ("keys", "rotate")


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="no-command"),
        pytest.param(["keys"], id="no-keys-command"),
        pytest.param(["keys", "rotate", "--now"], id="unknown-option"),
        pytest.param(["secrets", "rotate"], id="unknown-command"),
    ],
)
def test_bad_arguments_exit_with_a_usage_error(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(argv)

    assert raised.value.code == ARGPARSE_USAGE_ERROR


def test_the_flowbuilder_console_script_runs_main() -> None:
    [script] = entry_points(group="console_scripts", name="flowbuilder")

    assert script.value == "app.cli:main"
    assert script.load() is main


@pytest.mark.parametrize(
    "keyring",
    [
        pytest.param(ALL_ZERO_PLACEHOLDER, id="all-zero-placeholder"),
        pytest.param(f"{encoded(new_master_key())} {encoded(new_master_key())[:20]}", id="short"),
        pytest.param(in_typographic_quotes(encoded(new_master_key())), id="smart-quotes"),
    ],
)
def test_keys_rotate_exits_1_on_a_weak_keyring_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], keyring: str
) -> None:
    monkeypatch.setenv("MASTER_KEYS", keyring)
    monkeypatch.delenv("MASTER_KEYS_FILE", raising=False)

    exit_code = main(["keys", "rotate"])

    output = capsys.readouterr()
    assert exit_code == 1
    assert output.out == ""
    assert output.err.startswith("error: MASTER_KEYS: key ")
    assert "Traceback" not in output.err
    for key in keyring.split():
        assert key not in output.err
