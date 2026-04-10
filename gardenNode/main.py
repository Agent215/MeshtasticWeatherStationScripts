import network
import socket
import time
import json
import machine
from machine import Pin, UART

# ============================================================
# Pico W Tempest UDP -> UART bridge
#
# Supports:
#   - obs_st
#   - rapid_wind (used to backfill wind data when obs_st omits it)
#   - evt_precip
#   - evt_strike
#   - device_status
#   - hub_status
#
# Behavior:
#   - Per-event-type forwarding throttle
#   - Small outbound queue so messages are staggered
#   - Replace-latest behavior for status/weather snapshots
# ============================================================

# ----------------------------
# User settings
# ----------------------------
WIFI_SSID = "YOUR_WIFI_HERE"
WIFI_PASSWORD = "YOUR_PASSWORD_HERE"

UDP_PORT = 50222

# UART to RAK / Meshtastic
UART_BAUD = 115200
UART_TX_PIN = 0   # GP0
UART_RX_PIN = 1   # GP1

# Timing / resilience
SOCKET_TIMEOUT_SEC = 1.0
WIFI_CONNECT_TIMEOUT_SEC = 20
WIFI_RETRY_DELAY_SEC = 3
MAX_CONSECUTIVE_WIFI_FAILURES = 10
MAX_CONSECUTIVE_MAIN_LOOP_FAILURES = 10

# Per-type forward throttles (configurable)
FORWARD_INTERVAL_OBS_ST_SEC = 60
FORWARD_INTERVAL_EVT_PRECIP_SEC = 60
FORWARD_INTERVAL_EVT_STRIKE_SEC = 30
FORWARD_INTERVAL_DEVICE_STATUS_SEC = 60
FORWARD_INTERVAL_HUB_STATUS_SEC = 60

# Global pacing between ANY two UART sends to reduce mesh congestion
MIN_UART_SEND_GAP_SEC = 5

# Outbound queue behavior
MAX_OUTBOUND_QUEUE = 12

# Startup / heartbeat
STARTUP_DELAY_SEC = 3
HEARTBEAT_INTERVAL_SEC = 15 * 60   # every 15 minutes

# Optional small pause after socket recreation / reconnect
POST_RECOVERY_DELAY_MS = 250

# No-UDP staged recovery thresholds
NO_UDP_SOCKET_RECOVERY_THRESHOLD = 120
NO_UDP_WIFI_RECOVERY_THRESHOLD = 300
NO_UDP_REBOOT_THRESHOLD = 999999   # effectively disabled for now

# ----------------------------
# Hardware init
# ----------------------------
led = Pin("LED", Pin.OUT)
uart = UART(0, baudrate=UART_BAUD, tx=Pin(UART_TX_PIN), rx=Pin(UART_RX_PIN))

# ----------------------------
# Global runtime state
# ----------------------------
msg_id = 1

last_uart_send_tick_ms = None
last_heartbeat_tick_ms = None
boot_tick_ms = time.ticks_ms()

consecutive_wifi_failures = 0
consecutive_main_loop_failures = 0
consecutive_no_udp_cycles = 0

socket_recovery_triggered = False
wifi_recovery_triggered = False

# ----------------------------
# Debug / instrumentation state
# ----------------------------
udp_packet_count = 0
json_error_count = 0
unsupported_type_count = 0
sanity_reject_count = 0
rate_limit_skip_count = 0
queue_replace_count = 0
queue_drop_count = 0
forwarded_count = 0

socket_recreate_count = 0
wifi_reconnect_count = 0
socket_error_count = 0

last_any_udp_tick_ms = None
last_obs_st_tick_ms = None
last_valid_weather_tick_ms = None
last_rapid_wind = None

# Track last successful FORWARD per supported kind
last_forward_by_kind_ms = {}

# Simple outbound queue
# each item = {"kind": "...", "data": ..., "priority": int, "queued_ms": int}
outbound_queue = []

SUPPORTED_KINDS = (
    "obs_st",
    "evt_precip",
    "evt_strike",
    "device_status",
    "hub_status",
)

