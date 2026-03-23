from db import get_conn

with get_conn() as conn:
    print("\nLatest weather:")
    for row in conn.execute("""
        SELECT id, source_node_id, source_name, msg_id, temp_c, humidity_pct,
               pressure_hpa, wind_ms, wind_dir_deg, rain_mm, received_at_utc
        FROM weather_readings
        ORDER BY id DESC
        LIMIT 5
    """):
        print(dict(row))

    print("\nLatest health:")
    for row in conn.execute("""
        SELECT id, source_node_id, source_name, status, uptime_sec, ip_address, received_at_utc
        FROM device_health_events
        ORDER BY id DESC
        LIMIT 5
    """):
        print(dict(row))

    print("\nLatest queue:")
    for row in conn.execute("""
        SELECT id, reading_id, status, attempt_count, next_attempt_at_utc, last_error
        FROM aws_delivery_queue
        ORDER BY id DESC
        LIMIT 10
    """):
        print(dict(row))
