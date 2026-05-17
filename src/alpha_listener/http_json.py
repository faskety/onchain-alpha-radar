from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class JsonHttpError(RuntimeError):
    pass


def get_json(url: str, params: dict[str, Any], timeout: int, retries: int = 2) -> dict[str, Any]:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    full_url = f"{url}?{query}"
    return _request_json("GET", full_url, None, {}, timeout, retries)


def post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout: int = 30,
    retries: int = 2,
) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    merged_headers = {"Content-Type": "application/json"}
    if headers:
        merged_headers.update(headers)
    return _request_json("POST", url, body, merged_headers, timeout, retries)


def _request_json(
    method: str,
    url: str,
    body: bytes | None,
    headers: dict[str, str],
    timeout: int,
    retries: int,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise JsonHttpError(f"Expected JSON object from {_redact_url(url)}")
            return parsed
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, JsonHttpError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(0.6 * (attempt + 1))
    raise JsonHttpError(f"{method} {_redact_url(url)} failed: {last_error}")


def _redact_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = []
    for key, value in query:
        if key.lower() in {"apikey", "api_key", "token", "access_token", "authorization"}:
            redacted.append((key, "***"))
        else:
            redacted.append((key, value))
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(redacted)))
