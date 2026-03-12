class FakeClock:
    def __init__(self, start=0.0):
        self.t = float(start)

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += float(seconds)

    def advance(self, seconds):
        self.t += float(seconds)


class FakeWifi:
    def __init__(self):
        self.connected = False
        self.connect_calls = 0
        self.fail_connect = False
        self.ip = "192.168.1.99"

    def is_connected(self):
        return self.connected

    def connect(self, ssid, password):
        self.connect_calls += 1
        if self.fail_connect:
            raise OSError("wifi connect failed")
        self.connected = True

    def ip_address(self):
        return self.ip if self.connected else "0.0.0.0"


class FakeUdp:
    def __init__(self, packets=None):
        self.packets = list(packets or [])
        self.reopen_calls = 0
        self.raise_on_recv = None

    def recv(self, max_len):
        if self.raise_on_recv:
            raise self.raise_on_recv
        if not self.packets:
            from weather_bridge.adapters import TimeoutError
            raise TimeoutError()
        return self.packets.pop(0)

    def reopen(self):
        self.reopen_calls += 1


class FakeUart:
    def __init__(self):
        self.lines = []
        self.raise_on_write = False

    def write_line(self, line):
        if self.raise_on_write:
            raise OSError("uart write failed")
        self.lines.append(line)
