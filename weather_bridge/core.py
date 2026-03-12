import json


def round_weather_fields(obs):
    return {
        "ts": int(obs[0]),
        "t": round(float(obs[7]), 1),
        "h": int(round(float(obs[8]))),
        "p": round(float(obs[6]), 1),
        "w": round(float(obs[2]), 1),
        "d": int(round(float(obs[4]))),
        "r": round(float(obs[12]), 1),
    }


def weather_values_are_sane(w):
    return (
        -50 <= w["t"] <= 60
        and 0 <= w["h"] <= 100
        and 850 <= w["p"] <= 1100
        and 0 <= w["w"] <= 100
        and 0 <= w["d"] <= 360
        and 0 <= w["r"] <= 1000
    )


def parse_tempest_packet(raw_bytes):
    """Parse raw UDP bytes into compact weather dict; return None on malformed/non-obs packet."""
    try:
        text = raw_bytes.decode("utf-8")
        packet = json.loads(text)
    except Exception:
        return None

    if packet.get("type") != "obs_st":
        return None

    obs_list = packet.get("obs")
    if not isinstance(obs_list, list) or not obs_list:
        return None

    obs = obs_list[0]
    if not isinstance(obs, list) or len(obs) < 13:
        return None

    try:
        compact = round_weather_fields(obs)
    except Exception:
        return None

    if not weather_values_are_sane(compact):
        return None
    return compact


def build_weather_payload(message_id, compact_weather):
    return {
        "i": message_id,
        "t": compact_weather["t"],
        "h": compact_weather["h"],
        "p": compact_weather["p"],
        "w": compact_weather["w"],
        "d": compact_weather["d"],
        "r": compact_weather["r"],
    }


def build_heartbeat_payload(message_id, uptime_sec, ip_addr):
    return {
        "sys": "ok",
        "i": message_id,
        "up": int(uptime_sec),
        "ip": ip_addr or "0.0.0.0",
    }


def to_compact_json_line(payload):
    return json.dumps(payload, separators=(",", ":")) + "\n"