REPLACE_LATEST_KINDS = (
    "obs_st",
    "device_status",
    "hub_status",
)

KIND_PRIORITY = {
    "evt_strike": 0,
    "evt_precip": 1,
    "obs_st": 2,
    "device_status": 3,
    "hub_status": 4,
}


# ----------------------------
# Utility helpers
# ----------------------------
def blink(times=1, on_time=0.12, off_time=0.12):
    for _ in range(times):
        led.on()
        time.sleep(on_time)
        led.off()
        time.sleep(off_time)


def ticks_since_ms(start_tick):
    return time.ticks_diff(time.ticks_ms(), start_tick)


def seconds_to_ms(seconds):
    return int(seconds * 1000)


def elapsed_seconds_or_minus_one(last_tick_ms):
    if last_tick_ms is None:
        return -1
    return ticks_since_ms(last_tick_ms) // 1000


def reset_cause_name():
    cause = machine.reset_cause()
    for name in ("PWRON_RESET", "HARD_RESET", "WDT_RESET", "DEEPSLEEP_RESET", "SOFT_RESET"):
        if hasattr(machine, name) and cause == getattr(machine, name):
            return cause, name
    return cause, "UNKNOWN"


def register_healthy_no_udp_progress():
    global consecutive_no_udp_cycles
    consecutive_no_udp_cycles += 1


def reset_no_udp_counter():
    global consecutive_no_udp_cycles
    global socket_recovery_triggered, wifi_recovery_triggered

    consecutive_no_udp_cycles = 0
    socket_recovery_triggered = False
    wifi_recovery_triggered = False


def wifi_is_connected(wlan):
    try:
        return wlan is not None and wlan.active() and wlan.isconnected()
    except Exception:
        return False


def get_wifi_pm_value(wlan):
    try:
        return wlan.config("pm")
    except Exception:
        return -1


def disable_wifi_power_save(wlan):
    try:
        if hasattr(network.WLAN, "PM_NONE"):
            wlan.config(pm=network.WLAN.PM_NONE)
            print("Wi-Fi power save disabled using PM_NONE")
        else:
            wlan.config(pm=0)
            print("Wi-Fi power save disabled using pm=0 fallback")
    except Exception as e:
        print("Failed to disable Wi-Fi power save:", e)

    try:
        print("Wi-Fi PM value:", wlan.config("pm"))
    except Exception as e:
        print("Could not read Wi-Fi PM value:", e)


def reset_wlan():
    wlan = network.WLAN(network.STA_IF)

    try:
        wlan.disconnect()
    except Exception:
        pass

    try:
        wlan.active(False)
    except Exception:
        pass

    time.sleep(1)

    try:
        wlan.active(True)
    except Exception:
        pass

    time.sleep(1)
    return wlan


def connect_wifi(ssid, password, timeout_sec=WIFI_CONNECT_TIMEOUT_SEC):
    wlan = reset_wlan()

    print("Connecting to Wi-Fi:", ssid)
    wlan.connect(ssid, password)

    start_tick = time.ticks_ms()
    timeout_ms = seconds_to_ms(timeout_sec)

    while not wlan.isconnected():
        if ticks_since_ms(start_tick) > timeout_ms:
            raise RuntimeError("Wi-Fi connection timeout")

        blink(1, 0.05, 0.20)
        time.sleep(1)

    disable_wifi_power_save(wlan)

    print("Wi-Fi connected")
    print("IP config:", wlan.ifconfig())
    blink(3, 0.08, 0.08)
    return wlan


def ensure_wifi_connected(wlan, ssid, password):
    """
    Returns (wlan, reconnected)
    reconnected=True means caller should recreate UDP socket.
    """
    global consecutive_wifi_failures, wifi_reconnect_count

    if wifi_is_connected(wlan):
        consecutive_wifi_failures = 0
        return wlan, False

    print("Wi-Fi disconnected. Reconnecting...")

    while True:
        try:
            wlan = connect_wifi(ssid, password)
            print("Wi-Fi reconnected. IP:", wlan.ifconfig()[0])
            consecutive_wifi_failures = 0
            wifi_reconnect_count += 1
            return wlan, True

        except Exception as e:
            consecutive_wifi_failures += 1
            print("Wi-Fi reconnect failed:", e)
            blink(2, 0.05, 0.10)

            if consecutive_wifi_failures >= MAX_CONSECUTIVE_WIFI_FAILURES:
                print("Too many Wi-Fi failures. Rebooting Pico...")
                time.sleep(2)
                machine.reset()

            time.sleep(WIFI_RETRY_DELAY_SEC)


