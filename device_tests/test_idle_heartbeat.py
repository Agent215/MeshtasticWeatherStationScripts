from common import build_runtime, ok, fail

NAME = "idle_heartbeat"

try:
    rt, clock, _wifi, _udp, uart = build_runtime()
    rt.tick()
    count1 = len([x for x in uart.lines if '"sys":"ok"' in x])
    clock.advance(11)
    rt.tick()
    count2 = len([x for x in uart.lines if '"sys":"ok"' in x])
    assert count2 > count1
    ok(NAME)
except Exception as e:
    fail(NAME, e)
