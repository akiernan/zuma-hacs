# Zuma for Home Assistant

Local control of [Zuma](https://zuma.ai) ceiling speaker-lights (Lumisonic / Zuma SL)
over the local network — no cloud, no account, no API key.

> **Disclaimer — not affiliated with Zuma.** This is an independent,
> community-built integration. It is **not** produced, endorsed, sponsored, or
> supported by Zuma Array Limited or any of its brands (Zuma, Lumisonic). "Zuma"
> and "Lumisonic" are trademarks of their respective owner and are used here only
> to describe the hardware this project interoperates with (nominative fair use).
> The integration talks to an undocumented local interface found by inspecting a
> device the author owns; it may break at any firmware update and comes with no
> warranty. Use at your own risk.

## What it does

| Entity | Backing node | Notes |
|---|---|---|
| `light` | `zuma:lightState` | on/off, brightness, colour temperature (2200–6500 K) |
| `media_player` volume / mute | `player:volume`, `settings:/mediaPlayer/mute` | volume 0–100 ↔ HA 0.0–1.0 |
| `media_player` transport | `player:player/control` | stop; pause and next/previous only when the stream reports them (live radio can't pause, podcasts can); play resumes a pause in place, or restarts the last airable item once stopped |
| `media_player` play airable | `airable:` → `player:player/control` | `media_player.play_media` with an airable station id — native internet radio |
| `media_player` play URL | DLNA `AVTransport` | `media_player.play_media` — start any other stream URL |
| `media_player` now playing | `player:player/data` | state, title, artwork, `zuma_service` attribute |
| `switch` circadian lighting | `settings:/zuma/circadianLighting` | mode toggle |
| `switch` status LED curfew | `settings:/zuma/ledCurfewEnabled` | quiets the indicator LED overnight (config) |
| `sensor` WiFi signal / IP / firmware / thermal mode | `network:wirelessRssi` (activated for a live reading), `network:info`, device identity, `settings:/zuma/volatile/temperatureMode` | read-only diagnostics |
| `binary_sensor` smart bezel / area master | `settings:/zuma/bezelAttached`, `settings:/system/zuma/zumaMaster` | read-only diagnostics |

Units are discovered automatically over mDNS (`_sues800device._tcp`); the TXT record's
serial becomes the unique ID, so discovered and manually-added entries resolve to one
device. Manual setup by IP also works.

State changes are **pushed**: the integration long-polls the device's event queue
(`/api/event/*`, subscribing to leaf nodes as type `itemWithValue`). Each event carries
the node's new value, which is applied directly, so a change made from the app or the
unit itself shows up within about a second -- app-driven light changes included. A
10 s poll runs as a safety net and refreshes the diagnostics push doesn't cover.

## Install

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=luismalves&repository=zuma-hacs&category=integration)

Click the button above (requires [HACS](https://hacs.xyz)), or add it manually:

HACS → three-dot menu → Custom repositories → this repo, category *Integration*.
Then **Settings → Devices & Services → Add Integration → Zuma**.

Or copy `custom_components/zuma/` into your HA `config/custom_components/` and restart.

## Playing internet radio (airable)

The unit's built-in internet radio is airable. Play a station by the device's own id
for it with the standard `media_player.play_media` action:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.zuma_bathroom
data:
  media_content_id: airable://airable/radio/6495847017504275
  media_content_type: music
```

The id is the number at the end of a station's browse path, e.g.
`airable:https://…airable.io/id/airable/radio/6495847017504275`. Browse for them with
`scripts/live_check.py <host> --ls airable:` and descend into Radio → Favorites, History
and so on. An `airable:` browse path works as `media_content_id` too, when the first
row it lists is playable: a station's path, or a podcast's episodes list (which plays
its newest episode). A single episode's own `…/id/airable/feed.episode/<id>` path
can't be listed, so an episode can't be addressed directly.

This is the same dance the Zuma apps do: read the device's airable root (its `path`
role names an account-specific host such as `https://8779202999.airable.io/` — never
hardcode it), fetch the station's row under `<root>/id/airable/radio/<id>` with every
role (`@all`), and hand that row back unchanged as `mediaRoles` in a
`{"control": "play", "playMode": "normal", "mediaRoles": …}` activate on
`player:player/control`. The device resolves the stream itself. A bare
`{"control": "play"}` without roles is what makes the device try to play the current
directory and fail with *"Directory is empty"*.

**Play** resumes. When paused it sends `pause` again: the verb is a toggle, and resumes
from the same position (re-sending the item restarts it from the top, and a bare
`{"control": "play"}` stops playback). Once stopped the device forgets what was
playing and its position, so the integration keeps the last airable item's roles and
play re-sends them -- live radio picks up live, a podcast starts over.

## Playing a stream URL

For anything that isn't on airable, use `play_media` with a URL:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.zuma_bathroom
data:
  media_content_id: https://playerservices.streamtheworld.com/api/livestream-redirect/RADIO_RENASCENCA.mp3
  media_content_type: music
```

Under the hood the nsdk API can't play an arbitrary URL, so this bridges
to the unit's own Rygel DLNA renderer: a unicast SSDP M-SEARCH finds it each call (its
port is ephemeral and moves across reboots), then `SetAVTransportURI` + `Play`. Volume,
mute, pause and stop still go through the nsdk API. HA media-source items (TTS, the
media browser) work too, not just raw URLs.

**Format limits** (the renderer probes the URL and enforces its sink list): MP3
(`audio/mpeg`) and clean AAC/MP4 play. **HLS (`.m3u8`) and ICY `audio/aacp` do not** —
notably streamtheworld's `.aac` mounts serve `audio/aacp` and are refused, so use the
station's `.mp3` mount.

## How light control works

The lamp is a composite settings value with power, brightness (0–100) and colour
temperature (Kelvin):

```
zuma:lightState = {"type": "zumaLightState", "zumaLightState": {
  "power": true, "brightness": 25, "temperature": 3869,
  "lastTransitionPeriod": "ms1000"}}
```

The canonical node `settings:/zuma/lightState` is flagged `"internal": true` in the
firmware, so enumeration never lists it and `getData` returns *"Node is internal"*. But
the device mirrors the lamp into the **`zuma:` volatile namespace**, and `zuma:lightState`
is served over the LAN for both read and write with no authentication — no DTLS, no
CoAP, no per-device key. Notes that shaped the entity:

- **Colour temperature**: the firmware tolerates 1000–8000 K but the entity clamps to
  2200–6500 K, the range a fixture actually renders.
- **Power is its own field, but not fully independent** — `brightness: 0` leaves
  `power: true` (on but dark), so turn-off switches power and keeps the brightness,
  restoring the level on turn-on. The other way round doesn't hold: setting a non-zero
  brightness switches the lamp on.
- **Changes are patches.** Activating `zuma:lightState` (rather than writing its
  `value`) with a partial `zumaLightState` changes only the fields given, e.g.
  `{"type": "zumaLightState", "zumaLightState": {"power": false}}`. So the entity
  sends just power plus whatever was asked for, and never writes back stale fields
  over a change made from the app. Without a `lastTransitionPeriod` the device uses
  its default.
- **Transition** enum: `instant, ms25, ms50, ms125, ms250, ms500, ms1000, ms2000, ms4000`; HA's
  transition seconds snap to the nearest bucket.

## What isn't possible locally

- **Starting AirPlay, Spotify Connect or TIDAL Connect.** These are driven from the
  sending app; start them there and this integration then controls them.
- **Numeric device temperature.** The unit measures SoC / MCU / LED / amp temperatures
  (`zuma-metric-gatherer` reading `/sys/class/thermal/...`) but **publishes them only as
  MQTT telemetry to Zuma's cloud** — they are never written to a locally-readable node.
  The app's temperature figure comes from the cloud. The only local thermal signal is
  the `thermal_mode` enum (`normal` → `ledLimited` → `ledAmpLimited` → `ledAmpShutdown`),
  exposed as a sensor.

## The device API, for reference

Port 80, plaintext, unauthenticated. It is the StreamUnlimited StreamSDK web API — the
hardware pairs a StreamUnlimited S800 audio module (which owns this API) with Zuma's own
light/MCU board, which is why audio is richly exposed and the lamp hides in the `zuma:`
mirror.

Every call is a JSON POST, matching the official nSDK client bindings:

```
POST /api/getData            {"path":..,"roles":[..],"type":"structure"}
POST /api/getRows            {"path":..,"roles":[..],"from":<i>,"to":<i>,"type":"structure"}
POST /api/setData            {"path":..,"role":..,"value":..}
POST /api/event/modifyQueue  {"queueId":..,"subscribe":[..],"unsubscribe":[..]}
POST /api/event/pollQueue    {"queueId":..,"timeout":<seconds>}
```

`"type": "structure"` makes getData answer with an object keyed by role name
(`{"value": ..., "title": ...}`) and getRows with one such object per row. Without it
both answer with arrays in the order the roles were requested. The device also accepts
the same calls as GETs with query parameters.

Two things will trip you up:

1. **Values are tagged unions.** A read returns `{"value": {"i32_": 22, "type": "i32_"}}`, and a
   write must re-tag with the matching type name. Booleans tag as `bool_`, not `i32_`,
   even though Python's `bool` is an `int`.
2. **Application errors come back as HTTP 500 with a JSON body**, not a transport error:
   `{"error": {"name": "CMAbstractWorker::invalidPath", "message": "..."}}`. Some
   failures are bare text instead, e.g. a stale event queue gets `400 Unknown queue id!`.

Useful roles: `value`, `title`, `type`, `path`. Enumerate a container with
getRows and the roles `["path", "type"]`.

Open ports on the unit: **80** StreamSDK API, **2019** TIDAL Connect, **7000** AirPlay,
**8080 / 8085** unidentified (bare 404s), **41347** (ephemeral) the Rygel DLNA
MediaRenderer, **5683/udp** plain CoAP (answers `4.04` unauthenticated), **5684/udp** the
Zuma CoAP/DTLS channel (per-device X.509 cert in `settings:/zuma/factoryCert` /
`factoryKey`). Spotify Connect rides on port 80 at `/api/stream/spotify:zeroconf`.

## Tests

Offline tests (value codec, request shapes, DLNA helpers) need no device and no Home
Assistant:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest tests/test_api.py tests/test_dlna.py -q
```

Live tests talk to a real unit and print its responses:

```bash
ZUMA_HOST=192.168.20.158 .venv/bin/python -m pytest tests/test_live.py -v -s
```

Reads are safe; write round-trips (volume, light) move real hardware and restore it, so
they are gated behind an explicit opt-in:

```bash
ZUMA_HOST=… ZUMA_ALLOW_WRITE=1 .venv/bin/python -m pytest tests/test_live.py -v -s
```

A standalone explorer needs neither pytest nor Home Assistant:

```bash
python3 scripts/live_check.py <ip>                       # full report
python3 scripts/live_check.py <ip> --ls settings:/       # list a container
python3 scripts/live_check.py <ip> --walk settings:/zuma # dump a subtree
python3 scripts/live_check.py <ip> --get player:volume
python3 scripts/live_check.py <ip> --set player:volume 25
```

## Compatibility

Built against a Zuma SL on firmware `22.11.108952`, StreamSDK `21.03-Phosphorus`. Node
paths are undocumented and may move in any firmware update. Nothing here is official or
supported by Zuma.

## License

[MIT](LICENSE) © Luís Alves and contributors. The MIT grant covers this project's own
source only; it confers no rights in any Zuma trademark or firmware. "Zuma" and
"Lumisonic" are trademarks of Zuma Array Limited (see the disclaimer at the top).
