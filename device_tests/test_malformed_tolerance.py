from common import build_runtime, ok, fail

NAME = "malformed_packet_tolerance"

try:
    rt, _clock, _wifi, udp, _uart = build_runtime()
    udp.packets.append(b"{bad")
    rt.tick()
    assert rt.diag.counters["udp_packets_malformed"] == 1
    ok(NAME)
except Exception as e:
    fail(NAME, e)
