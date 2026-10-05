"""Constants for the Zuma integration."""

DOMAIN = "zuma"

# mDNS type the unit advertises; its TXT record carries name, serial, uuid and ip.
ZEROCONF_TYPE = "_sues800device._tcp.local."

# StreamSDK data-model paths, confirmed against a Zuma SL on firmware 22.11.
PATH_VOLUME = "player:volume"
PATH_MUTE = "settings:/mediaPlayer/mute"
PATH_PLAYER_DATA = "player:player/data"
PATH_CONTROL = "player:player/control"
PATH_DEVICE_NAME = "settings:/deviceName"
PATH_VERSION = "settings:/version"
PATH_SERIAL = "settings:/system/serialNumber"
PATH_MODEL = "settings:/system/modelName"
PATH_MANUFACTURER = "settings:/system/manufacturer"
PATH_NETWORK_INFO = "network:info"
PATH_TEMP_MODE = "settings:/zuma/volatile/temperatureMode"
PATH_BEZEL = "settings:/zuma/bezelAttached"
PATH_MASTER = "settings:/system/zuma/zumaMaster"
PATH_CIRCADIAN = "settings:/zuma/circadianLighting"
PATH_LED_CURFEW = "settings:/zuma/ledCurfewEnabled"
PATH_VOLUME_MAP = "settings:/mediaPlayer/volumeMap"

# The lamp. settings:/zuma/lightState is internal (loopback-only); the zuma:
# volatile namespace mirrors it and IS served over the LAN HTTP API, read+write.
# Value is a composite: {"type":"zumaLightState","zumaLightState":{power,
# brightness 0-100, temperature Kelvin, lastTransitionPeriod}}.
PATH_LIGHT = "zuma:lightState"

# Firmware tolerates 1000-8000 K but that is outside useful tunable white; clamp to
# the range a real fixture renders. Brightness and power are independent on the
# device (brightness 0 keeps power true), so turn-off toggles power, not brightness.
LIGHT_MIN_KELVIN = 2200
LIGHT_MAX_KELVIN = 6500

# lastTransitionPeriod enum from the firmware, in milliseconds.
LIGHT_TRANSITIONS = {0: "instant", 125: "ms125", 250: "ms250", 500: "ms500",
                     1000: "ms1000", 2000: "ms2000", 4000: "ms4000"}

# The device's volumeMap has 101 entries (-120 dB .. 0 dB), so volume is 0-100.
VOLUME_MAX = 100

# Poll cadence. Push (the event queue) delivers changes within about a second,
# including app-driven light changes on zuma:lightState, so this poll is only a
# safety net and the source of the rarely-changing diagnostics push doesn't cover.
SCAN_INTERVAL_SECONDS = 10

# The full transport vocabulary accepted by player:player/control. Swept exhaustively:
# there is no play/resume verb at any spelling, so playback cannot be started here.
CONTROL_VERBS = ("pause", "stop", "next", "previous")


# pollQueue's timeout is in seconds: the device answers with an empty list once it
# passes with nothing queued. The client timeout sits a little above it.
PUSH_POLL_TIMEOUT_SECONDS = 30
