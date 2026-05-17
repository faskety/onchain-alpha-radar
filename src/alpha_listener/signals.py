from __future__ import annotations

import re

GENERIC_NAME_RE = re.compile(
    r"^\s*(my\s+)?(collection|token|coin|nft|contract|proxy|safeproxy|wallet|test|demo|sample|new\s+token)\s*$",
    re.IGNORECASE,
)
LOW_SIGNAL_WORD_RE = re.compile(r"\b(test|demo|mock|sample|airdrop|claim|free|presale)\b", re.IGNORECASE)
SOCIAL_AGGREGATOR_TOKEN_RE = re.compile(
    r"(scanner|screener|signal|signals|alert|alerts|tracker|radar|monitor|analyzer|bot|calls|gems|trending|whale|news|kol|alpha\s*call)",
    re.IGNORECASE,
)
SIGNAL_TWEET_RE = re.compile(
    r"\b(detected\s+paid\s+dexscreener|quick\s+(swap|buy)|approved:\s*#|check\s+chart\s+buy|fdv\s+5min|liquid:\s*\$|mc:\s*\$)\b",
    re.IGNORECASE,
)
SOCIAL_AGGREGATOR_RE = re.compile(
    r"\b(scanner|screener|signal|signals|alert|alerts|tracker|radar|monitor|analyzer|bot|calls|gems|trending|whale|news|kol|alpha\s+call)\b|雷达|掘金|监控",
    re.IGNORECASE,
)
INFRASTRUCTURE_CONTRACT_NAME_RE = re.compile(
    r"^(safeproxy|gnosissafeproxy|forwarder(v\d+)?|minimalforwarder|"
    r"beaconproxy|transparentupgradeableproxy|erc1967proxy|proxyadmin|"
    r"account|permissioneddisputegame|faultdisputegame(v\d+)?|"
    r"morphochainlinkoraclev\d+)$",
    re.IGNORECASE,
)


def meaningful_name(name: object) -> str | None:
    if not isinstance(name, str):
        return None
    cleaned = " ".join(name.strip().split())
    if len(cleaned) < 4 or GENERIC_NAME_RE.search(cleaned):
        return None
    return cleaned


def meaningful_symbol(symbol: object) -> str | None:
    if not isinstance(symbol, str):
        return None
    cleaned = symbol.strip()
    if len(cleaned) < 2 or len(cleaned) > 16:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+", cleaned):
        return None
    if cleaned.lower() in {"eth", "weth", "usdc", "usdt", "btc", "wbtc", "nft", "token", "coin"}:
        return None
    return cleaned


def low_signal_identifier(name: object, symbol: object, contract_name: object = None) -> bool:
    combined = " ".join(str(v or "") for v in (name, symbol, contract_name))
    if LOW_SIGNAL_WORD_RE.search(combined):
        return True
    return bool(name and meaningful_name(name) is None) or bool(symbol and meaningful_symbol(symbol) is None)


def social_aggregator_profile(*values: object) -> bool:
    combined = " ".join(str(value or "") for value in values)
    return bool(SOCIAL_AGGREGATOR_RE.search(combined) or SOCIAL_AGGREGATOR_TOKEN_RE.search(combined))


def social_signal_tweet(text: object) -> bool:
    return bool(SIGNAL_TWEET_RE.search(str(text or "")))


def infrastructure_contract_name(contract_name: object) -> bool:
    normalized = re.sub(r"[^A-Za-z0-9]+", "", str(contract_name or ""))
    return bool(normalized and INFRASTRUCTURE_CONTRACT_NAME_RE.fullmatch(normalized))
