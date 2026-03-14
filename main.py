import network
import socket
import time
import json
import machine
from machine import Pin, UART, WDT

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
HEARTBEAT_INTERVAL_SEC = 6 * 60 * 60   # every 6 hours

# Optional small pause after socket recreation / reconnect
POST_RECOVERY_DELAY_MS = 250

# Watchdog
ENABLE_WATCHDOG = True
WATCHDOG_TIMEOUT_MS = 8000  # RP2040 practical max is ~8388 ms

# No-weather staged recovery thresholds
NO_WEATHER_SOCKET_RECOVERY_THRESHOLD = 60
NO_WEATHER_WIFI_RECOVERY_THRESHOLD = 120
NO_WEATHER_REBOOT_THRESHOLD = 180

# ----------------------------
# Hardware init
# ----------------------------
led = Pin("LED", Pin.OUT)
uart = UART(0, baudrate=UART_BAUD, tx=Pin(UART_TX_PIN), rx=Pin(UART_RX_PIN))

# IMPORTANT:
# Create watchdog only after Wi-Fi + socket setup succeeds.
wdt = None

# ----------------------------
# Global runtime state
# ----------------------------
msg_id = 1

last_forward_tick_ms = None
last_heartbeat_tick_ms = None
boot_tick_ms = time.ticks_ms()

consecutive_wifi_failures = 0
consecutive_main_loop_failures = 0
consecutive_no_weather_feeds = 0

# staged recovery guards so we don't repeat the same action every loop
socket_recovery_triggered = False
wifi_recovery_triggered = False


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


def feed_watchdog():
    if wdt is not None:
        wdt.feed()


def register_healthy_no_weather_progress():
    """
    Feed watchdog for a healthy loop cycle that did NOT send weather,
    and increment the no-weather counter.
    Heartbeat does not reset this counter.
    """
    global consecutive_no_weather_feeds
    feed_watchdog()
    consecutive_no_weather_feeds += 1


def reset_no_weather_counter():
    """
    Reset only on successful weather send.
    """
    global consecutive_no_weather_feeds
    global socket_recovery_triggered, wifi_recovery_triggered

    consecutive_no_weather_feeds = 0
    socket_recovery_triggered = False
    wifi_recovery_triggered = False


def wifi_is_connected(wlan):
    try:
        return wlan is not None and wlan.active() and wlan.isconnected()
    except Exception:
        return False


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
        feed_watchdog()
        time.sleep(1)

    print("Wi-Fi connected")
    print("IP config:", wlan.ifconfig())
    blink(3, 0.08, 0.08)
    feed_watchdog()
    return wlan


def ensure_wifi_connected(wlan, ssid, password):
    """
    Returns (wlan, reconnected)
    reconnected=True means caller should recreate UDP socket.
    """
    global consecutive_wifi_failures

    if wifi_is_connected(wlan):
        consecutive_wifi_failures = 0
        return wlan, False

    print("Wi-Fi disconnected. Reconnecting...")

    while True:
        try:
            wlan = connect_wifi(ssid, password)
            print("Wi-Fi reconnected. IP:", wlan.ifconfig()[0])
            consecutive_wifi_failures = 0
            return wlan, True

        except Exception as e:
            consecutive_wifi_failures += 1
            print("Wi-Fi reconnect failed:", e)
            blink(2, 0.05, 0.10)

            if consecutive_wifi_failures >= MAX_CONSECUTIVE_WIFI_FAILURES:
                print("Too many Wi-Fi failures. Rebooting Pico...")
                time.sleep(2)
                machine.reset()

            feed_watchdog()
            time.sleep(WIFI_RETRY_DELAY_SEC)


def make_udp_listener(port):
    addr = socket.getaddrinfo("0.0.0.0", port)[0][-1]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(addr)
    sock.settimeout(SOCKET_TIMEOUT_SEC)
    print("Listening for UDP on port", port)
    return sock


def recreate_udp_listener(old_sock, port):
    try:
        if old_sock is not None:
            old_sock.close()
    except Exception:
        pass

    sock = make_udp_listener(port)
    time.sleep_ms(POST_RECOVERY_DELAY_MS)
    feed_watchdog()
    return sock


def is_socket_timeout_error(exc):
    try:
        args = getattr(exc, "args", ())
        if args:
            first = args[0]

            if isinstance(first, int):
                if first in (110,):
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


def maybe_trigger_no_weather_recovery(wlan, sock):
    """
    Staged recovery based on consecutive healthy watchdog feeds without a weather send.

    >= 60  -> recreate UDP socket
    >= 120 -> reconnect Wi-Fi
    >= 180 -> reboot Pico

    Returns (wlan, sock)
    """
    global socket_recovery_triggered, wifi_recovery_triggered, consecutive_no_weather_feeds

    if consecutive_no_weather_feeds >= NO_WEATHER_REBOOT_THRESHOLD:
        print("No weather sends for", consecutive_no_weather_feeds, "healthy feeds. Rebooting Pico...")
        time.sleep(2)
        machine.reset()

    if (consecutive_no_weather_feeds >= NO_WEATHER_WIFI_RECOVERY_THRESHOLD
            and not wifi_recovery_triggered):
        print("No weather sends for", consecutive_no_weather_feeds,
              "healthy feeds. Forcing Wi-Fi reconnect...")
        wifi_recovery_triggered = True

        try:
            wlan = reset_wlan()
            wlan, reconnected = ensure_wifi_connected(wlan, WIFI_SSID, WIFI_PASSWORD)
            if reconnected:
                sock = recreate_udp_listener(sock, UDP_PORT)
        except Exception as e:
            print("Wi-Fi staged recovery failed:", e)

        return wlan, sock

    if (consecutive_no_weather_feeds >= NO_WEATHER_SOCKET_RECOVERY_THRESHOLD
            and not socket_recovery_triggered):
        print("No weather sends for", consecutive_no_weather_feeds,
              "healthy feeds. Recreating UDP socket...")
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
        "ts": int(obs[0]),                  # Tempest observation timestamp
        "t": round(float(obs[7]), 1),
        "h": int(round(float(obs[8]))),
        "p": round(float(obs[6]), 1),
        "w": round(float(obs[2]), 1),
        "d": int(round(float(obs[4]))),
        "r": round(float(obs[12]), 1),
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
    if not (0 <= w["d"] <= 360):
        return False
    if not (0 <= w["r"] <= 1000):
        return False
    return True


