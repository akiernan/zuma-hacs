"""Offline tests for the value codec and request building. No device needed."""

from __future__ import annotations

import sys

import pytest


def test_unwrap_tagged_scalars(zuma_api):
    """A tagged leaf yields the bare Python value."""
    assert zuma_api.unwrap({"value": {"i32_": 22, "type": "i32_"}}) == 22
    assert zuma_api.unwrap({"value": {"bool_": False, "type": "bool_"}}) is False
    assert zuma_api.unwrap({"value": {"string_": "Bathroom", "type": "string_"}}) == "Bathroom"


def test_unwrap_bool_strings(zuma_api):
    """The device may send a bool_ as "0"/"1"; "0" must not read as true."""
    assert zuma_api.unwrap({"value": {"bool_": "0", "type": "bool_"}}) is False
    assert zuma_api.unwrap({"value": {"bool_": "1", "type": "bool_"}}) is True
    assert zuma_api.unwrap_item({"bool_": "0", "type": "bool_"}) is False
    # Anything else is unknown rather than truthy.
    assert zuma_api.unwrap({"value": {"bool_": "yes", "type": "bool_"}}) is None
    assert zuma_api.unwrap({"value": {"bool_": 1, "type": "bool_"}}) is None
    # Strings under other tags are left alone.
    assert zuma_api.unwrap({"value": {"string_": "0", "type": "string_"}}) == "0"


def test_unwrap_picks_role(zuma_api):
    """A structure reply is keyed by role; unwrap reads the one asked for."""
    reply = {"title": "Volume", "type": "value", "value": {"type": "i32_", "i32_": 50}}
    assert zuma_api.unwrap(reply) == 50
    assert zuma_api.unwrap(reply, "title") == "Volume"


def test_unwrap_untagged_composite(zuma_api):
    """player:player/data comes back untagged and must survive intact."""
    data = {"state": "stopped", "keepActive": False, "error": ""}
    assert zuma_api.unwrap({"value": data}) == data


def test_unwrap_handles_empty_and_garbage(zuma_api):
    assert zuma_api.unwrap({}) is None
    assert zuma_api.unwrap({"value": None}) is None
    assert zuma_api.unwrap(None) is None
    assert zuma_api.unwrap([{"i32_": 1, "type": "i32_"}]) is None


def test_wrap_tags_bool_before_int(zuma_api):
    """bool subclasses int; tagging True as i32_ makes the device reject the write."""
    assert zuma_api.wrap(True) == {"bool_": True, "type": "bool_"}
    assert zuma_api.wrap(1) == {"i32_": 1, "type": "i32_"}
    assert zuma_api.wrap("x") == {"string_": "x", "type": "string_"}
    with pytest.raises(TypeError):
        zuma_api.wrap(1.5)


def test_roundtrip_wrap_unwrap(zuma_api):
    for value in (0, 22, 100, True, False, "Bathroom"):
        assert zuma_api.unwrap({"value": zuma_api.wrap(value)}) == value


async def test_set_volume_clamps_to_device_range(zuma_api, fake_session):
    """Out-of-range volumes are clamped, not sent through and rejected."""
    session = fake_session()
    api = zuma_api.ZumaApi("host.invalid", session)

    await api.set_volume(150)
    await api.set_volume(-10)
    await api.set_volume(35)

    sent = [body["value"]["i32_"] for _, body in session.calls]
    assert sent == [100, 0, 35]


async def test_get_volume_unwraps(zuma_api, fake_session):
    session = fake_session('{"value": {"i32_": 22, "type": "i32_"}}')
    api = zuma_api.ZumaApi("host.invalid", session)
    assert await api.get_volume() == 22
    url, body = session.calls[0]
    assert url.endswith("/api/getData")
    assert body == {"path": "player:volume", "roles": ["value"], "type": "structure"}


async def test_application_error_raises(zuma_api, fake_session):
    """A 500-with-JSON-body error must surface as ZumaError, not pass silently."""
    session = fake_session(
        '{"error": {"name": "x", "message": "Node does not exist"}}', status=500
    )
    api = zuma_api.ZumaApi("host.invalid", session)
    with pytest.raises(zuma_api.ZumaError, match="Node does not exist"):
        await api.get_volume()