def make_udp_listener(port):
    addr = socket.getaddrinfo("0.0.0.0", port)[0][-1]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(addr)
    sock.settimeout(SOCKET_TIMEOUT_SEC)
    print("Listening for UDP on port", port)
    return sock


def recreate_udp_listener(old_sock, port):
    global socket_recreate_count

    try:
        if old_sock is not None:
            old_sock.close()
    except Exception:
        pass

    sock = make_udp_listener(port)
    socket_recreate_count += 1
    time.sleep_ms(POST_RECOVERY_DELAY_MS)
    return sock


def is_socket_timeout_error(exc):
    try:
        args = getattr(exc, "args", ())
        if args:
            first = args[0]

            if isinstance(first, int) and first in (110,):
                return True

            if isinstance(first, str):
                s = first.lower()
                if "timed out" in s or "timeout" in s:
                    return True

        s = str(exc).lower()
        if "timed out" in s or "timeout" in s:
            return True

    except Exception:
        pass

    return False


def maybe_trigger_no_udp_recovery(wlan, sock):
    global socket_recovery_triggered, wifi_recovery_triggered, consecutive_no_udp_cycles

    if consecutive_no_udp_cycles >= NO_UDP_REBOOT_THRESHOLD:
        print("No UDP for", consecutive_no_udp_cycles, "healthy cycles. Rebooting Pico...")
        time.sleep(2)
        machine.reset()

    if (consecutive_no_udp_cycles >= NO_UDP_WIFI_RECOVERY_THRESHOLD
            and not wifi_recovery_triggered):
        print("No UDP for", consecutive_no_udp_cycles,
              "healthy cycles. Forcing Wi-Fi reconnect...")
        wifi_recovery_triggered = True

        try:
            wlan = reset_wlan()
            wlan, reconnected = ensure_wifi_connected(wlan, WIFI_SSID, WIFI_PASSWORD)
            if reconnected:
                sock = recreate_udp_listener(sock, UDP_PORT)
        except Exception as e:
            print("Wi-Fi staged recovery failed:", e)

        return wlan, sock

    if (consecutive_no_udp_cycles >= NO_UDP_SOCKET_RECOVERY_THRESHOLD
            and not socket_recovery_triggered):
        print("No UDP for", consecutive_no_udp_cycles,
              "healthy cycles. Recreating UDP socket...")
        socket_recovery_triggered = True

        try:
            sock = recreate_udp_listener(sock, UDP_PORT)
        except Exception as e:
            print("Socket staged recovery failed:", e)

        return wlan, sock

    return wlan, sock


# ----------------------------
# Throttle / queue helpers
# ----------------------------
def min_forward_interval_ms_for_kind(kind):
    if kind == "obs_st":
        return seconds_to_ms(FORWARD_INTERVAL_OBS_ST_SEC)
    if kind == "evt_precip":
        return seconds_to_ms(FORWARD_INTERVAL_EVT_PRECIP_SEC)
    if kind == "evt_strike":
        return seconds_to_ms(FORWARD_INTERVAL_EVT_STRIKE_SEC)
    if kind == "device_status":
        return seconds_to_ms(FORWARD_INTERVAL_DEVICE_STATUS_SEC)
    if kind == "hub_status":
        return seconds_to_ms(FORWARD_INTERVAL_HUB_STATUS_SEC)
    return seconds_to_ms(60)


def may_forward_kind_now(kind):
    last_tick = last_forward_by_kind_ms.get(kind)
    if last_tick is None:
        return True

    interval_ms = min_forward_interval_ms_for_kind(kind)
    return time.ticks_diff(time.ticks_ms(), last_tick) >= interval_ms


