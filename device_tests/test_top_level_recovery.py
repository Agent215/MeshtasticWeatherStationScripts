from common import build_runtime, ok, fail

NAME = "top_level_recovery"

try:
    rt, clock, _wifi, _udp, _uart = build_runtime()
    rt.injector.force_main_loop_exception = True
    rt.tick()
    clock.advance(2)
    rt.tick()
    clock.advance(2)
    rt.tick()
    assert rt.state_machine.state == "FATAL_RESTART_PENDING"
    ok(NAME)
except Exception as e:
    fail(NAME, e)