async def test_non_json_error_raises_with_status(zuma_api, fake_session):
    """A bare-text failure (e.g. a stale queue id) is an error by status alone."""
    api = zuma_api.ZumaApi("host.invalid", fake_session("Unknown queue id!", status=400))
    with pytest.raises(zuma_api.ZumaError, match="HTTP 400.*Unknown queue id"):
        await api.poll_events("{stale}")


async def test_non_json_success_raises(zuma_api, fake_session):
    api = zuma_api.ZumaApi("host.invalid", fake_session("<html>", status=200))
    with pytest.raises(zuma_api.ZumaError, match="non-JSON"):
        await api.get_volume()


async def test_get_rssi_activates_live_reading(zuma_api, fake_session):
    """RSSI is sampled by activating network:wirelessRssi, not read from network:info."""
    session = fake_session('{"double_": -54, "type": "double_"}')
    api = zuma_api.ZumaApi("host.invalid", session)
    assert await api.get_rssi() == -54
    url, body = session.calls[0]
    assert url.endswith("/api/setData")
    assert body == {"path": "network:wirelessRssi", "role": "activate", "value": None}


async def test_get_rssi_failure_is_no_reading(zuma_api, fake_session):
    session = fake_session('{"error": {"message": "no wireless link"}}', status=500)
    api = zuma_api.ZumaApi("host.invalid", session)
    assert await api.get_rssi() is None


async def test_get_rows_requests_structure(zuma_api, fake_session):
    """getRows asks for structure rows and returns them keyed by role."""
    session = fake_session(
        '{"rowsVersion": 0, "rowsCount": 2, "rows": ['
        '{"type": "value", "path": "settings:/zuma/bezelAttached"},'
        '{"type": "container", "path": "settings:/zuma/avs"}]}'
    )
    api = zuma_api.ZumaApi("host.invalid", session)
    rows = await api.get_rows("settings:/zuma", start=0, end=45)
    assert rows == [
        {"type": "value", "path": "settings:/zuma/bezelAttached"},
        {"type": "container", "path": "settings:/zuma/avs"},
    ]
    url, body = session.calls[0]
    assert url.endswith("/api/getRows")
    assert body == {
        "path": "settings:/zuma",
        "roles": ["path", "type"],
        "from": 0,
        "to": 45,
        "type": "structure",
    }


async def test_control_sends_expected_payload(zuma_api, fake_session):
    """Transport verbs go out as {"control": verb} on the activate role."""
    session = fake_session("null")
    api = zuma_api.ZumaApi("host.invalid", session)
    await api.control("pause")
    url, body = session.calls[0]
    assert url.endswith("/api/setData")
    assert body == {
        "path": "player:player/control",
        "role": "activate",
        "value": {"control": "pause"},
    }


async def test_control_rejects_unknown_verb(zuma_api, fake_session):
    """play needs the item's roles (play_roles); a bare verb fails loudly."""
    api = zuma_api.ZumaApi("host.invalid", fake_session("null"))
    with pytest.raises(ValueError, match="unknown control verb"):
        await api.control("play")


STATION_ROW = (
    '{"type": "audio", "title": "Radio X UK",'
    ' "id": "airable://airable/radio/6495847017504275",'
    ' "path": "airable:https://8779202999.airable.io/id/airable/radio/6495847017504275",'
    ' "mediaData": {"metaData": {"serviceID": "airableRadios"}}}'
)


async def test_play_roles_sends_play_with_media_roles(zuma_api, fake_session):
    """play goes to the control node with the item's roles and a play mode."""
    session = fake_session("null")
    api = zuma_api.ZumaApi("host.invalid", session)
    roles = {"type": "audio", "path": "airable:x"}
    await api.play_roles(roles)
    url, body = session.calls[0]
    assert url.endswith("/api/setData")
    assert body == {
        "path": "player:player/control",
        "role": "activate",
        "value": {"control": "play", "playMode": "normal", "mediaRoles": roles},
    }