def parse_obs_st(packet_obj):
    if packet_obj.get("type") != "obs_st":
        return None

    obs_list = packet_obj.get("obs")
    if not obs_list or not isinstance(obs_list, list):
        return None

    obs = obs_list[0]
    if not isinstance(obs, list) or len(obs) < 13:
        return None

    try:
        w = round_weather_fields(obs)
        if not weather_values_are_sane(w):
            print("Rejected out-of-range weather payload:", w)
            return None
        return w
    except Exception as e:
        print("obs_st field parse error:", e)
        return None


def may_forward_now():
    """
    Check only. Do not commit state here.
    """
    global last_forward_tick_ms

    if last_forward_tick_ms is None:
        return True

    min_interval_ms = seconds_to_ms(MIN_FORWARD_INTERVAL_SEC)
    if time.ticks_diff(time.ticks_ms(), last_forward_tick_ms) < min_interval_ms:
        return False

    return True


def commit_forward_success():
    """
    Commit delivery-related state only after successful UART send.
    """
    global last_forward_tick_ms
    last_forward_tick_ms = time.ticks_ms()


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

    print("UART sent:", msg.strip())
    blink(1, 0.15, 0.05)


def send_weather_to_rak(compact_weather):
    global msg_id

    payload = {
        "i": msg_id,
        "ts": compact_weather["ts"],
        "t": compact_weather["t"],
        "h": compact_weather["h"],
        "p": compact_weather["p"],
        "w": compact_weather["w"],
        "d": compact_weather["d"],
        "r": compact_weather["r"],
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

    uptime = ticks_since_ms(boot_tick_ms) // 1000

    payload = {
        "sys": "ok",
        "i": msg_id,
        "up": uptime,
        "ip": ip,
        "nw": consecutive_no_weather_feeds
    }

    uart_send_json(payload)
    msg_id += 1
    last_heartbeat_tick_ms = now_tick
    return True


# ----------------------------
# Main
# ----------------------------
def main():
    global consecutive_main_loop_failures, wdt

    print("Startup delay:", STARTUP_DELAY_SEC, "seconds")
    time.sleep(STARTUP_DELAY_SEC)

    wlan, _ = ensure_wifi_connected(None, WIFI_SSID, WIFI_PASSWORD)
    print("Pico IP address:", wlan.ifconfig()[0])

    sock = make_udp_listener(UDP_PORT)

    # Enable watchdog only after successful startup.
    if ENABLE_WATCHDOG:
        wdt = WDT(timeout=WATCHDOG_TIMEOUT_MS)
        feed_watchdog()
        print("Watchdog enabled")

    print("Ready. Waiting for Tempest-style UDP packets...")

    while True:
        sent_weather_this_cycle = False

        try:
            wlan, reconnected = ensure_wifi_connected(wlan, WIFI_SSID, WIFI_PASSWORD)
            feed_watchdog()

            if reconnected:
                print("Recreating UDP listener after Wi-Fi reconnect...")
                sock = recreate_udp_listener(sock, UDP_PORT)
                feed_watchdog()

            heartbeat_sent = maybe_send_heartbeat(wlan)
            if heartbeat_sent:
                # Feed watchdog, but do NOT reset no-weather counter.
                feed_watchdog()

            data, addr = sock.recvfrom(2048)

            try:
                text = data.decode("utf-8")
            except Exception as e:
                print("Decode error from", addr, ":", e)
                register_healthy_no_weather_progress()
                wlan, sock = maybe_trigger_no_weather_recovery(wlan, sock)
                continue

            try:
                packet_obj = json.loads(text)
            except Exception as e:
                print("JSON parse error from", addr, ":", e)
                register_healthy_no_weather_progress()
                wlan, sock = maybe_trigger_no_weather_recovery(wlan, sock)
                continue

            compact_weather = parse_obs_st(packet_obj)
            if compact_weather is None:
                register_healthy_no_weather_progress()
                wlan, sock = maybe_trigger_no_weather_recovery(wlan, sock)
                continue

            if not may_forward_now():
                register_healthy_no_weather_progress()
                wlan, sock = maybe_trigger_no_weather_recovery(wlan, sock)
                continue

            print("Parsed obs_st:", compact_weather)

            send_weather_to_rak(compact_weather)
            commit_forward_success()
            reset_no_weather_counter()
            feed_watchdog()
            sent_weather_this_cycle = True

            consecutive_main_loop_failures = 0

        except OSError as e:
            if is_socket_timeout_error(e):
                # Normal listener timeout: loop is still healthy, but no weather sent
                register_healthy_no_weather_progress()
                wlan, sock = maybe_trigger_no_weather_recovery(wlan, sock)
            else:
                consecutive_main_loop_failures += 1
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