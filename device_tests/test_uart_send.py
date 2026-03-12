from common import build_runtime, obs_packet, ok, fail

NAME = "uart_send_behavior"

try:
    rt, _clock, _wifi, udp, uart = build_runtime()
    udp.packets.append(obs_packet())
    rt.tick()
    assert any('"t":22.1' in line for line in uart.lines)
    ok(NAME)
except Exception as e:
    fail(NAME, e)
