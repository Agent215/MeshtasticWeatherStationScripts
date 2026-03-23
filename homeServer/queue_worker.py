from __future__ import annotations

import json
import os
import signal
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request

from storage import fetch_pending_deliveries, mark_delivery_failure, mark_delivery_success

RUNNING = True
POLL_INTERVAL_SEC = 5
HTTP_TIMEOUT_SEC = 15
MAX_POST_ATTEMPTS = 5
INITIAL_RETRY_DELAY_SEC = 2.0
MAX_RETRY_DELAY_SEC = 30.0
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class ApiConfig:
    api_url: str
    api_key: str


class RetryableDeliveryError(Exception):
    pass


class NonRetryableDeliveryError(Exception):
    pass


class SystemdNotifier:
    """
    Minimal native systemd notify support without external packages.
    Works only when the service uses Type=notify and NOTIFY_SOCKET is set.
    """

    def __init__(self) -> None:
        self.notify_socket = os.environ.get("NOTIFY_SOCKET", "")
        self.watchdog_usec = int(os.environ.get("WATCHDOG_USEC", "0") or "0")
        self.watchdog_interval_sec = self.watchdog_usec / 1_000_000 if self.watchdog_usec > 0 else 0.0
        self._last_watchdog_sent = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.notify_socket)

    def _send(self, message: str) -> None:
        if not self.notify_socket:
            return

        addr = self.notify_socket
        if addr.startswith("@"):
            # Abstract namespace socket
            addr = "\0" + addr[1:]

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(addr)
            sock.sendall(message.encode("utf-8"))
        except Exception:
            # Never crash the app just because systemd notification failed.
            pass
        finally:
            sock.close()

    def ready(self, status: str | None = None) -> None:
        parts = ["READY=1"]
        if status:
            parts.append(f"STATUS={status}")
        self._send("\n".join(parts))

    def status(self, status: str) -> None:
        self._send(f"STATUS={status}")

    def stopping(self, status: str | None = None) -> None:
        parts = ["STOPPING=1"]
        if status:
            parts.append(f"STATUS={status}")
        self._send("\n".join(parts))

    def watchdog_ping_if_due(self) -> None:
        if self.watchdog_interval_sec <= 0:
            return

        # Send a ping at about half the configured watchdog interval.
        now = time.monotonic()
        interval = max(1.0, self.watchdog_interval_sec / 2.0)
        if now - self._last_watchdog_sent >= interval:
            self._send("WATCHDOG=1")
            self._last_watchdog_sent = now


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def normalize_utc_z(value: str | None) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.endswith("Z"):
        return text

    if text.endswith("+00:00"):
        return text[:-6] + "Z"

    return text

def log_event(event: str, **fields: Any) -> None:
    record = {
        "ts": utc_now(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, ensure_ascii=False), flush=True)


def handle_signal(signum: int, frame: Any) -> None:
    global RUNNING
    RUNNING = False
    log_event("shutdown_requested", signal=signum)


def load_dotenv_file(env_path: Path) -> None:
    if not env_path.is_file():
        return

    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        key, separator, value = line.partition("=")
        if not separator:
            raise RuntimeError(f"Invalid .env entry at line {line_number}: expected KEY=VALUE")

        key = key.removeprefix("export ").strip()
        if not key:
            raise RuntimeError(f"Invalid .env entry at line {line_number}: missing key")

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        os.environ.setdefault(key, value)


def get_required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    raise RuntimeError(f"Missing required setting: {name}")


def load_api_config() -> ApiConfig:
    load_dotenv_file(ENV_PATH)
    return ApiConfig(
        api_url=get_required_env("API_URL"),
        api_key=get_required_env("API_KEY"),
    )


def drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def build_weather_payload(row: dict[str, Any]) -> dict[str, Any]:
    weather = {
        "air_temp_c": row.get("temp_c"),
        "relative_humidity_pct": row.get("humidity_pct"),
        "station_pressure_hpa": row.get("pressure_hpa"),
        "wind_avg_ms": row.get("wind_ms"),
        "wind_dir_deg": row.get("wind_dir_deg"),
        "rain_interval_mm": row.get("rain_mm"),
        "wind_lull_ms": row.get("wind_lull_ms"),
        "wind_gust_ms": row.get("wind_gust_ms"),
        "wind_sample_interval_s": row.get("wind_sample_interval_s"),
        "illuminance_lux": row.get("illuminance_lux"),
        "uv_index": row.get("uv_index"),
        "solar_radiation_wm2": row.get("solar_radiation_wm2"),
        "precipitation_type": row.get("precipitation_type"),
        "lightning_avg_distance_km": row.get("lightning_avg_distance_km"),
        "lightning_strike_count": row.get("lightning_strike_count"),
        "battery_voltage_v": row.get("battery_voltage_v"),
        "report_interval_min": row.get("report_interval_min"),
        "local_day_rain_mm": row.get("local_day_rain_mm"),
        "nearcast_rain_mm": row.get("nearcast_rain_mm"),
        "local_day_nearcast_rain_mm": row.get("local_day_nearcast_rain_mm"),
        "precipitation_analysis_type": row.get("precipitation_analysis_type"),
        "timestamp": row.get("weather_timestamp"),
    }
    return drop_none(weather)


def build_api_request_body(row: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "source_node_id": row["source_node_id"],
        "source_name": row.get("source_name"),
        "msg_id": row["msg_id"],
        "source_ts_utc": row.get("source_ts_utc"),
        "received_at_utc": normalize_utc_z(row.get("received_at_utc")),
        "weather": build_weather_payload(row),
    }
    return {"payload": drop_none(payload)}


