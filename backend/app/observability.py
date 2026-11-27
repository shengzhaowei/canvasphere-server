"""Structured JSON logging & zero-dependency Prometheus /metrics instrumentation."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import Request, Response


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    fmt = os.environ.get("LOG_FORMAT", "text").strip().lower()
    if fmt != "json":
        return
    root = logging.getLogger()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root.handlers = [handler]
    root.setLevel(logging.INFO)


class MetricsRegistry:
    """Thread-safe Prometheus text-format (0.0.4) HTTP metrics collector."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, int], int] = {}
        self._errors: dict[tuple[str, str], int] = {}
        self._duration_sum: dict[tuple[str, str], float] = {}
        self._duration_count: dict[tuple[str, str], int] = {}

    def reset(self) -> None:
        with self._lock:
            self._requests.clear()
            self._errors.clear()
            self._duration_sum.clear()
            self._duration_count.clear()

    def observe(self, method: str, path_template: str, status_code: int, duration_seconds: float) -> None:
        m = method.upper()
        with self._lock:
            rk = (m, path_template, int(status_code))
            self._requests[rk] = self._requests.get(rk, 0) + 1
            dk = (m, path_template)
            self._duration_sum[dk] = self._duration_sum.get(dk, 0.0) + float(duration_seconds)
            self._duration_count[dk] = self._duration_count.get(dk, 0) + 1
            if int(status_code) >= 500:
                self._errors[dk] = self._errors.get(dk, 0) + 1

    def render_prometheus(self) -> str:
        lines: list[str] = [
            "# HELP canvasphere_http_requests_total Total HTTP requests handled.",
            "# TYPE canvasphere_http_requests_total counter",
        ]
        with self._lock:
            for (method, path, status), count in sorted(self._requests.items()):
                lines.append(
                    f'canvasphere_http_requests_total{{method="{method}",path="{path}",status="{status}"}} {count}'
                )
            lines.extend([
                "# HELP canvasphere_http_errors_total Total 5xx HTTP errors.",
                "# TYPE canvasphere_http_errors_total counter",
            ])
            for (method, path), count in sorted(self._errors.items()):
                lines.append(f'canvasphere_http_errors_total{{method="{method}",path="{path}"}} {count}')
            lines.extend([
                "# HELP canvasphere_http_request_duration_seconds HTTP request duration summary.",
                "# TYPE canvasphere_http_request_duration_seconds summary",
            ])
            for (method, path), total in sorted(self._duration_sum.items()):
                cnt = self._duration_count.get((method, path), 0)
                lines.append(f'canvasphere_http_request_duration_seconds_sum{{method="{method}",path="{path}"}} {total:.6f}')
                lines.append(f'canvasphere_http_request_duration_seconds_count{{method="{method}",path="{path}"}} {cnt}')
        return "\n".join(lines) + "\n"


metrics_registry = MetricsRegistry()


async def metrics_middleware(request: Request, call_next):
    t0 = time.monotonic()
    response = await call_next(request)
    dur = time.monotonic() - t0
    route = request.scope.get("route")
    path_label = getattr(route, "path", request.url.path)
    metrics_registry.observe(request.method, path_label, response.status_code, dur)
    return response


def prometheus_response() -> Response:
    return Response(
        content=metrics_registry.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