async def test_airable_station_roles_looks_up_under_device_root(zuma_api, fake_session):
    """The station is found under the root the device reports, never a fixed host."""
    session = fake_session([
        '{"path": "airable:https://8779202999.airable.io/"}',
        '{"rowsCount": 4, "rows": [' + STATION_ROW + "]}",
    ])
    api = zuma_api.ZumaApi("host.invalid", session)
    roles = await api.airable_station_roles("airable://airable/radio/6495847017504275")
    assert roles["title"] == "Radio X UK"

    (_, root_req), (url, rows_req) = session.calls
    assert root_req == {"path": "airable:", "roles": ["path"], "type": "structure"}
    assert url.endswith("/api/getRows")
    assert rows_req == {
        "path": "airable:https://8779202999.airable.io/id/airable/radio/6495847017504275",
        "roles": ["@all"],
        "from": 0,
        "to": 1,
        "type": "structure",
    }


async def test_airable_root_is_cached(zuma_api, fake_session):
    session = fake_session([
        '{"path": "airable:https://h.airable.io/"}',
        '{"rows": [' + STATION_ROW + "]}",
        '{"rows": [' + STATION_ROW + "]}",
    ])
    api = zuma_api.ZumaApi("host.invalid", session)
    await api.airable_station_roles("1")
    await api.airable_station_roles("2")
    assert [body["path"] for _, body in session.calls] == [
        "airable:",
        "airable:https://h.airable.io/id/airable/radio/1",
        "airable:https://h.airable.io/id/airable/radio/2",
    ]


async def test_airable_playable_roles_rejects_containers(zuma_api, fake_session):
    """A path whose first row isn't an audio item has nothing to play."""
    reply = '{"rows": [{"type": "container", "title": "Favorites", "path": "airable:f"}]}'
    api = zuma_api.ZumaApi("host.invalid", fake_session(reply))
    with pytest.raises(zuma_api.ZumaError, match="nothing playable"):
        await api.airable_playable_roles("airable:f")


def test_thermal_modes_cover_device_enum(zuma_api):
    """Every NsdkZumaTemperatureMode value maps to a lowercase HA state."""
    import re

    modes = sys.modules["zuma_under_test.const"].THERMAL_MODES
    assert set(modes) == {"normal", "ledLimited", "ledAmpLimited", "ledAmpShutdown"}
    assert all(re.fullmatch(r"[a-z0-9_]+", state) for state in modes.values())


def test_player_fields_keeps_media_roles_for_resume(zuma_api):
    playing = zuma_api.player_fields(
        {"state": "playing", "mediaRoles": {"path": "airable:x"}, "trackRoles": {"title": "T"}}
    )
    assert playing["media_roles"] == {"path": "airable:x"}
    assert zuma_api.player_fields({"state": "stopped"})["media_roles"] is None


async def test_set_light_wraps_composite_value(zuma_api, fake_session):
    """Light state is a composite tagged value, not a scalar."""
    session = fake_session("null")
    api = zuma_api.ZumaApi("host.invalid", session)
    await api.set_light(
        {"power": True, "brightness": 40, "temperature": 3000,
         "lastTransitionPeriod": "ms500"}
    )
    url, body = session.calls[0]
    assert url.endswith("/api/setData")
    assert body["path"] == "zuma:lightState"
    assert body["value"]["type"] == "zumaLightState"
    assert body["value"]["zumaLightState"]["brightness"] == 40


async def test_get_light_unwraps_state(zuma_api, fake_session):
    reply = '{"value":{"type":"zumaLightState","zumaLightState":{"power":true,"brightness":17,"temperature":3869}}}'
    api = zuma_api.ZumaApi("host.invalid", fake_session(reply))
    light = await api.get_light()
    assert light == {"power": True, "brightness": 17, "temperature": 3869}


async def test_create_event_queue_subscribes_with_value(zuma_api, fake_session):
    """Leaf nodes subscribe as itemWithValue so events carry the new value."""
    session = fake_session('"{q-123}"')
    api = zuma_api.ZumaApi("host.invalid", session)
    qid = await api.create_event_queue(["player:volume", "zuma:lightState"])
    assert qid == "{q-123}"
    url, body = session.calls[0]
    assert url.endswith("/api/event/modifyQueue")
    assert "queueId" not in body
    assert body["subscribe"] == [
        {"path": "player:volume", "type": "itemWithValue"},
        {"path": "zuma:lightState", "type": "itemWithValue"},
    ]