def classify_http_error(status_code: int, response_text: str) -> Exception:
    message = f"AWS API HTTP {status_code}: {response_text}"
    if status_code in RETRYABLE_HTTP_STATUS_CODES:
        return RetryableDeliveryError(message)
    return NonRetryableDeliveryError(message)


def post_to_aws(body: dict[str, Any], config: ApiConfig) -> dict[str, Any]:
    url = config.api_url.rstrip("/") + "/observations"
    raw_body = json.dumps(body).encode("utf-8")

    req = request.Request(
        url=url,
        data=raw_body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-weatherstation-key": config.api_key,
        },
    )

    try:
        with request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            response_text = resp.read().decode("utf-8")
            status_code = resp.getcode()

            if not response_text:
                return {"status_code": status_code, "body": None}

            try:
                parsed = json.loads(response_text)
            except json.JSONDecodeError:
                parsed = {"raw_body": response_text}

            return {
                "status_code": status_code,
                "body": parsed,
            }

    except error.HTTPError as exc:
        response_text = exc.read().decode("utf-8", errors="replace")
        raise classify_http_error(exc.code, response_text) from exc
    except error.URLError as exc:
        raise RetryableDeliveryError(f"AWS API network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RetryableDeliveryError("AWS API timeout") from exc


def compute_retry_delay(attempt_number: int) -> float:
    delay = INITIAL_RETRY_DELAY_SEC * (2 ** (attempt_number - 1))
    return min(delay, MAX_RETRY_DELAY_SEC)


def post_to_aws_with_retry(
    body: dict[str, Any],
    config: ApiConfig,
    *,
    queue_id: int,
    reading_id: int,
    msg_id: int,
    source_node_id: str,
) -> dict[str, Any]:
    last_error: Exception | None = None

    for attempt in range(1, MAX_POST_ATTEMPTS + 1):
        if not RUNNING:
            raise RuntimeError("Shutdown requested")

        try:
            result = post_to_aws(body, config)

            if attempt > 1:
                log_event(
                    "delivery_retry_recovered",
                    queue_id=queue_id,
                    reading_id=reading_id,
                    msg_id=msg_id,
                    source_node_id=source_node_id,
                    attempt=attempt,
                    http_status=result.get("status_code"),
                )

            return result

        except NonRetryableDeliveryError:
            raise

        except RetryableDeliveryError as exc:
            last_error = exc

            if attempt >= MAX_POST_ATTEMPTS:
                break

            delay_sec = compute_retry_delay(attempt)
            log_event(
                "delivery_retry_scheduled",
                queue_id=queue_id,
                reading_id=reading_id,
                msg_id=msg_id,
                source_node_id=source_node_id,
                attempt=attempt,
                max_attempts=MAX_POST_ATTEMPTS,
                retry_delay_sec=delay_sec,
                error=str(exc),
            )

            deadline = time.monotonic() + delay_sec
            while RUNNING and time.monotonic() < deadline:
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    assert last_error is not None
    raise last_error


def process_one(row: dict[str, Any], config: ApiConfig) -> None:
    queue_id = row["queue_id"]
    reading_id = row["reading_id"]
    msg_id = row["msg_id"]
    source_node_id = row["source_node_id"]
    request_body = build_api_request_body(row)

    log_event(
        "delivery_attempt_started",
        queue_id=queue_id,
        reading_id=reading_id,
        msg_id=msg_id,
        source_node_id=source_node_id,
        attempt_count=row["attempt_count"],
        api_url=config.api_url,
    )

    try:
        api_result = post_to_aws_with_retry(
            request_body,
            config,
            queue_id=queue_id,
            reading_id=reading_id,
            msg_id=msg_id,
            source_node_id=source_node_id,
        )
        response_body = api_result.get("body") or {}

        mark_delivery_success(queue_id)

        log_event(
            "delivery_success",
            queue_id=queue_id,
            reading_id=reading_id,
            msg_id=msg_id,
            source_node_id=source_node_id,
            http_status=api_result.get("status_code"),
            deduped=response_body.get("deduped"),
            response=response_body,
        )

    except Exception as exc:
        mark_delivery_failure(queue_id, str(exc))
        log_event(
            "delivery_failure",
            queue_id=queue_id,
            reading_id=reading_id,
            msg_id=msg_id,
            source_node_id=source_node_id,
            error=str(exc),
        )


def main() -> int:
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    notifier = SystemdNotifier()

    try:
        api_config = load_api_config()
    except Exception as exc:
        log_event("queue_worker_config_error", error=str(exc), env_path=str(ENV_PATH))
        notifier.status(f"config error: {exc}")
        return 1

    log_event("queue_worker_start", api_url=api_config.api_url, env_path=str(ENV_PATH))
    notifier.ready("weather queue worker started")

    while RUNNING:
        try:
            notifier.watchdog_ping_if_due()
            notifier.status("polling for pending deliveries")

            rows = fetch_pending_deliveries(limit=10)

            if not rows:
                deadline = time.monotonic() + POLL_INTERVAL_SEC
                while RUNNING and time.monotonic() < deadline:
                    notifier.watchdog_ping_if_due()
                    time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
                continue

            notifier.status(f"processing {len(rows)} pending deliveries")

            for row in rows:
                if not RUNNING:
                    break

                notifier.watchdog_ping_if_due()
                process_one(dict(row), api_config)

        except Exception as exc:
            log_event("queue_worker_error", error=str(exc))
            notifier.status(f"worker error: {exc}")

            deadline = time.monotonic() + POLL_INTERVAL_SEC
            while RUNNING and time.monotonic() < deadline:
                notifier.watchdog_ping_if_due()
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    notifier.stopping("queue worker stopping")
    log_event("queue_worker_stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())