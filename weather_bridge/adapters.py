import socket
import time


class TimeoutError(Exception):
    pass


class ClockAdapter:
    def now(self):
        raise NotImplementedError

    def sleep(self, seconds):
        raise NotImplementedError


class WifiAdapter:
    def is_connected(self):
        raise NotImplementedError

    def connect(self, ssid, password):
        raise NotImplementedError

    def ip_address(self):
        raise NotImplementedError


class UdpAdapter:
    def recv(self, max_len):
        raise NotImplementedError

    def reopen(self):
        raise NotImplementedError


class UartAdapter:
    def write_line(self, line):
        raise NotImplementedError


class StdClock(ClockAdapter):
    def now(self):
        return time.time()

    def sleep(self, seconds):
        time.sleep(seconds)


class MicroPythonWifi(WifiAdapter):
    def __init__(self, network_module):
        self.network = network_module
        self.wlan = self.network.WLAN(self.network.STA_IF)
        self.wlan.active(True)

    def is_connected(self):
        try:
            return self.wlan.isconnected()
        except Exception:
            return False

    def connect(self, ssid, password):
        self.wlan.active(True)
        self.wlan.connect(ssid, password)

    def ip_address(self):
        try:
            return self.wlan.ifconfig()[0]
        except Exception:
            return "0.0.0.0"


class MicroPythonUdp(UdpAdapter):
    def __init__(self, port, timeout=1.0):
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.reopen()

    def reopen(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        addr = socket.getaddrinfo("0.0.0.0", self.port)[0][-1]
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(addr)
        self.sock.settimeout(self.timeout)

    def recv(self, max_len):
        try:
            data, _addr = self.sock.recvfrom(max_len)
            return data
        except OSError:
            raise TimeoutError


class MicroPythonUart(UartAdapter):
    def __init__(self, uart_obj):
        self.uart = uart_obj

    def write_line(self, line):
        self.uart.write(line)
