"""The lint rule that keeps outbound HTTP inside the egress client."""

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
BANNED_IMPORTS = [
    "import httpx",
    "from httpx import AsyncClient",
    "import httpcore",
    "import requests",
    "from requests import Session",
    "import urllib.request",
    "from urllib import request",
    "from urllib.request import urlopen",
    "import http.client",
    "from http import client",
    "import aiohttp",
    "import urllib3",
]


def lint(source: str, path: str) -> subprocess.CompletedProcess[str]:
    """Runs the project's ruff configuration on ``source`` as if it were the file at ``path``."""
    return subprocess.run(  # noqa: S603 - a fixed command; the input is test source
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--select",
            "TID251",
            "--stdin-filename",
            path,
            "-",
        ],
        input=f"{source}\n",
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        check=False,
    )


@pytest.mark.parametrize("source", BANNED_IMPORTS)
@pytest.mark.parametrize("path", ["src/app/nodes/probe.py", "src/app/connectors/probe.py"])
def test_a_raw_http_import_outside_the_egress_package_fails_lint(source: str, path: str) -> None:
    result = lint(source, path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "TID251" in result.stdout
    assert "EgressClient" in result.stdout


@pytest.mark.parametrize("source", BANNED_IMPORTS)
def test_the_egress_package_may_import_http_libraries(source: str) -> None:
    result = lint(source, "src/app/egress/probe.py")

    assert result.returncode == 0, result.stdout + result.stderr
