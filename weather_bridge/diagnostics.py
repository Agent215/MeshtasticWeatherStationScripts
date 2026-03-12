class Diagnostics:
    def __init__(self):
        self.counters = {
            "wifi_connect_attempts": 0,
            "wifi_connect_failures": 0,
            "udp_packets_received": 0,
            "udp_packets_malformed": 0,
            "udp_socket_errors": 0,
            "uart_sent": 0,
            "uart_errors": 0,
            "heartbeat_sent": 0,
            "recoveries": 0,
            "top_level_exceptions": 0,
            "memory_pressure_events": 0,
        }
        self.events = []

    def inc(self, key, amount=1):
        self.counters[key] = self.counters.get(key, 0) + amount

    def event(self, name, **fields):
        data = {"event": name}
        data.update(fields)
        self.events.append(data)

    def snapshot(self):
        return {
            "counters": dict(self.counters),
            "events": list(self.events[-100:]),
        }