def may_uart_send_now():
    global last_uart_send_tick_ms

    if last_uart_send_tick_ms is None:
        return True

    min_gap_ms = seconds_to_ms(MIN_UART_SEND_GAP_SEC)
    return time.ticks_diff(time.ticks_ms(), last_uart_send_tick_ms) >= min_gap_ms


def commit_uart_send(kind=None):
    global last_uart_send_tick_ms, last_valid_weather_tick_ms, forwarded_count

    now_tick = time.ticks_ms()
    last_uart_send_tick_ms = now_tick

    if kind is not None:
        last_forward_by_kind_ms[kind] = now_tick
        forwarded_count += 1

        if kind == "obs_st":
            last_valid_weather_tick_ms = now_tick


def enqueue_forward_item(kind, data):
    global rate_limit_skip_count, queue_replace_count, queue_drop_count

    if kind not in SUPPORTED_KINDS:
        return False

    if not may_forward_kind_now(kind):
        rate_limit_skip_count += 1
        return False

    queued_ms = time.ticks_ms()
    priority = KIND_PRIORITY.get(kind, 99)

    if kind in REPLACE_LATEST_KINDS:
        for item in outbound_queue:
            if item["kind"] == kind:
                item["data"] = data
                item["queued_ms"] = queued_ms
                queue_replace_count += 1
                return True

    if len(outbound_queue) >= MAX_OUTBOUND_QUEUE:
        print("Outbound queue full. Dropping", kind)
        queue_drop_count += 1
        return False

    new_item = {
        "kind": kind,
        "data": data,
        "priority": priority,
        "queued_ms": queued_ms,
    }

    insert_at = len(outbound_queue)
    for idx in range(len(outbound_queue)):
        if priority < outbound_queue[idx]["priority"]:
            insert_at = idx
            break

    outbound_queue.insert(insert_at, new_item)
    return True


def maybe_send_next_queued():
    if not outbound_queue:
        return False

    if not may_uart_send_now():
        return False

    item = outbound_queue.pop(0)

    try:
        send_forward_item(item["kind"], item["data"])
    except Exception:
        # Put it back and re-raise so caller recovery logic can run
        outbound_queue.insert(0, item)
        raise

    commit_uart_send(item["kind"])
    return True


# ----------------------------
# Weather / event parsing
# ----------------------------
def round_weather_fields(obs):
    global last_rapid_wind

    def obs_value(index, default=None):
        if index >= len(obs):
            return default
        value = obs[index]
        if value is None:
            return default
        return value

    def round_two(value):
        return round(float(value), 2)

    def optional_float(index):
        value = obs_value(index)
        if value is None:
            return None
        return round_two(value)

    def optional_int(index):
        value = obs_value(index)
        if value is None:
            return None
        return int(round(float(value)))

    wind_speed = obs_value(2)
    wind_direction = obs_value(4)

    if last_rapid_wind is not None:
        if wind_speed is None:
            wind_speed = last_rapid_wind["w"]
        if wind_direction is None:
            wind_direction = last_rapid_wind["d"]

    if wind_speed is None:
        wind_speed = 0
    if wind_direction is None:
        wind_direction = 0

    return {
        "ts": int(obs[0]),
        "t": round_two(obs_value(7, 0)),
        "h": round_two(obs_value(8, 0)),
        "p": round_two(obs_value(6, 0)),
        "w": round_two(wind_speed),
        "g": round_two(obs_value(3, wind_speed)),
        "l": round_two(obs_value(1, wind_speed)),
        "d": int(round(float(wind_direction))),
        "ws": optional_int(5),
        "r": round_two(obs_value(12, 0)),
        "uv": round_two(obs_value(10, 0)),
        "sr": round_two(obs_value(11, 0)),
        "lux": round_two(obs_value(9, 0)),
        "bat": round_two(obs_value(16, 0)),
        "ld": round_two(obs_value(14, 0)),
        "lc": int(round(float(obs_value(15, 0)))),
        "pt": int(round(float(obs_value(13, 0)))),
        "ri": int(round(float(obs_value(17, 0)))),
        "rd": optional_float(18),
        "nr": optional_float(19),
        "nrd": optional_float(20),
        "pa": optional_int(21),
    }


