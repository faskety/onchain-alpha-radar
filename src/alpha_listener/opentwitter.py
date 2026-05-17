from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen

from .http_json import post_json

URL_RE = re.compile(r"https?://[^\s)>\]}\"']+", re.IGNORECASE)
TWITTER_HOSTS = {"twitter.com", "x.com", "mobile.twitter.com"}
SHORT_URL_HOSTS = {"t.co"}
IGNORED_URL_KEYS = {
    "profileImageUrl",
    "profileBannerUrl",
    "profile_image_url",
    "profile_banner_url",
    "profile_image_url_https",
    "profile_banner_url_https",
}
NON_OFFICIAL_WEBSITE_HOSTS = {
    "t.co",
    "shar.es",
    "telegram.me",
    "t.me",
    "discord.gg",
    "discord.com",
    "medium.com",
    "etherscan.io",
    "basescan.org",
    "bscscan.com",
    "dexscreener.com",
    "www.dextools.io",
    "dextools.io",
    "app.uniswap.org",
    "opensea.io",
    "blur.io",
    "magiceden.io",
    "coinmarketcap.com",
    "coingecko.com",
    "developer.mozilla.org",
    "graphics.stanford.edu",
    "pbs.twimg.com",
    "abs.twimg.com",
    "twimg.com",
    "w3.org",
}


@dataclass(frozen=True)
class OpenTwitterClient:
    base_url: str
    api_key: str
    timeout: int = 30

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return post_json(
            f"{self.base_url.rstrip('/')}{path}",
            payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )

    def search(self, keywords: str, max_results: int = 20, product: str = "Latest") -> dict[str, Any]:
        return self._post(
            "/open/twitter_search",
            {
                "keywords": keywords,
                "maxResults": max_results,
                "product": product,
            },
        )

    def user_info(self, username: str) -> dict[str, Any]:
        return self._post("/open/twitter_user_info", {"username": username})


def normalize_search_items(response: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    data = response.get("data")
    if isinstance(data, dict):
        for key in ("tweets", "list", "items", "data"):
            nested = data.get(key)
            if isinstance(nested, list):
                data = nested
                break
    if not isinstance(data, list):
        return []
    items = [item for item in data if isinstance(item, dict)]
    return items[:limit]


def normalize_user(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data")
    if isinstance(data, dict):
        return data
    return {}


def extract_urls_from_value(value: Any) -> list[str]:
    urls: list[str] = []
    seen_containers: set[int] = set()

    def walk(item: Any) -> None:
        if isinstance(item, str):
            urls.extend(URL_RE.findall(item))
            return
        if isinstance(item, list):
            marker = id(item)
            if marker in seen_containers:
                return
            seen_containers.add(marker)
            for nested in item:
                walk(nested)
            return
        if isinstance(item, dict):
            marker = id(item)
            if marker in seen_containers:
                return
            seen_containers.add(marker)
            for key, nested in item.items():
                if str(key) in IGNORED_URL_KEYS:
                    continue
                walk(nested)

    walk(value)
    return dedupe_urls(_strip_url(url) for url in urls)


def dedupe_urls(urls: Any) -> list[str]:
    seen = set()
    result = []
    for url in urls:
        if not isinstance(url, str) or not url:
            continue
        marker = url.lower()
        if marker in seen:
            continue
        seen.add(marker)
        result.append(url)
    return result


def choose_website(urls: list[str]) -> str | None:
    candidates = []
    for url in urls:
        host = _host(url)
        if not host or blocked_website_host(host):
            continue
        candidates.append(canonical_website_url(url))
    if not candidates:
        return None
    counts = Counter(candidates)
    return counts.most_common(1)[0][0]


def expand_short_urls(urls: list[str], timeout: int = 5) -> list[str]:
    expanded_urls = []
    for url in urls:
        expanded_urls.append(url)
        host = _host(url)
        if host not in SHORT_URL_HOSTS:
            continue
        expanded = resolve_redirect_url(url, timeout=timeout)
        if expanded:
            expanded_urls.append(expanded)
    return dedupe_urls(expanded_urls)


def resolve_redirect_url(url: str, timeout: int = 5) -> str | None:
    try:
        request = Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=timeout) as response:
            return response.geturl()
    except Exception:
        return None


def _strip_url(url: str) -> str:
    return url.rstrip(".,;:!?")


def _host(url: str) -> str | None:
    match = re.match(r"https?://([^/]+)", url.lower())
    if not match:
        return None
    return match.group(1).split("@")[-1].split(":")[0].removeprefix("www.")


def blocked_website_host(host: str) -> bool:
    normalized = host.lower().removeprefix("www.")
    if normalized in TWITTER_HOSTS or normalized.endswith(".twitter.com") or normalized.endswith(".x.com"):
        return True
    if normalized in NON_OFFICIAL_WEBSITE_HOSTS:
        return True
    return any(
        normalized == host_pattern or normalized.endswith(f".{host_pattern}")
        for host_pattern in NON_OFFICIAL_WEBSITE_HOSTS
    )


def canonical_website_url(url: str) -> str:
    cleaned = _strip_url(url)
    return cleaned.rstrip("/")
