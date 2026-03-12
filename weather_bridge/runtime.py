from dataclasses import dataclass

from .adapters import TimeoutError
from .core import (
    build_heartbeat_payload,
    build_weather_payload,
    parse_tempest_packet,
    to_compact_json_line,
)
from .diagnostics import Diagnostics
from .retry import BackoffPolicy
from .state_machine import BridgeState, StateMachine


class FaultInjector:
    def __init__(self):
        self.force_wifi_loss = False
        self.force_malformed_udp = False
        self.force_socket_exception = False
        self.force_uart_exception = False
        self.force_memory_pressure = False
        self.force_main_loop_exception = False


@dataclass
class BridgeConfig:
    wifi_ssid: str
    wifi_password: str
    min_forward_interval_sec: float = 60.0
    heartbeat_interval_sec: float = 21600.0
    loop_sleep_sec: float = 0.2
    fatal_error_threshold: int = 10


class BridgeRuntime:
    def __init__(self, config, wifi, udp, uart, clock, diagnostics=None, injector=None):
        self.config = config
        self.wifi = wifi
        self.udp = udp
        self.uart = uart
        self.clock = clock
        self.diag = diagnostics or Diagnostics()
        self.injector = injector or FaultInjector()
        self.state_machine = StateMachine()

        self.boot_time = self.clock.now()
        self.msg_id = 1
        self.last_obs_ts = None
        self.last_forward_at = 0
        self.last_heartbeat_at = 0
        self.next_recovery_at = 0
        self.consecutive_errors = 0
        self.backoff = BackoffPolicy()

    def tick(self):
        try:
            if self.injector.force_main_loop_exception:
                raise RuntimeError("forced main loop exception")

            if self.injector.force_memory_pressure:
                self.diag.inc("memory_pressure_events")
                raise MemoryError("forced memory pressure")

            self._ensure_wifi()
            self._maybe_send_heartbeat()

            if self.state_machine.state != BridgeState.UDP_READY:
                return

            self._receive_and_forward_once()
            self.consecutive_errors = 0
            self.backoff.reset()
        except TimeoutError:
            # no UDP traffic is normal
            return
        except Exception as exc:
            self.diag.inc("top_level_exceptions")
            self._enter_recovery(exc)

    def _ensure_wifi(self):
        if self.injector.force_wifi_loss:
            self.state_machine.set_state(BridgeState.WIFI_CONNECTING, reason="fault_injected_wifi_loss")
        if self.wifi.is_connected() and not self.injector.force_wifi_loss:
            if self.state_machine.state in (BridgeState.BOOTING, BridgeState.WIFI_CONNECTING, BridgeState.RECOVERING):
                self.state_machine.set_state(BridgeState.UDP_READY, reason="wifi_connected")
            return

        now = self.clock.now()
        if now < self.next_recovery_at:
            self.state_machine.set_state(BridgeState.RECOVERING, reason="waiting_for_backoff")
            return

        self.state_machine.set_state(BridgeState.WIFI_CONNECTING, reason="wifi_not_connected")
        self.diag.inc("wifi_connect_attempts")
        try:
            self.wifi.connect(self.config.wifi_ssid, self.config.wifi_password)
            if self.wifi.is_connected() and not self.injector.force_wifi_loss:
                self.state_machine.set_state(BridgeState.UDP_READY, reason="wifi_reconnected")
                self.backoff.reset()
                self.consecutive_errors = 0
                return
            raise RuntimeError("wifi connect attempt did not result in connected state")
        except Exception:
            self.diag.inc("wifi_connect_failures")
            raise

    def _receive_and_forward_once(self):
        if self.injector.force_socket_exception:
            self.diag.inc("udp_socket_errors")
            raise OSError("forced socket exception")

        raw = self.udp.recv(2048)
        self.diag.inc("udp_packets_received")

        if self.injector.force_malformed_udp:
            raw = b"{bad"

        weather = parse_tempest_packet(raw)
        if weather is None:
            self.diag.inc("udp_packets_malformed")
            return

        if not self._is_fresh(weather["ts"]):
            return

        if not self._may_forward_now():
            return

        payload = build_weather_payload(self.msg_id, weather)
        self._uart_send(payload)

    def _uart_send(self, payload):
        line = to_compact_json_line(payload)
        if self.injector.force_uart_exception:
            self.diag.inc("uart_errors")
            raise OSError("forced uart exception")
        self.uart.write_line(line)
        self.diag.inc("uart_sent")
        self.msg_id += 1

    def _is_fresh(self, ts):
        if self.last_obs_ts is not None and ts <= self.last_obs_ts:
            return False
        self.last_obs_ts = ts
        return True

    def _may_forward_now(self):
        now = self.clock.now()
        if self.last_forward_at and (now - self.last_forward_at) < self.config.min_forward_interval_sec:
            return False
        self.last_forward_at = now
        return True

    def _maybe_send_heartbeat(self):
        now = self.clock.now()
        if self.last_heartbeat_at and (now - self.last_heartbeat_at) < self.config.heartbeat_interval_sec:
            return
        payload = build_heartbeat_payload(self.msg_id, now - self.boot_time, self.wifi.ip_address())
        self._uart_send(payload)
        self.diag.inc("heartbeat_sent")
        self.last_heartbeat_at = now

    def _enter_recovery(self, exc):
        self.diag.inc("recoveries")
        self.diag.event("recovery", error=str(exc), state=self.state_machine.state)
        self.state_machine.set_state(BridgeState.RECOVERING, reason="exception")

        try:
            self.udp.reopen()
        except Exception:
            self.diag.inc("udp_socket_errors")

        self.consecutive_errors += 1
        if self.consecutive_errors >= self.config.fatal_error_threshold:
            self.state_machine.set_state(BridgeState.FATAL_RESTART_PENDING, reason="fatal_threshold")
            return

        self.next_recovery_at = self.clock.now() + self.backoff.next_delay()

    def run_forever(self, reset_callback, max_ticks=None):
        ticks = 0
        while True:
            self.tick()
            if self.state_machine.is_fatal():
                reset_callback()
                return
            self.clock.sleep(self.config.loop_sleep_sec)
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                return
