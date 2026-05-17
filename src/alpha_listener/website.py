from __future__ import annotations

import html
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .signals import meaningful_name, meaningful_symbol

MAX_WEBSITE_BYTES = 250_000
USER_AGENT = "ether-onchain-alpha-listen/1.0"


def verify_website(url: str, label: str, names: list[str], symbols: list[str], timeout: int) -> dict[str, Any]:
    started_url = normalize_url(url)
    terms = website_match_terms(label, names, symbols)
    result: dict[str, Any] = {
        "website": started_url,
        "status": "fail",
        "http_status": None,
        "final_url": "",
        "title": "",
        "description": "",
        "twitter_links": [],
        "matched_terms": [],
        "error": "",
        "failure_kind": "",
        "fallback_url": "",
        "fallback_reason": "",
    }
    primary_error = ""
    used_fallback = False
    try:
        body, final_url, http_status, content_type = fetch_website_urllib(started_url, timeout)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        primary_error = compact_space(str(exc))
        fallback = fetch_website_powershell(started_url, timeout)
        if fallback is None:
            alternate_url = alternate_http_url(started_url)
            if not alternate_url:
                result["error"] = primary_error
                result["failure_kind"] = fetch_failure_kind(primary_error)
                return result
            alternate = fetch_website_with_fallback(alternate_url, timeout)
            if alternate is None:
                result["error"] = f"primary_fetch_failed={primary_error}; fallback_url={alternate_url}; fallback_fetch_failed"
                result["failure_kind"] = fetch_failure_kind(primary_error)
                result["fallback_url"] = alternate_url
                result["fallback_reason"] = primary_error
                return result
            body, final_url, http_status, content_type = alternate
            used_fallback = True
            result["fallback_url"] = alternate_url
            result["fallback_reason"] = primary_error
        else:
            body, final_url, http_status, content_type = fallback

    text = decode_website_body(body, content_type)
    title = extract_title(text)
    description = extract_meta_description(text)
    searchable = " ".join([started_url, final_url, title, description, strip_tags(text[:120_000])])
    matched_terms = [term for term in terms if normalized_contains(searchable, term)]
    twitter_links = extract_twitter_links(text)
    result.update(
        {
            "status": website_status(http_status, matched_terms),
            "http_status": http_status,
            "final_url": final_url,
            "title": title,
            "description": description,
            "twitter_links": twitter_links[:8],
            "matched_terms": matched_terms[:8],
            "error": website_error(http_status, primary_error if used_fallback else ""),
            "failure_kind": website_failure_kind(http_status, matched_terms),
        }
    )
    return result


def fetch_website_urllib(url: str, timeout: int) -> tuple[bytes, str, int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read(MAX_WEBSITE_BYTES)
            final_url = response.geturl()
            http_status = int(getattr(response, "status", 0) or response.getcode() or 0)
            content_type = str(response.headers.get("Content-Type") or "")
            return body, final_url, http_status, content_type
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_WEBSITE_BYTES)
        final_url = exc.geturl()
        http_status = int(exc.code or 0)
        content_type = str(exc.headers.get("Content-Type") or "")
        return body, final_url, http_status, content_type