def weather_values_are_sane(w):
    if not (-50 <= w["t"] <= 60):
        return False
    if not (0 <= w["h"] <= 100):
        return False
    if not (850 <= w["p"] <= 1100):
        return False
    if not (0 <= w["w"] <= 100):
        return False
    if not (0 <= w["g"] <= 120):
        return False
    if not (0 <= w["l"] <= 100):
        return False
    if not (0 <= w["d"] <= 360):
        return False
    if w["ws"] is not None and not (0 <= w["ws"] <= 120):
        return False
    if not (0 <= w["r"] <= 1000):
        return False
    if not (0 <= w["uv"] <= 30):
        return False
    if not (0 <= w["sr"] <= 2000):
        return False
    if not (0 <= w["lux"] <= 200000):
        return False
    if not (0 <= w["bat"] <= 5):
        return False
    if not (0 <= w["ld"] <= 100):
        return False
    if not (0 <= w["lc"] <= 10000):
        return False
    if not (0 <= w["pt"] <= 4):
        return False
    if not (0 <= w["ri"] <= 60):
        return False
    if w["rd"] is not None and not (0 <= w["rd"] <= 2000):
        return False
    if w["nr"] is not None and not (0 <= w["nr"] <= 2000):
        return False
    if w["nrd"] is not None and not (0 <= w["nrd"] <= 2000):
        return False
    if w["pa"] is not None and not (0 <= w["pa"] <= 10):
        return False
    return True


def device_status_values_are_sane(d):
    if d["ts"] < 0:
        return False
    if d["up"] < 0:
        return False
    if not (0 <= d["v"] <= 5):
        return False
    if not (-150 <= d["r"] <= 20):
        return False
    if not (-150 <= d["hr"] <= 20):
        return False
    if not (0 <= d["dbg"] <= 1):
        return False
    if d["ss"] < 0:
        return False
    return True


def hub_status_values_are_sane(h):
    if h["ts"] < 0:
        return False
    if h["up"] < 0:
        return False
    if not (-150 <= h["r"] <= 20):
        return False
    if h["seq"] < 0:
        return False
    if not isinstance(h["fs"], list):
        return False
    if not isinstance(h["rs"], list):
        return False
    if not isinstance(h["ms"], list):
        return False
    return True


def strike_values_are_sane(s):
    if s["ts"] < 0:
        return False
    if not (0 <= s["ld"] <= 500):
        return False
    if not (0 <= s["se"] <= 1000000000):
        return False
    return True


def precip_values_are_sane(p):
    return p["ts"] >= 0


def parse_obs_st(packet_obj):
    global sanity_reject_count, last_obs_st_tick_ms

    last_obs_st_tick_ms = time.ticks_ms()

    obs_list = packet_obj.get("obs")
    if not obs_list or not isinstance(obs_list, list):
        sanity_reject_count += 1
        return None

    obs = obs_list[0]
    if not isinstance(obs, list) or len(obs) < 18:
        sanity_reject_count += 1
        return None

    try:
        w = round_weather_fields(obs)
        if not weather_values_are_sane(w):
            print("Rejected out-of-range obs_st payload:", w)
            sanity_reject_count += 1
            return None
        return w
    except Exception as e:
        print("obs_st field parse error:", e)
        sanity_reject_count += 1
        return None


def parse_evt_precip(packet_obj):
    global sanity_reject_count

    evt = packet_obj.get("evt")
    if not isinstance(evt, list) or len(evt) < 1:
        sanity_reject_count += 1
        return None

    try:
        p = {
            "ts": int(evt[0]),
        }
        if not precip_values_are_sane(p):
            print("Rejected out-of-range evt_precip payload:", p)
            sanity_reject_count += 1
            return None
        return p
    except Exception as e:
        print("evt_precip field parse error:", e)
        sanity_reject_count += 1
        return None


