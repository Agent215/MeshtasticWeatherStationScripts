from __future__ import annotations

from datetime import datetime, timezone

from parser import parse_text_payload
from storage import insert_health, insert_weather, record_ingest_event

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

samples = [
    ('{"i":17,"t":22.5,"h":52,"p":1011.4,"w":3.4,"d":230,"r":0.0}', "garden-node-1", "garden"),
    ('{"sys":"ok","i":18,"up":21600,"ip":"192.168.1.205"}', "garden-node-1", "garden"),
    ('{"i":17,"t":22.5,"h":52,"p":1011.4,"w":3.4,"d":230,"r":0.0}', "garden-node-1", "garden"),
    ('not-json', "garden-node-1", "garden"),
]

for text, source_node_id, source_name in samples:
    event = parse_text_payload(
        text=text,
        source_node_id=source_node_id,
        source_name=source_name,
        received_at_utc=now_utc(),
    )

    if event.packet_type == "weather":
        result = insert_weather(event)
        print("weather:", result, event.msg_id)
    elif event.packet_type == "health":
        insert_health(event)
        print("health: inserted", event.msg_id)
    else:
        record_ingest_event(event)
        print(event.packet_type + ":", event.reason)