def fetch_website_powershell(url: str, timeout: int) -> tuple[bytes, str, int, str] | None:
    if os.name != "nt":
        return None
    timeout_seconds = max(3, int(timeout or 30))
    ps_url = url.replace("'", "''")
    script = f"""
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$url = '{ps_url}'
try {{
  $response = Invoke-WebRequest -Uri $url -TimeoutSec {timeout_seconds} -MaximumRedirection 5 -UseBasicParsing
  $content = [string]$response.Content
  if ($content.Length -gt {MAX_WEBSITE_BYTES}) {{
    $content = $content.Substring(0, {MAX_WEBSITE_BYTES})
  }}
  $finalUrl = ''
  if ($response.BaseResponse -and $response.BaseResponse.ResponseUri) {{
    $finalUrl = $response.BaseResponse.ResponseUri.AbsoluteUri
  }}
  [pscustomobject]@{{
    ok = $true
    statusCode = [int]$response.StatusCode
    finalUrl = $finalUrl
    contentType = [string]$response.Headers['Content-Type']
    content = $content
  }} | ConvertTo-Json -Compress
}} catch {{
  [pscustomobject]@{{
    ok = $false
    error = $_.Exception.Message
  }} | ConvertTo-Json -Compress
}}
"""
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds + 10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        payload = json.loads(completed.stdout.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not payload.get("ok"):
        return None
    content = str(payload.get("content") or "").encode("utf-8", errors="replace")
    final_url = str(payload.get("finalUrl") or url)
    status = int(payload.get("statusCode") or 0)
    content_type = str(payload.get("contentType") or "text/html; charset=utf-8")
    return content, final_url, status, content_type


def fetch_website_with_fallback(url: str, timeout: int) -> tuple[bytes, str, int, str] | None:
    try:
        return fetch_website_urllib(url, timeout)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return fetch_website_powershell(url, timeout)


def alternate_http_url(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return None
    return urllib.parse.urlunsplit(parsed._replace(scheme="http"))


def fetch_failure_kind(error: str) -> str:
    text = str(error or "").lower()
    if any(token in text for token in ("ssl", "tls", "handshake", "certificate", "eof occurred")):
        return "tls_error"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    return "fetch_error"


def website_error(http_status: int, primary_error: str = "") -> str:
    parts = []
    if primary_error:
        parts.append(f"primary_fetch_failed={primary_error}")
    if http_status < 200 or http_status >= 400:
        parts.append(f"http_status={http_status}")
    return "; ".join(parts)


def normalize_url(url: str) -> str:
    text = str(url or "").strip()
    parsed = urllib.parse.urlsplit(text)
    if not parsed.scheme:
        text = "https://" + text
    return text


def website_match_terms(label: str, names: list[str], symbols: list[str]) -> list[str]:
    terms: list[str] = []
    for value in [label, *names, *symbols]:
        name = meaningful_name(value) or meaningful_symbol(value) or str(value or "").strip()
        if len(name) < 3:
            continue
        terms.append(name)
    return dedupe_keep_order(terms)


def decode_website_body(body: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset=([\w.-]+)", content_type, flags=re.I)
    encodings = [charset_match.group(1)] if charset_match else []
    encodings.extend(["utf-8", "latin-1"])
    for encoding in encodings:
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            continue
    return body.decode("utf-8", errors="replace")


def extract_title(text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
    if not match:
        return ""
    return compact_space(html.unescape(strip_tags(match.group(1))))[:240]


def extract_meta_description(text: str) -> str:
    for pattern in (
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']',
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']',
    ):
        match = re.search(pattern, text, flags=re.I | re.S)
        if match:
            return compact_space(html.unescape(match.group(1)))[:300]
    return ""


def extract_twitter_links(text: str) -> list[str]:
    links = []
    for raw_url in re.findall(r'https?://(?:www\.)?(?:x|twitter)\.com/[^\s"\'<>]+', text, flags=re.I):
        cleaned = raw_url.rstrip(").,;]\\")
        parsed = urllib.parse.urlsplit(html.unescape(cleaned))
        path_parts = [part for part in parsed.path.split("/") if part]
        if not path_parts:
            continue
        handle = path_parts[0]
        if handle.lower() in {"intent", "share", "home", "search", "i"}:
            continue
        links.append(f"https://x.com/{handle}")
    return dedupe_keep_order(links)


def website_status(http_status: int, matched_terms: list[str]) -> str:
    if http_status < 200 or http_status >= 400:
        return "fail"
    if matched_terms:
        return "ok"
    return "warn"


def website_failure_kind(http_status: int, matched_terms: list[str]) -> str:
    if http_status < 200 or http_status >= 400:
        return "http_error"
    if not matched_terms:
        return "content_mismatch"
    return ""


def normalized_contains(text: str, term: str) -> bool:
    haystack = normalize_match_text(text)
    needle = normalize_match_text(term)
    return bool(needle and needle in haystack)


def normalize_match_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text)


def compact_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def dedupe_keep_order(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
