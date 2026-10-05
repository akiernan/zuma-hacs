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
| `media_player` transport | `player:player/control` | pause, stop; next/previous only when the stream reports them |
| `media_player` play URL | DLNA `AVTransport` | `media_player.play_media` — start a stream URL |
| `media_player` now playing | `player:player/data` | state, title, artwork, `zuma_service` attribute |
| `switch` circadian lighting | `settings:/zuma/circadianLighting` | mode toggle |
| `switch` status LED curfew | `settings:/zuma/ledCurfewEnabled` | quiets the indicator LED overnight (config) |
| `sensor` WiFi signal / IP / firmware / thermal mode | `network:info`, device identity, `zuma:volatile/temperatureMode` | read-only diagnostics |
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

## Playing a stream URL (internet radio, etc.)

Use the standard `media_player.play_media` action — no custom service:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.zuma_bathroom
data:
  media_content_id: https://playerservices.streamtheworld.com/api/livestream-redirect/RADIO_RENASCENCA.mp3
  media_content_type: music
```

Under the hood the nsdk API can't *start* a URL (only pause/stop/skip), so this bridges
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
- **Brightness and power are independent** — `brightness: 0` leaves `power: true` (on
  but dark), so turn-off writes `power: false` and keeps the brightness, restoring the
  level on turn-on.
- **Transition** enum: `instant, ms125, ms250, ms500, ms1000, ms2000, ms4000`; HA's
  transition seconds snap to the nearest bucket.

## What isn't possible locally

- **Starting native (airable) internet radio.** Browsing the airable directory works,
  but nothing in the HTTP API *starts* a station: `player:player/control` accepts only
  `pause`/`stop`/`next`/`previous` (24 other verb spellings were tried), and activating
  a station only navigates. Start playback from the Zuma app / AirPlay / Spotify /
  TIDAL and this integration then controls it — or push any stream URL yourself with
  `play_media` (above), which is the practical substitute.
- **Numeric device temperature.** The unit measures SoC / MCU / LED / amp temperatures
  (`zuma-metric-gatherer` reading `/sys/class/thermal/...`) but **publishes them only as
  MQTT telemetry to Zuma's cloud** — they are never written to a locally-readable node.
  The app's temperature figure comes from the cloud. The only local thermal signal is
  the `thermal_mode` enum (`normal` → `limited` → `shutdown`), exposed as a sensor.

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
