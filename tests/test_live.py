"""Live tests against a real Zuma unit.

    ZUMA_HOST=192.168.20.158 pytest tests/test_live.py -v -s

Reads are always safe. The volume round-trip actually moves the volume on a real
speaker, so it stays behind an explicit opt-in:

    ZUMA_HOST=192.168.20.158 ZUMA_ALLOW_WRITE=1 pytest tests/test_live.py -v -s
"""

from __future__ import annotations

import json
import os

import aiohttp
import pytest

HOST = os.environ.get("ZUMA_HOST")
ALLOW_WRITE = os.environ.get("ZUMA_ALLOW_WRITE") == "1"

pytestmark = pytest.mark.skipif(not HOST, reason="set ZUMA_HOST to run live tests")


@pytest.fixture
async def api(zuma_api):
    """A client bound to the real device."""
    async with aiohttp.ClientSession() as session:
        yield zuma_api.ZumaApi(HOST, session)


async def test_identity(api):
    """The unit reports a serial, name and firmware version."""
    identity = await api.get_identity()
    print("\nidentity:", json.dumps(identity, indent=2))
    assert identity["serial"], "no serialNumber -- is this really a Zuma?"
    assert identity["name"]
    assert identity["manufacturer"]


async def test_volume_is_in_device_range(api):
    """Volume reads back as an int in 0..100."""
    volume = await api.get_volume()
    print(f"\nvolume: {volume}")
    assert isinstance(volume, int)
    assert 0 <= volume <= 100


async def test_mute_is_boolean(api):
    mute = await api.get_mute()
    print(f"\nmute: {mute}")
    assert isinstance(mute, bool)


async def test_player_state_is_known(api):
    """Transport state is one of the strings the media player maps."""
    state = await api.get_player_state()
    print(f"\nplayer state: {state}")
    assert state in {"stopped", "playing", "paused", "transitioning"}, state


async def test_full_poll_matches_entity_expectations(api):
    """One coordinator poll returns every key the entities read."""
    state = await api.get_state()
    print("\npoll:", json.dumps(state, indent=2))
    assert {
        "volume", "mute", "state", "title", "image", "source", "controls",
        "circadian", "led_curfew", "light", "ip", "ssid", "bssid", "rssi",
        "frequency", "thermal", "bezel", "master",
    } <= set(state)
    assert isinstance(state["controls"], dict)
    assert isinstance(state["circadian"], bool)
    assert isinstance(state["led_curfew"], bool)


async def test_bad_path_raises(api, zuma_api):
    """The device's 500-with-JSON error convention is handled."""
    with pytest.raises(zuma_api.ZumaError):
        await api.get_value("settings:/definitely/not/a/node")


@pytest.mark.skipif(not ALLOW_WRITE, reason="set ZUMA_ALLOW_WRITE=1 to test writes")
async def test_volume_roundtrip_and_restore(api):
    """Write volume, confirm it took, put it back where it was."""
    original = await api.get_volume()
    target = 30 if original != 30 else 25
    try:
        await api.set_volume(target)
        assert await api.get_volume() == target
        print(f"\nvolume write confirmed: {original} -> {target}")
    finally:
        await api.set_volume(original)
        assert await api.get_volume() == original
        print(f"restored to {original}")


async def test_light_reads(api):
    """The lamp state is readable over the LAN HTTP API."""
    light = await api.get_light()
    print("\nlight:", json.dumps(light))
    assert light is not None
    assert 0 <= light["brightness"] <= 100
    assert isinstance(light["power"], bool)
    assert light["temperature"] > 0


@pytest.mark.skipif(not ALLOW_WRITE, reason="set ZUMA_ALLOW_WRITE=1 to test writes")
async def test_light_brightness_roundtrip_and_restore(api):
    """Nudge brightness, confirm, restore -- this visibly dims a real lamp."""
    orig = await api.get_light()
    target = dict(orig)
    target["brightness"] = 30 if orig["brightness"] != 30 else 20
    target["lastTransitionPeriod"] = "ms500"
    try:
        await api.set_light(target)
        assert (await api.get_light())["brightness"] == target["brightness"]
        print(f"\nlight write confirmed: {orig['brightness']} -> {target['brightness']}")
    finally:
        await api.set_light({**orig, "lastTransitionPeriod": "ms500"})
        assert (await api.get_light())["brightness"] == orig["brightness"]


async def test_diagnostics_present(api):
    """Diagnostics poll returns connectivity + thermal + role fields."""
    st = await api.get_state()
    print("\ndiagnostics:", json.dumps({k: st.get(k) for k in
        ("ip","ssid","rssi","thermal","bezel","master")}, indent=2))
    assert st["rssi"] is None or isinstance(st["rssi"], int | float)
    assert st["thermal"] in (None, "normal", "ledLimited", "ledAmpLimited", "ledAmpShutdown")
    assert isinstance(st["master"], bool)