async def test_poll_events_timeout_is_seconds(zuma_api, fake_session):
    """The device reads pollQueue's timeout as seconds, not milliseconds."""
    session = fake_session("[]")
    api = zuma_api.ZumaApi("host.invalid", session)
    await api.poll_events("{q-123}")
    url, body = session.calls[0]
    assert url.endswith("/api/event/pollQueue")
    assert body == {"queueId": "{q-123}", "timeout": zuma_api.PUSH_POLL_TIMEOUT_SECONDS}
    assert zuma_api.PUSH_POLL_TIMEOUT_SECONDS < 120


async def test_poll_events_returns_unwrapped_values(zuma_api, fake_session):
    """Each event yields (path, unwrapped itemValue), in order."""
    reply = (
        '[{"itemType": "update", "rowsEvents": [], "path": "player:volume",'
        ' "itemValue": {"type": "i32_", "i32_": 31}},'
        ' {"itemType": "update", "rowsEvents": [], "path": "zuma:lightState",'
        ' "itemValue": {"type": "zumaLightState", "zumaLightState":'
        ' {"power": true, "brightness": 86, "temperature": 4350}}},'
        ' {"itemType": "remove", "rowsEvents": [], "path": "player:player/data"}]'
    )
    api = zuma_api.ZumaApi("host.invalid", fake_session(reply))
    assert await api.poll_events("{q-123}") == [
        ("player:volume", 31),
        ("zuma:lightState", {"power": True, "brightness": 86, "temperature": 4350}),
        ("player:player/data", None),
    ]


async def test_poll_events_tolerates_empty(zuma_api, fake_session):
    api = zuma_api.ZumaApi("host.invalid", fake_session("[]"))
    assert await api.poll_events("{q-123}") == []


def test_push_updates_last_value_wins(zuma_api):
    """A burst (a slider drag) collapses to its final value per key."""
    updates, refresh = zuma_api.push_updates([
        ("zuma:lightState", {"power": True, "brightness": 86}),
        ("player:volume", 20),
        ("zuma:lightState", {"power": True, "brightness": 32}),
        ("settings:/mediaPlayer/mute", False),
    ])
    assert updates == {
        "light": {"power": True, "brightness": 32},
        "volume": 20,
        "mute": False,
    }
    assert refresh is False


def test_push_updates_maps_player_data(zuma_api):
    """player:player/data events fill the same keys a full poll would."""
    updates, refresh = zuma_api.push_updates([
        ("player:player/data", {"state": "playing", "trackRoles": {"title": "Radio X"}}),
    ])
    assert updates["state"] == "playing"
    assert updates["title"] == "Radio X"
    assert updates["controls"] == {}
    assert refresh is False


def test_push_updates_falls_back_to_refresh(zuma_api):
    """A valueless event or an unknown path can only be resolved by reading."""
    assert zuma_api.push_updates([("player:volume", None)]) == ({}, True)
    assert zuma_api.push_updates([("other:node", 1)]) == ({}, True)
    assert zuma_api.push_updates([("zuma:lightState", "garbage")]) == ({}, True)


def test_push_paths_cover_entity_state(zuma_api):
    """Every subscribed path is one the coordinator knows how to apply."""
    assert set(zuma_api.PUSH_UPDATERS) == {
        "player:volume",
        "settings:/mediaPlayer/mute",
        "player:player/data",
        "zuma:lightState",
        "settings:/zuma/circadianLighting",
        "settings:/zuma/ledCurfewEnabled",
    }


def test_play_action_resumes_paused_and_replays_stopped(zuma_api):
    """Paused resumes in place (pause toggles); stopped replays the remembered item."""
    roles = {"path": "airable:x", "type": "audio"}
    assert zuma_api.play_action("paused", roles) == "resume"
    assert zuma_api.play_action("paused", None) == "resume"
    assert zuma_api.play_action("stopped", roles) == "replay"
    assert zuma_api.play_action(None, roles) == "replay"
    assert zuma_api.play_action("stopped", None) is None
    assert zuma_api.play_action("playing", roles) is None
