"""Small shared transport for platform APIs; mutations are never retried."""
from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests


def _seconds(name: str, default: float) -> float:
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _retry_delay(response: requests.Response | None, attempt: int) -> float:
    value = response.headers.get("Retry-After", "") if response is not None else ""
    try:
        delay = float(value)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            delay = (deadline - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            delay = 2 ** attempt
    if not math.isfinite(delay):
        delay = 2 ** attempt
    return min(60.0, max(0.0, delay))


def request(method: str, url: str, **kwargs) -> requests.Response:
    """Send an API request with verified TLS, finite timeouts and safe retries."""
    method = method.upper()
    if kwargs.get("verify") is False:
        raise ValueError("Platform TLS verification cannot be disabled")
    kwargs.setdefault("verify", os.environ.get("PROVIDER_CA_BUNDLE") or True)
    kwargs.setdefault("timeout", (
        _seconds("PROVIDER_CONNECT_TIMEOUT", 5),
        _seconds("PROVIDER_READ_TIMEOUT", 30),
    ))
    attempts = 3 if method in {"GET", "HEAD"} else 1
    for attempt in range(attempts):
        response = None
        try:
            response = requests.request(method, url, **kwargs)
        except requests.exceptions.SSLError:
            raise requests.RequestException("Platform TLS certificate verification failed") from None
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt + 1 == attempts:
                # Native exceptions may contain credential-bearing URLs.
                raise requests.RequestException(f"Platform {method} failed: {type(exc).__name__}") from None
        else:
            if response.status_code not in {429, 502, 503, 504} or attempt + 1 == attempts:
                return response
        delay = _retry_delay(response, attempt)
        if response is not None:
            response.close()
        time.sleep(delay)
    raise AssertionError("request retry loop exhausted")


def get(url: str, **kwargs) -> requests.Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    return request("POST", url, **kwargs)
