from common import build_runtime, ok, fail

NAME = "wifi_reconnect"

try:
    rt, clock, wifi, _udp, _uart = build_runtime()
    wifi.fail_connect = True
    rt.tick()
    assert rt.state_machine.state == "RECOVERING"
    wifi.fail_connect = False
    clock.advance(2)
    rt.tick()
    assert rt.state_machine.state == "UDP_READY"
    ok(NAME)
except Exception as e:
    fail(NAME, e)
