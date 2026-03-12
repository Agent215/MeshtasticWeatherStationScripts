import json

from tests.fakes import FakeClock, FakeUart, FakeUdp, FakeWifi
from weather_bridge.runtime import BridgeConfig, BridgeRuntime, FaultInjector
from weather_bridge.state_machine import BridgeState


def make_obs_packet(ts=1700000000):
    obs = [ts, 0.1, 3.4, 0, 220, 0, 1011.2, 22.1, 57, 10000, 0, 0, 0.2]
    return json.dumps({"type": "obs_st", "obs": [obs]}).encode("utf-8")


def make_runtime(packets=None):
    clock = FakeClock(start=0)
    wifi = FakeWifi()
    udp = FakeUdp(packets=packets)
    uart = FakeUart()
    cfg = BridgeConfig(
        wifi_ssid="ssid",
        wifi_password="pw",
        heartbeat_interval_sec=10,
        min_forward_interval_sec=5,
        fatal_error_threshold=3,
        loop_sleep_sec=0,
    )
    rt = BridgeRuntime(cfg, wifi=wifi, udp=udp, uart=uart, clock=clock, injector=FaultInjector())
    return rt, clock, wifi, udp, uart


def test_component_happy_path_with_heartbeat_and_weather_forward():
    rt, clock, wifi, _udp, uart = make_runtime(packets=[make_obs_packet()])
    rt.tick()  # connects wifi, sends heartbeat, forwards packet
    assert wifi.connected
    assert len(uart.lines) == 2
    assert '"sys":"ok"' in uart.lines[0]
    assert '"t":22.1' in uart.lines[1]


def test_malformed_packet_tolerance_counter():
    rt, _clock, _wifi, udp, _uart = make_runtime(packets=[b"{bad-json"])
    rt.tick()
    assert rt.diag.counters["udp_packets_malformed"] == 1
    assert rt.state_machine.state == BridgeState.UDP_READY


def test_exception_containment_socket_then_recovery():
    rt, clock, _wifi, udp, _uart = make_runtime()
    udp.raise_on_recv = RuntimeError("boom")
    rt.tick()
    assert rt.state_machine.state == BridgeState.RECOVERING
    assert udp.reopen_calls == 1
    clock.advance(2)
    udp.raise_on_recv = None
    rt.tick()
    assert rt.state_machine.state == BridgeState.UDP_READY


def test_fault_injection_uart_write_exception_goes_recovering():
    rt, _clock, _wifi, _udp, _uart = make_runtime(packets=[make_obs_packet()])
    rt.injector.force_uart_exception = True
    rt.tick()
    assert rt.diag.counters["uart_errors"] >= 1
    assert rt.state_machine.state == BridgeState.RECOVERING


def test_wifi_reconnect_behavior_via_fake_wifi():
    rt, clock, wifi, _udp, _uart = make_runtime()
    wifi.fail_connect = True
    rt.tick()
    assert rt.state_machine.state == BridgeState.RECOVERING
    wifi.fail_connect = False
    clock.advance(2)
    rt.tick()
    assert rt.state_machine.state in (BridgeState.UDP_READY,)


def test_forced_top_level_exception_reaches_fatal_restart_pending():
    rt, clock, _wifi, _udp, _uart = make_runtime()
    rt.injector.force_main_loop_exception = True
    rt.tick()
    clock.advance(2)
    rt.tick()
    clock.advance(2)
    rt.tick()
    assert rt.state_machine.state == BridgeState.FATAL_RESTART_PENDING
