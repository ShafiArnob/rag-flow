"""The ``flowbuilder`` maintenance command.

``flowbuilder keys rotate`` re-wraps every secret's data key under the first key of the keyring.
It connects as the schema owner, from MIGRATION_DATABASE_URL, and reads the keyring from
MASTER_KEYS or MASTER_KEYS_FILE. It exits 1 while any secret is left under another key, so a
runbook can repeat it until it succeeds.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from app.config import KeyringSettings, MigrationSettings
from app.crypto.key_provider import KeyringError, load_local_key_provider
from app.crypto.rotation import DataKeyRewrapError, rewrap_data_keys
from app.database.engine import create_database_engine

EXIT_SUCCESS = 0
EXIT_FAILURE = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flowbuilder", description="FlowBuilder maintenance")
    commands = parser.add_subparsers(dest="command", required=True)
    keys = commands.add_parser("keys", help="manage the master keys that wrap secrets' data keys")
    key_commands = keys.add_subparsers(dest="keys_command", required=True)
    key_commands.add_parser(
        "rotate", help="re-wrap every data key under the first key of the keyring"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Exits 2 on unknown arguments, as argparse does."""
    # keys rotate is the only command, so parsing the arguments is all the dispatch there is.
    build_parser().parse_args(argv)
    return asyncio.run(rotate_keys())


async def rotate_keys() -> int:
    try:
        key_provider = load_local_key_provider(KeyringSettings())
    except KeyringError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_FAILURE
    settings = MigrationSettings()  # type: ignore[call-arg]
    # Row security stays on: the owner reaches every workspace's secrets only through the key
    # rotation policies, and with it off FORCE ROW LEVEL SECURITY would reject every statement.
    engine = create_database_engine(settings.migration_database_url)
    try:
        report = await rewrap_data_keys(engine, key_provider)
    except DataKeyRewrapError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        await engine.dispose()
    print(
        f"rewrapped {report.rewrapped} data keys to {report.active_key_id};"
        f" {report.remaining} remain under other keys"
    )
    return EXIT_SUCCESS if report.remaining == 0 else EXIT_FAILURE
