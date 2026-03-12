import json

from weather_bridge.core import (
    build_weather_payload,
    parse_tempest_packet,
    to_compact_json_line,
)
from weather_bridge.retry import BackoffPolicy
from weather_bridge.state_machine import BridgeState, StateMachine


def make_obs_packet(ts=1700000000, t=21.4, h=55, p=1012.2, w=3.6, d=250, r=0.0):
    obs = [ts, 0.1, w, 0, d, 0, p, t, h, 10000, 0, 0, r]
    return json.dumps({"type": "obs_st", "obs": [obs]}).encode("utf-8")


def test_packet_parsing_valid():
    result = parse_tempest_packet(make_obs_packet())
    assert result == {"ts": 1700000000, "t": 21.4, "h": 55, "p": 1012.2, "w": 3.6, "d": 250, "r": 0.0}


def test_packet_parsing_malformed_handling():
    assert parse_tempest_packet(b"not-json") is None
    assert parse_tempest_packet(json.dumps({"type": "rapid_wind"}).encode("utf-8")) is None
    assert parse_tempest_packet(json.dumps({"type": "obs_st", "obs": [[]]}).encode("utf-8")) is None


def test_payload_construction_compact_json():
    payload = build_weather_payload(9, {"t": 20.0, "h": 50, "p": 1000.1, "w": 2.2, "d": 180, "r": 0.0})
    assert payload == {"i": 9, "t": 20.0, "h": 50, "p": 1000.1, "w": 2.2, "d": 180, "r": 0.0}
    assert to_compact_json_line(payload) == '{"i":9,"t":20.0,"h":50,"p":1000.1,"w":2.2,"d":180,"r":0.0}\n'


def test_retry_backoff_logic():
    b = BackoffPolicy(base_delay=1, factor=2, max_delay=5)
    assert [b.next_delay(), b.next_delay(), b.next_delay(), b.next_delay()] == [1, 2, 4, 5]
    b.reset()
    assert b.next_delay() == 1


def test_state_machine_transitions():
    sm = StateMachine()
    assert sm.state == BridgeState.BOOTING
    sm.set_state(BridgeState.WIFI_CONNECTING, reason="start")
    sm.set_state(BridgeState.UDP_READY, reason="wifi_ok")
    sm.set_state(BridgeState.RECOVERING, reason="error")
    sm.set_state(BridgeState.FATAL_RESTART_PENDING, reason="fatal")
    assert sm.is_fatal()
    assert sm.transitions[-1][1] == BridgeState.FATAL_RESTART_PENDING
