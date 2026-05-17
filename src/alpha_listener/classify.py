from __future__ import annotations

import re
import string
from typing import Any
from urllib.parse import urlparse

from .etherscan import EtherscanClient, hex_to_int
from .opentwitter import blocked_website_host, canonical_website_url, dedupe_urls

SELECTOR_NAME = "0x06fdde03"
SELECTOR_SYMBOL = "0x95d89b41"
SELECTOR_DECIMALS = "0x313ce567"
SELECTOR_TOTAL_SUPPLY = "0x18160ddd"
SELECTOR_SUPPORTS_INTERFACE = "0x01ffc9a7"
ERC721_INTERFACE = "80ac58cd"
ERC1155_INTERFACE = "d9b67a26"
SOURCE_URL_RE = re.compile(r"https?://[^\s)>\]}\[\"'`\\]+", re.IGNORECASE)
SOURCE_URL_BLOCKED_HOSTS = {
    "api.github.com",
    "arweave.net",
    "book.getfoundry.sh",
    "consensys.net",
    "creativecommons.org",
    "developer.mozilla.org",
    "docs.ethers.io",
    "docs.metamask.io",
    "docs.openzeppelin.com",
    "docs.soliditylang.org",
    "eips.ethereum.org",
    "ethereum.org",
    "ethereum.github.io",
    "foundry-rs.github.io",
    "forum.openzeppelin.com",
    "forum.zeppelin.solutions",
    "graphics.stanford.edu",
    "gist.github.com",
    "gitbook.io",
    "github.com",
    "hardhat.org",
    "ipfs.io",
    "nodejs.org",
    "npmjs.com",
    "open.spotify.com",
    "openzeppelin.com",
    "opensource.org",
    "raw.githubusercontent.com",
    "readthedocs.io",
    "soliditylang.org",
    "spdx.org",
    "stackexchange.com",
    "unlicense.org",
    "w3.org",
    "wikipedia.org",
    "xn--2-umb.com",
    "yarnpkg.com",
    "zeppelin.solutions",
}


def decode_string_result(raw: str | None) -> str | None:
    if not raw or raw == "0x":
        return None
    hex_body = raw[2:]
    try:
        if len(hex_body) >= 128:
            offset = int(hex_body[:64], 16)
            if offset == 32:
                length = int(hex_body[64:128], 16)
                text_hex = hex_body[128 : 128 + length * 2]
                return _clean_text(bytes.fromhex(text_hex).decode("utf-8", errors="replace"))
        if len(hex_body) >= 64:
            return _clean_text(bytes.fromhex(hex_body[:64]).decode("utf-8", errors="replace").rstrip("\x00"))
    except ValueError:
        return None
    return None


def decode_uint_result(raw: str | None) -> int | None:
    if not raw or raw == "0x":
        return None
    try:
        return int(raw, 16)
    except ValueError:
        return None


def decode_bool_result(raw: str | None) -> bool:
    value = decode_uint_result(raw)
    return value == 1


def supports_interface_data(interface_id: str) -> str:
    normalized = interface_id.lower().removeprefix("0x")
    if len(normalized) != 8 or any(ch not in string.hexdigits for ch in normalized):
        raise ValueError(f"invalid ERC165 interface id: {interface_id}")
    # ABI bytes4 arguments are left-aligned in their 32-byte slot.
    return SELECTOR_SUPPORTS_INTERFACE + normalized.ljust(64, "0")


def classify_contract(client: EtherscanClient, address: str) -> dict[str, Any]:
    name = decode_string_result(client.eth_call(address, SELECTOR_NAME))
    symbol = decode_string_result(client.eth_call(address, SELECTOR_SYMBOL))
    decimals = decode_uint_result(client.eth_call(address, SELECTOR_DECIMALS))
    total_supply = decode_uint_result(client.eth_call(address, SELECTOR_TOTAL_SUPPLY))
    is_erc721 = decode_bool_result(client.eth_call(address, supports_interface_data(ERC721_INTERFACE)))
    is_erc1155 = decode_bool_result(client.eth_call(address, supports_interface_data(ERC1155_INTERFACE)))

    source = client.get_source_code(address) or {}
    source_code = source.get("SourceCode") or ""
    contract_name = source.get("ContractName") or ""
    abi = source.get("ABI") or ""
    verified = bool(source_code) and abi != "Contract source code not verified"

    kind = "contract"
    if is_liquidity_pool_artifact(name, symbol, contract_name):
        kind = "liquidity_pool"
    elif is_erc721:
        kind = "erc721"
    elif is_erc1155:
        kind = "erc1155"
    elif name and symbol and decimals is not None and total_supply is not None:
        kind = "erc20"
    elif name and symbol:
        kind = "named_contract"

    confidence = 0.25
    if kind in {"erc20", "erc721", "erc1155"}:
        confidence += 0.35
    if verified:
        confidence += 0.2
    if name or contract_name:
        confidence += 0.1
    if symbol:
        confidence += 0.1

    return {
        "kind": kind,
        "name": name,
        "symbol": symbol,
        "decimals": decimals if decimals is not None and decimals <= 255 else None,
        "total_supply": str(total_supply) if total_supply is not None else None,
        "verified": verified,
        "contract_name": contract_name,
        "source_len": len(source_code) if isinstance(source_code, str) else 0,
        "confidence": round(min(confidence, 1.0), 3),
        "source": {
            "compiler": source.get("CompilerVersion") or "",
            "optimization_used": source.get("OptimizationUsed") or "",
            "license": source.get("LicenseType") or "",
            "urls": source_url_candidates(source_code),
        },
    }


def is_liquidity_pool_artifact(name: str | None, symbol: str | None, contract_name: str | None = None) -> bool:
    normalized_name = (name or "").strip().lower()
    normalized_symbol = (symbol or "").strip().lower()
    normalized_contract = (contract_name or "").strip().lower()
    if normalized_name == "uniswap v2" and normalized_symbol == "uni-v2":
        return True
    if normalized_contract in {"uniswapv2pair", "uniswap v2 pair"}:
        return True
    return False


def source_url_candidates(source_code: Any) -> list[str]:
    if not isinstance(source_code, str) or not source_code:
        return []
    candidates = []
    for raw_url in SOURCE_URL_RE.findall(source_code):
        url = canonical_website_url(raw_url.rstrip(".,;:!?`\\"))
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if parsed.scheme.lower() not in {"http", "https"} or not host:
            continue
        if blocked_website_host(host) or source_url_blocked_host(host):
            continue
        candidates.append(url)
    return dedupe_urls(candidates)[:10]


def source_url_blocked_host(host: str) -> bool:
    normalized = host.lower().removeprefix("www.")
    return any(
        normalized == blocked_host or normalized.endswith(f".{blocked_host}")
        for blocked_host in SOURCE_URL_BLOCKED_HOSTS
    )


def _clean_text(value: str) -> str | None:
    value = value.replace("\x00", "").strip()
    if not value:
        return None
    printable = set(string.printable)
    ascii_ratio = sum(1 for ch in value if ch in printable) / max(len(value), 1)
    if ascii_ratio < 0.55:
        return None
    return value[:160]
