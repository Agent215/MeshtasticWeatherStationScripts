import network
import socket
import time
import json
import machine
from machine import Pin, UART

# ============================================================
# Pico W Tempest UDP -> UART bridge
#
# Changes in this version:
#   - Disable Wi-Fi power saving
#   - Debug heartbeat every 15 minutes
#   - Recovery based on NO UDP RECEIVED, not no forwarded weather
#   - Extra recovery/debug counters in heartbeat
# ============================================================

# ----------------------------
# User settings
# ----------------------------
WIFI_SSID = "YOUR_WIFI_NAME"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"

UDP_PORT = 50222

# UART to RAK / Meshtastic
UART_BAUD = 115200
UART_TX_PIN = 0   # GP0
UART_RX_PIN = 1   # GP1

# Timing / resilience
SOCKET_TIMEOUT_SEC = 1.0
WIFI_CONNECT_TIMEOUT_SEC = 20
WIFI_RETRY_DELAY_SEC = 3
MIN_FORWARD_INTERVAL_SEC = 60
MAX_CONSECUTIVE_WIFI_FAILURES = 10
MAX_CONSECUTIVE_MAIN_LOOP_FAILURES = 10

# Startup / heartbeat
STARTUP_DELAY_SEC = 3
HEARTBEAT_INTERVAL_SEC = 15 * 60   # every 15 minutes

# Optional small pause after socket recreation / reconnect
POST_RECOVERY_DELAY_MS = 250

# No-UDP staged recovery thresholds
# Because recvfrom() times out every ~1 second, these behave roughly like seconds.
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

last_forward_tick_ms = None
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
non_obs_count = 0
sanity_reject_count = 0
rate_limit_skip_count = 0

socket_recreate_count = 0
wifi_reconnect_count = 0
socket_error_count = 0

last_any_udp_tick_ms = None
last_obs_st_tick_ms = None
last_valid_weather_tick_ms = None


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
    """
    Prefer official PM_NONE constant when available.
    Fall back to pm=0 if needed.
    """
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
    """
    Staged recovery based on consecutive healthy loop cycles with NO UDP RECEIVED.

    >= 120 -> recreate UDP socket
    >= 300 -> reconnect Wi-Fi
    >= very high threshold -> reboot (disabled for now)
    """
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
# Weather parsing / validation
# ----------------------------
def round_weather_fields(obs):
    return {
        "ts": int(obs[0]),
        "t": round(float(obs[7]), 1),
        "h": int(round(float(obs[8]))),
        "p": round(float(obs[6]), 1),
        "w": round(float(obs[2]), 1),
        "g": round(float(obs[3]), 1),
        "l": round(float(obs[1]), 1),
        "d": int(round(float(obs[4]))),
        "r": round(float(obs[12]), 1),
        "uv": round(float(obs[10]), 1),
        "sr": round(float(obs[11]), 1),
        "lux": int(round(float(obs[9]))),
        "bat": round(float(obs[16]), 2),
        "ld": round(float(obs[14]), 1),
        "lc": int(round(float(obs[15]))),
        "pt": int(round(float(obs[13]))),
        "ri": int(round(float(obs[17]))),
        "rd": round(float(obs[18]), 1),
        "nr": round(float(obs[19]), 1),
        "nrd": round(float(obs[20]), 1),
        "pa": int(round(float(obs[21]))),
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
    if not (0 <= w["rd"] <= 2000):
        return False
    if not (0 <= w["nr"] <= 2000):
        return False
    if not (0 <= w["nrd"] <= 2000):
        return False
    if not (0 <= w["pa"] <= 10):
        return False
    return True


def parse_obs_st(packet_obj):
    global non_obs_count, sanity_reject_count, last_obs_st_tick_ms

    if packet_obj.get("type") != "obs_st":
        non_obs_count += 1
        return None

    last_obs_st_tick_ms = time.ticks_ms()

    obs_list = packet_obj.get("obs")
    if not obs_list or not isinstance(obs_list, list):
        sanity_reject_count += 1
        return None

    obs = obs_list[0]
    if not isinstance(obs, list) or len(obs) < 22:
        sanity_reject_count += 1
        return None

    try:
        w = round_weather_fields(obs)
        if not weather_values_are_sane(w):
            print("Rejected out-of-range weather payload:", w)
            sanity_reject_count += 1
            return None
        return w
    except Exception as e:
        print("obs_st field parse error:", e)
        sanity_reject_count += 1
        return None


def may_forward_now():
    global last_forward_tick_ms

    if last_forward_tick_ms is None:
        return True

    min_interval_ms = seconds_to_ms(MIN_FORWARD_INTERVAL_SEC)
    if time.ticks_diff(time.ticks_ms(), last_forward_tick_ms) < min_interval_ms:
        return False

    return True


def commit_forward_success():
    global last_forward_tick_ms, last_valid_weather_tick_ms
    last_forward_tick_ms = time.ticks_ms()
    last_valid_weather_tick_ms = last_forward_tick_ms


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


def send_weather_to_rak(weather):
    global msg_id

    payload = {
        "i": msg_id,
        "ts": weather["ts"],
        "t": weather["t"],
        "h": weather["h"],
        "p": weather["p"],
        "w": weather["w"],
        "g": weather["g"],
        "l": weather["l"],
        "d": weather["d"],
        "r": weather["r"],
        "uv": weather["uv"],
        "sr": weather["sr"],
        "lux": weather["lux"],
        "bat": weather["bat"],
        "ld": weather["ld"],
        "lc": weather["lc"],
        "pt": weather["pt"],
        "ri": weather["ri"],
        "rd": weather["rd"],
        "nr": weather["nr"],
        "nrd": weather["nrd"],
        "pa": weather["pa"],
    }

    uart_send_json(payload)
    msg_id += 1


def maybe_send_heartbeat(wlan):
    global last_heartbeat_tick_ms, msg_id

    now_tick = time.ticks_ms()
    interval_ms = seconds_to_ms(HEARTBEAT_INTERVAL_SEC)

    if last_heartbeat_tick_ms is not None:
        if time.ticks_diff(now_tick, last_heartbeat_tick_ms) < interval_ms:
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
        "nobs": non_obs_count,
        "rej": sanity_reject_count,
        "skip": rate_limit_skip_count,
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
    last_heartbeat_tick_ms = now_tick
    return True


# ----------------------------
# Main
# ----------------------------
def main():
    global consecutive_main_loop_failures
    global udp_packet_count, json_error_count, rate_limit_skip_count, last_any_udp_tick_ms
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

            weather = parse_obs_st(packet_obj)
            if weather is None:
                continue

            if not may_forward_now():
                rate_limit_skip_count += 1
                continue

            print("Parsed obs_st:", weather)

            send_weather_to_rak(weather)
            commit_forward_success()
            consecutive_main_loop_failures = 0

        except OSError as e:
            if is_socket_timeout_error(e):
                register_healthy_no_udp_progress()
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