def parse_rapid_wind(packet_obj):
    global sanity_reject_count, last_rapid_wind

    ob = packet_obj.get("ob")
    if not isinstance(ob, list) or len(ob) < 3:
        sanity_reject_count += 1
        return None

    try:
        last_rapid_wind = {
            "ts": int(ob[0]),
            "w": round(float(ob[1]), 2),
            "d": int(round(float(ob[2]))),
        }
        return last_rapid_wind
    except Exception as e:
        print("rapid_wind field parse error:", e)
        sanity_reject_count += 1
        return None


def parse_evt_strike(packet_obj):
    global sanity_reject_count

    evt = packet_obj.get("evt")
    if not isinstance(evt, list) or len(evt) < 3:
        sanity_reject_count += 1
        return None

    try:
        s = {
            "ts": int(evt[0]),
            "ld": int(round(float(evt[1]))),
            "se": int(round(float(evt[2]))),
        }
        if not strike_values_are_sane(s):
            print("Rejected out-of-range evt_strike payload:", s)
            sanity_reject_count += 1
            return None
        return s
    except Exception as e:
        print("evt_strike field parse error:", e)
        sanity_reject_count += 1
        return None


def parse_device_status(packet_obj):
    global sanity_reject_count

    try:
        d = {
            "ts": int(packet_obj.get("timestamp")),
            "up": int(packet_obj.get("uptime")),
            "v": round(float(packet_obj.get("voltage")), 2),
            "fw": packet_obj.get("firmware_revision"),
            "r": int(packet_obj.get("rssi")),
            "hr": int(packet_obj.get("hub_rssi")),
            "ss": int(packet_obj.get("sensor_status")),
            "dbg": int(packet_obj.get("debug")),
        }
        if not device_status_values_are_sane(d):
            print("Rejected out-of-range device_status payload:", d)
            sanity_reject_count += 1
            return None
        return d
    except Exception as e:
        print("device_status field parse error:", e)
        sanity_reject_count += 1
        return None


def parse_hub_status(packet_obj):
    global sanity_reject_count

    try:
        h = {
            "ts": int(packet_obj.get("timestamp")),
            "up": int(packet_obj.get("uptime")),
            "fw": packet_obj.get("firmware_revision"),
            "r": int(packet_obj.get("rssi")),
            "rf": packet_obj.get("reset_flags", ""),
            "seq": int(packet_obj.get("seq")),
            "fs": packet_obj.get("fs", []),
            "rs": packet_obj.get("radio_stats", []),
            "ms": packet_obj.get("mqtt_stats", []),
        }
        if not hub_status_values_are_sane(h):
            print("Rejected out-of-range hub_status payload:", h)
            sanity_reject_count += 1
            return None
        return h
    except Exception as e:
        print("hub_status field parse error:", e)
        sanity_reject_count += 1
        return None


def parse_supported_packet(packet_obj):
    global unsupported_type_count

    packet_type = packet_obj.get("type")

    if packet_type == "obs_st":
        return "obs_st", parse_obs_st(packet_obj)

    if packet_type == "rapid_wind":
        parse_rapid_wind(packet_obj)
        return None, None

    if packet_type == "evt_precip":
        return "evt_precip", parse_evt_precip(packet_obj)

    if packet_type == "evt_strike":
        return "evt_strike", parse_evt_strike(packet_obj)

    if packet_type == "device_status":
        return "device_status", parse_device_status(packet_obj)

    if packet_type == "hub_status":
        return "hub_status", parse_hub_status(packet_obj)

    unsupported_type_count += 1
    return None, None


# ----------------------------
# UART send helpers
# ----------------------------
def uart_send_json(payload):
    msg = json.dumps(payload, separators=(",", ":")) + "\n"

    written = uart.write(msg)
    if written is None:
        written = len(msg)

    if written != len(msg):
        raise RuntimeError("Partial UART write: wrote {} of {} bytes".format(written, len(msg)))

    print("UART bytes:", len(msg))
    print("UART sent:", msg.strip())
    blink(1, 0.15, 0.05)


