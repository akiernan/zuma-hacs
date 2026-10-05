#!/usr/bin/env python3
"""Poke a real Zuma unit and print what it says. No Home Assistant required.

    python3 scripts/live_check.py 192.168.20.158
    python3 scripts/live_check.py 192.168.20.158 --walk settings:/zuma
    python3 scripts/live_check.py 192.168.20.158 --ls settings:/
    python3 scripts/live_check.py 192.168.20.158 --get player:volume
    python3 scripts/live_check.py 192.168.20.158 --set player:volume 25

Useful for exploring nodes the integration does not model yet -- internet radio
lives under airable: and ui:/presets, for instance.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
from pathlib import Path
import sys
import types

import aiohttp

# ponytail: same 10-line loader as tests/conftest.py, duplicated on purpose --
# importing the package properly would pull in all of Home Assistant, and a
# shared helper would need sys.path juggling to reach from two directories.
PKG = "zuma_live"
COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "zuma"


def load_api():
    """Load api.py without executing the integration's __init__.py."""
    package = types.ModuleType(PKG)
    package.__path__ = [str(COMPONENT)]
    sys.modules[PKG] = package
    for name in ("const", "api"):
        spec = importlib.util.spec_from_file_location(
            f"{PKG}.{name}", COMPONENT / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{PKG}.{name}"] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{PKG}.api"]


async def report(api) -> None:
    """Print everything the integration's entities are built on."""
    identity = await api.get_identity()
    print("device")
    for key, value in identity.items():
        print(f"  {key:14} {value}")

    state = await api.get_state()
    print("\nstate")
    for key, value in state.items():
        print(f"  {key:14} {value}")

    volume = state["volume"]
    print(f"\nmedia_player  volume_level={volume / 100 if volume is not None else None}"
          f"  muted={state['mute']}  state={state['state']}")
    print(f"switches      circadian_lighting={state['circadian']}"
          f"  led_curfew={state['led_curfew']}")


async def walk(api, path: str, depth: int = 0, max_depth: int = 6) -> None:
    """Depth-first dump of a subtree."""
    rows = await api.get_rows(path) if depth < max_depth else []
    children = [(r.get("path"), r.get("type")) for r in rows if r.get("path") not in (None, path)]
    if not children:
        print(f"{'  ' * depth}{path} = {await api.get_value(path)!r}")
        return
    print(f"{'  ' * depth}{path}/")
    for child, kind in children:
        if kind == "container":
            await walk(api, child, depth + 1, max_depth)
        else:
            print(f"{'  ' * (depth + 1)}{child} = {await api.get_value(child)!r}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("--walk", metavar="PATH", help="recursively dump a subtree")
    parser.add_argument("--ls", metavar="PATH", help="list a container's children")
    parser.add_argument("--get", metavar="PATH", help="read one node")
    parser.add_argument("--set", nargs=2, metavar=("PATH", "VALUE"), help="write one node")
    args = parser.parse_args()

    api_mod = load_api()
    async with aiohttp.ClientSession() as session:
        api = api_mod.ZumaApi(args.host, session)
        try:
            if args.walk:
                await walk(api, args.walk)
            elif args.ls:
                for row in await api.get_rows(args.ls):
                    print(f"  {row.get('type') or '?':10} {row.get('path')}")
            elif args.get:
                print(await api.get_value(args.get))
            elif args.set:
                path, raw = args.set
                value: bool | int | str
                if raw in ("true", "false"):
                    value = raw == "true"
                elif raw.lstrip("-").isdigit():
                    value = int(raw)
                else:
                    value = raw
                print(await api.set_value(path, value))
            else:
                await report(api)
        except api_mod.ZumaError as err:
            sys.exit(f"device error: {err}")


if __name__ == "__main__":
    asyncio.run(main())
