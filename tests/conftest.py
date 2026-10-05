"""Test fixtures.

The api module only needs aiohttp, but importing it the normal way would execute
custom_components/zuma/__init__.py and drag in all of Home Assistant. So load
const and api directly into a synthetic package instead -- that keeps the test
suite runnable with just `pytest` + `aiohttp`, which is the whole point of being
able to poke a real device from a laptop.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

import pytest

PKG = "zuma_under_test"
COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "zuma"


def _load_api():
    if f"{PKG}.api" in sys.modules:
        return sys.modules[f"{PKG}.api"]

    package = types.ModuleType(PKG)
    package.__path__ = [str(COMPONENT)]
    sys.modules[PKG] = package

    # const first: api's `from .const import ...` resolves through sys.modules.
    for name in ("const", "api"):
        spec = importlib.util.spec_from_file_location(
            f"{PKG}.{name}", COMPONENT / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{PKG}.{name}"] = module
        spec.loader.exec_module(module)

    return sys.modules[f"{PKG}.api"]


@pytest.fixture(scope="session")
def zuma_api():
    """The api module, importable without Home Assistant installed."""
    return _load_api()


class FakeResponse:
    """Minimal stand-in for an aiohttp response context manager."""

    def __init__(self, text: str, status: int = 200) -> None:
        self._text = text
        self.status = status

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeSession:
    """Records POSTed bodies and replays one canned reply."""

    def __init__(self, reply: str = "true", status: int = 200) -> None:
        self.reply = reply
        self.status = status
        self.calls: list[tuple[str, dict | None]] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return FakeResponse(self.reply, self.status)


@pytest.fixture
def fake_session():
    """Factory for a request-recording fake session."""
    return FakeSession
