import json

from weather_bridge.runtime import BridgeConfig, BridgeRuntime, FaultInjector


class FakeClock:
    def __init__(self):
        self._now = 0

    def now(self):
        return self._now

    def sleep(self, sec):
        self._now += sec

    def advance(self, sec):
        self._now += sec


class FakeWifi:
    def __init__(self):
        self.connected = False
        self.fail_connect = False

    def is_connected(self):
        return self.connected

    def connect(self, ssid, password):
        if self.fail_connect:
            raise OSError("wifi fail")
        self.connected = True

    def ip_address(self):
        return "192.168.4.10" if self.connected else "0.0.0.0"


class FakeUdp:
    def __init__(self):
        self.packets = []
        self.raise_exc = False

    def recv(self, max_len):
        if self.raise_exc:
            raise OSError("socket fail")
        if self.packets:
            return self.packets.pop(0)
        from weather_bridge.adapters import TimeoutError
        raise TimeoutError()

    def reopen(self):
        pass


class FakeUart:
    def __init__(self):
        self.lines = []
        self.fail = False

    def write_line(self, line):
        if self.fail:
            raise OSError("uart fail")
        self.lines.append(line)


def obs_packet(ts=1700000000):
    obs = [ts, 0.1, 3.4, 0, 220, 0, 1011.2, 22.1, 57, 10000, 0, 0, 0.2]
    return json.dumps({"type": "obs_st", "obs": [obs]}).encode()


def build_runtime():
    clock = FakeClock()
    wifi = FakeWifi()
    udp = FakeUdp()
    uart = FakeUart()
    cfg = BridgeConfig("ssid", "pw", heartbeat_interval_sec=10, min_forward_interval_sec=5, fatal_error_threshold=3)
    rt = BridgeRuntime(cfg, wifi=wifi, udp=udp, uart=uart, clock=clock, injector=FaultInjector())
    return rt, clock, wifi, udp, uart


def ok(name):
    print("PASS", name)


def fail(name, e):
    print("FAIL", name, e)
