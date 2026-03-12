from common import build_runtime, obs_packet, ok, fail

NAME = "udp_receive"

try:
    rt, _clock, _wifi, udp, uart = build_runtime()
    udp.packets.append(obs_packet())
    rt.tick()
    assert len(uart.lines) >= 2
    ok(NAME)
except Exception as e:
    fail(NAME, e)