def send_obs_st_to_rak(weather):
    global msg_id

    payload = {
        "et": "obs_st",
        "i": msg_id,
        "ts": weather["ts"],
        "t": weather["t"],
        "h": weather["h"],
        "p": weather["p"],
        "w": weather["w"],
        "g": weather["g"],
        "l": weather["l"],
        "d": weather["d"],
        "ws": weather["ws"],
        "r": weather["r"],
        "uv": weather["uv"],
        "sr": weather["sr"],
        "lux": weather["lux"],
        "bat": weather["bat"],
        "ld": weather["ld"],
        "lc": weather["lc"],
        "pt": weather["pt"],
        "ri": weather["ri"],
    }

    if weather["rd"] is not None:
        payload["rd"] = weather["rd"]
    if weather["nr"] is not None:
        payload["nr"] = weather["nr"]
    if weather["nrd"] is not None:
        payload["nrd"] = weather["nrd"]
    if weather["pa"] is not None:
        payload["pa"] = weather["pa"]

    uart_send_json(payload)
    msg_id += 1


def send_evt_precip_to_rak(evt):
    global msg_id

    payload = {
        "et": "evt_precip",
        "i": msg_id,
        "ts": evt["ts"],
    }

    uart_send_json(payload)
    msg_id += 1


def send_evt_strike_to_rak(evt):
    global msg_id

    payload = {
        "et": "evt_strike",
        "i": msg_id,
        "ts": evt["ts"],
        "ld": evt["ld"],
        "se": evt["se"],
    }

    uart_send_json(payload)
    msg_id += 1


def send_device_status_to_rak(dev):
    global msg_id

    payload = {
        "et": "device_status",
        "i": msg_id,
        "ts": dev["ts"],
        "up": dev["up"],
        "v": dev["v"],
        "fw": dev["fw"],
        "r": dev["r"],
        "hr": dev["hr"],
        "ss": dev["ss"],
        "dbg": dev["dbg"],
    }

    uart_send_json(payload)
    msg_id += 1


def send_hub_status_to_rak(hub):
    global msg_id

    payload = {
        "et": "hub_status",
        "i": msg_id,
        "ts": hub["ts"],
        "up": hub["up"],
        "fw": hub["fw"],
        "r": hub["r"],
        "rf": hub["rf"],
        "seq": hub["seq"],
        "fs": hub["fs"],
        "rs": hub["rs"],
        "ms": hub["ms"],
    }

    uart_send_json(payload)
    msg_id += 1


def send_forward_item(kind, data):
    if kind == "obs_st":
        send_obs_st_to_rak(data)
        return

    if kind == "evt_precip":
        send_evt_precip_to_rak(data)
        return

    if kind == "evt_strike":
        send_evt_strike_to_rak(data)
        return

    if kind == "device_status":
        send_device_status_to_rak(data)
        return

    if kind == "hub_status":
        send_hub_status_to_rak(data)
        return

    raise RuntimeError("Unsupported forward kind: {}".format(kind))


def maybe_send_heartbeat(wlan):
    global last_heartbeat_tick_ms, msg_id

    now_tick = time.ticks_ms()
    interval_ms = seconds_to_ms(HEARTBEAT_INTERVAL_SEC)

    if last_heartbeat_tick_ms is not None:
        if time.ticks_diff(now_tick, last_heartbeat_tick_ms) < interval_ms:
            return False

    if not may_uart_send_now():
        return False

    try:
        ip = wlan.ifconfig()[0] if wifi_is_connected(wlan) else "0.0.0.0"
    except Exception:
        ip = "0.0.0.0"

    payload = {
        "sys": "dbg",
        "i": msg_id,
        "up": ticks_since_ms(boot_tick_ms) // 1000,
        "ip": ip,
        "wc": 1 if wifi_is_connected(wlan) else 0,
        "pm": get_wifi_pm_value(wlan),
        "udp": udp_packet_count,
        "jerr": json_error_count,
        "unsup": unsupported_type_count,
        "rej": sanity_reject_count,
        "skip": rate_limit_skip_count,
        "qsz": len(outbound_queue),
        "qrepl": queue_replace_count,
        "qdrop": queue_drop_count,
        "fwd": forwarded_count,
        "sockrec": socket_recreate_count,
        "wifirec": wifi_reconnect_count,
        "sockerr": socket_error_count,
        "nwu": consecutive_no_udp_cycles,
        "last_udp_s": elapsed_seconds_or_minus_one(last_any_udp_tick_ms),
        "last_obs_s": elapsed_seconds_or_minus_one(last_obs_st_tick_ms),
        "last_ok_s": elapsed_seconds_or_minus_one(last_valid_weather_tick_ms),
    }

    uart_send_json(payload)
    msg_id += 1
    commit_uart_send()
    last_heartbeat_tick_ms = now_tick
    return True


