import network
import socket
import time
import json
import machine
from machine import Pin, UART

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

# ----------------------------
# Hardware init
# ----------------------------
led = Pin("LED", Pin.OUT)
uart = UART(0, baudrate=UART_BAUD, tx=Pin(UART_TX_PIN), rx=Pin(UART_RX_PIN))

msg_id = 1
last_obs_ts = None
last_forward_monotonic = 0
last_heartbeat_monotonic = 0
boot_monotonic = time.time()
consecutive_wifi_failures = 0
consecutive_main_loop_failures = 0


def blink(times=1, on_time=0.12, off_time=0.12):
    for _ in range(times):
        led.on()
        time.sleep(on_time)
        led.off()
        time.sleep(off_time)


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
    wlan.active(True)
    time.sleep(1)
    return wlan


def connect_wifi(ssid, password, timeout_sec=WIFI_CONNECT_TIMEOUT_SEC):
    wlan = reset_wlan()

    print("Connecting to Wi-Fi:", ssid)
    wlan.connect(ssid, password)

    start = time.time()
    while not wlan.isconnected():
        if time.time() - start > timeout_sec:
            raise RuntimeError("Wi-Fi connection timeout")
        blink(1, 0.05, 0.20)
        time.sleep(1)

    print("Wi-Fi connected")
    print("IP config:", wlan.ifconfig())
    blink(3, 0.08, 0.08)
    return wlan


def ensure_wifi_connected(wlan, ssid, password):
    global consecutive_wifi_failures

    if wifi_is_connected(wlan):
        consecutive_wifi_failures = 0
        return wlan

    print("Wi-Fi disconnected. Reconnecting...")

    while True:
        try:
            wlan = connect_wifi(ssid, password)
            print("Wi-Fi reconnected. IP:", wlan.ifconfig()[0])
            consecutive_wifi_failures = 0
            return wlan
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
    try:
        if old_sock is not None:
            old_sock.close()
    except Exception:
        pass
    return make_udp_listener(port)


def round_weather_fields(obs):
    return {
        "ts": int(obs[0]),
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


def is_fresh_observation(compact_weather):
    global last_obs_ts

    obs_ts = compact_weather["ts"]
    if last_obs_ts is not None and obs_ts <= last_obs_ts:
        return False

    last_obs_ts = obs_ts
    return True


def may_forward_now():
    global last_forward_monotonic
    now = time.time()

    if last_forward_monotonic != 0 and (now - last_forward_monotonic) < MIN_FORWARD_INTERVAL_SEC:
        return False

    last_forward_monotonic = now
    return True


def uart_send_json(payload):
    msg = json.dumps(payload, separators=(",", ":")) + "\n"
    uart.write(msg)
    print("UART sent:", msg.strip())
    blink(1, 0.15, 0.05)


def send_weather_to_rak(compact_weather):
    global msg_id

    payload = {
        "i": msg_id,
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
    global last_heartbeat_monotonic, msg_id

    now = time.time()
    if last_heartbeat_monotonic != 0 and (now - last_heartbeat_monotonic) < HEARTBEAT_INTERVAL_SEC:
        return

    try:
        ip = wlan.ifconfig()[0] if wifi_is_connected(wlan) else "0.0.0.0"
    except Exception:
        ip = "0.0.0.0"

    uptime = int(now - boot_monotonic)

    payload = {
        "sys": "ok",
        "i": msg_id,
        "up": uptime,
        "ip": ip
    }

    uart_send_json(payload)
    msg_id += 1
    last_heartbeat_monotonic = now


def main():
    global consecutive_main_loop_failures

    print("Startup delay:", STARTUP_DELAY_SEC, "seconds")
    time.sleep(STARTUP_DELAY_SEC)

    wlan = ensure_wifi_connected(None, WIFI_SSID, WIFI_PASSWORD)
    print("Pico IP address:", wlan.ifconfig()[0])

    sock = make_udp_listener(UDP_PORT)

    print("Ready. Waiting for Tempest-style UDP packets...")
    while True:
        try:
            wlan = ensure_wifi_connected(wlan, WIFI_SSID, WIFI_PASSWORD)
            maybe_send_heartbeat(wlan)

            data, addr = sock.recvfrom(2048)

            try:
                text = data.decode("utf-8")
            except Exception as e:
                print("Decode error:", e)
                continue

            try:
                packet_obj = json.loads(text)
            except Exception as e:
                print("JSON parse error:", e)
                continue

            compact_weather = parse_obs_st(packet_obj)
            if compact_weather is None:
                continue

            if not is_fresh_observation(compact_weather):
                continue

            if not may_forward_now():
                continue

            print("Parsed fresh obs_st:", compact_weather)
            send_weather_to_rak(compact_weather)
            consecutive_main_loop_failures = 0

        except OSError:
            # Normal socket timeout
            pass
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