# ----------------------------
# Main
# ----------------------------
def main():
    global consecutive_main_loop_failures
    global udp_packet_count, json_error_count, last_any_udp_tick_ms
    global socket_error_count

    cause_num, cause_name = reset_cause_name()
    print("=== BRIDGE START ===")
    print("Reset cause:", cause_num, cause_name)
    print("Startup delay:", STARTUP_DELAY_SEC, "seconds")
    time.sleep(STARTUP_DELAY_SEC)

    wlan, _ = ensure_wifi_connected(None, WIFI_SSID, WIFI_PASSWORD)
    print("Pico IP address:", wlan.ifconfig()[0])

    sock = make_udp_listener(UDP_PORT)

    print("Ready. Waiting for Tempest-style UDP packets...")

    while True:
        try:
            wlan, reconnected = ensure_wifi_connected(wlan, WIFI_SSID, WIFI_PASSWORD)

            if reconnected:
                print("Recreating UDP listener after Wi-Fi reconnect...")
                sock = recreate_udp_listener(sock, UDP_PORT)

            # Opportunistically drain one queued item before blocking on recvfrom()
            maybe_send_next_queued()

            # Heartbeat is low frequency and also respects UART send gap
            maybe_send_heartbeat(wlan)

            data, addr = sock.recvfrom(2048)
            udp_packet_count += 1
            last_any_udp_tick_ms = time.ticks_ms()
            reset_no_udp_counter()

            try:
                text = data.decode("utf-8")
            except Exception as e:
                print("Decode error from", addr, ":", e)
                json_error_count += 1
                continue

            try:
                packet_obj = json.loads(text)
            except Exception as e:
                print("JSON parse error from", addr, ":", e)
                json_error_count += 1
                continue

            kind, parsed = parse_supported_packet(packet_obj)
            if kind is None or parsed is None:
                continue

            if enqueue_forward_item(kind, parsed):
                print("Queued", kind, "queue_size=", len(outbound_queue))
            else:
                print("Skipped", kind, "(rate-limited or dropped)")

            # Try to send one item after enqueue as well
            maybe_send_next_queued()

            consecutive_main_loop_failures = 0

        except OSError as e:
            if is_socket_timeout_error(e):
                register_healthy_no_udp_progress()

                # Even with no UDP arriving, continue draining the queue slowly
                try:
                    maybe_send_next_queued()
                    maybe_send_heartbeat(wlan)
                except Exception as send_e:
                    print("Send during timeout loop failed:", send_e)

                wlan, sock = maybe_trigger_no_udp_recovery(wlan, sock)

            else:
                consecutive_main_loop_failures += 1
                socket_error_count += 1
                print("Socket error:", e)

                try:
                    sock = recreate_udp_listener(sock, UDP_PORT)
                except Exception as sock_e:
                    print("Socket recreate failed:", sock_e)

                if consecutive_main_loop_failures >= MAX_CONSECUTIVE_MAIN_LOOP_FAILURES:
                    print("Too many main loop/socket failures. Rebooting Pico...")
                    time.sleep(2)
                    machine.reset()

                time.sleep(1)

        except Exception as e:
            consecutive_main_loop_failures += 1
            print("Main loop error:", e)

            try:
                sock = recreate_udp_listener(sock, UDP_PORT)
            except Exception as sock_e:
                print("Socket recreate failed:", sock_e)

            if consecutive_main_loop_failures >= MAX_CONSECUTIVE_MAIN_LOOP_FAILURES:
                print("Too many main loop failures. Rebooting Pico...")
                time.sleep(2)
                machine.reset()

            time.sleep(1)


main()
