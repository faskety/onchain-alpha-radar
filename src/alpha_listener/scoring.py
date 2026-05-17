from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import urlparse

from .chains import chain_context_in_text
from .signals import low_signal_identifier, meaningful_name, meaningful_symbol

ETH_ADDRESS_RE = re.compile(r"0x[a-f0-9]{40}", re.IGNORECASE)
SOLANA_PUMP_ADDRESS_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{28,48}pump\b", re.IGNORECASE)
GENERIC_TOKEN_FACTORY_HOSTS = {
    "eth-token.com",
}


def score_project(contract: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    breakdown: dict[str, int] = {}

    sources = observation_sources(contract)
    provenance = 0
    if "contract_creation" in sources:
        provenance += 8
    if "internal_create" in sources or "internal_create2" in sources:
        provenance += 8
    if any(dex_liquidity_source(source) for source in sources):
        provenance += 7
    if "uniswap_v4_hook" in sources:
        provenance += 4
    if any("mint" in source for source in sources):
        provenance += 5
    observation_count = int(contract.get("observation_count") or 0)
    if observation_count > 1:
        provenance += min(4, observation_count - 1)
    breakdown["onchain_provenance"] = min(provenance, 14)

    kind = contract.get("kind")
    if kind == "liquidity_pool":
        breakdown["project_surface"] = 2
    elif kind == "erc20":
        breakdown["project_surface"] = 20
    elif kind in {"erc721", "erc1155"}:
        breakdown["project_surface"] = 18
    elif kind == "named_contract":
        breakdown["project_surface"] = 12
    else:
        breakdown["project_surface"] = 6

    technical = 4
    if contract.get("verified"):
        technical += 14
    if contract.get("contract_name"):
        technical += 4
    if contract.get("source_len", 0) and contract.get("source_len", 0) > 2000:
        technical += 4
    breakdown["technical_readability"] = min(technical, 22)

    identity = 0
    if meaningful_name(contract.get("name")):
        identity += 8
    if meaningful_symbol(contract.get("symbol")):
        identity += 5
    if evidence.get("twitter_account"):
        identity += 10
    if website_live(evidence):
        identity += 7
    breakdown["identity_resolution"] = min(identity, 30)

    social = 0
    tweet_count = int(evidence.get("tweet_count") or 0)
    address_mentions = int(evidence.get("address_mentions") or 0)
    credible_address_mentions = evidence.get("credible_address_mentions")
    if credible_address_mentions is None:
        credible_address_mentions = address_mentions
    credible_address_mentions = int(credible_address_mentions or 0)
    if credible_address_mentions > 0:
        social += min(10, credible_address_mentions * 3)
        social += 4
    elif address_mentions > 0:
        social += min(3, address_mentions)
    elif evidence.get("twitter_account"):
        social += min(8, tweet_count)
    elif tweet_count > 0:
        social += min(4, tweet_count)
    followers = int(evidence.get("twitter_followers") or 0)
    if followers > 0 and evidence.get("twitter_account"):
        social += min(12, int(math.log10(followers + 1) * 3))
    if evidence.get("twitter_verified") and evidence.get("twitter_account"):
        social += 4
    breakdown["social_signal"] = min(social, 26)

    penalties = 0
    if low_signal_identifier(contract.get("name"), contract.get("symbol"), contract.get("contract_name")):
        penalties += 10
    if not contract.get("verified"):
        penalties += 4
    if not evidence.get("twitter_account") and not evidence.get("website"):
        penalties += 10
    if website_check_failed(evidence):
        penalties += 10
    if source_verified_website_from_generic_token_factory(evidence):
        penalties += 10
    if third_party_profile_identity_only(evidence):
        penalties += 12
    if twitter_only_foreign_chain_conflict(contract, evidence):
        penalties += 12
    if evidence.get("discussion_only") and address_mentions == 0:
        penalties += 12
    if kind == "contract" and not contract.get("contract_name"):
        penalties += 6
    if kind == "liquidity_pool":
        penalties += 18
    breakdown["risk_penalty"] = -min(penalties, 24)

    total = max(0, min(100, sum(breakdown.values())))
    official_resolved = bool(evidence.get("twitter_account") or website_live(evidence))
    tier = "watch"
    if total >= 70 and official_resolved and high_tier_support(contract, evidence, sources, observation_count):
        tier = "high"
    elif total >= 50 and official_resolved and medium_tier_support(contract, evidence, sources, observation_count):
        tier = "medium"
    elif total < 30:
        tier = "low"

    return {"score": total, "tier": tier, "breakdown": breakdown}


def observation_sources(contract: dict[str, Any]) -> set[str]:
    sources = set()
    raw_sources = contract.get("observation_sources")
    if isinstance(raw_sources, str):
        sources.update(source.strip() for source in raw_sources.split(",") if source.strip())
    source = contract.get("discovery_source")
    if isinstance(source, str) and source:
        sources.add(source)
    return sources


def high_tier_support(
    contract: dict[str, Any],
    evidence: dict[str, Any],
    sources: set[str],
    observation_count: int,
) -> bool:
    reason = str(evidence.get("official_identity_reason") or "")
    has_strong_onchain = strong_onchain_support(sources, observation_count)
    followers = int(evidence.get("twitter_followers") or 0)
    if twitter_only_foreign_chain_conflict(contract, evidence):
        return False
    if website_live(evidence):
        if source_verified_website_from_generic_token_factory(evidence):
            return False
        if source_verified_website_with_unresolved_discussion(evidence):
            return False
        return True
    if int(evidence.get("credible_address_mentions") or evidence.get("address_mentions") or 0) > 0:
        return True
    if reason == "profile_project_identity":
        if third_party_profile_identity_only(evidence):
            return False
        return has_strong_onchain
    if reason == "account_crypto_project_context":
        return has_strong_onchain and account_crypto_context_has_high_support(evidence, followers)
    if reason == "profile_crypto_identity":
        return has_strong_onchain and followers >= 500
    if evidence.get("twitter_verified") and has_strong_onchain:
        return True
    if followers >= 500 and has_strong_onchain:
        return True
    if contract.get("kind") in {"erc721", "erc1155"} and observation_count >= 5 and reason:
        return True
    return False


def medium_tier_support(
    contract: dict[str, Any],
    evidence: dict[str, Any],
    sources: set[str],
    observation_count: int,
) -> bool:
    reason = str(evidence.get("official_identity_reason") or "")
    has_strong_onchain = strong_onchain_support(sources, observation_count)
    followers = int(evidence.get("twitter_followers") or 0)
    if twitter_only_foreign_chain_conflict(contract, evidence):
        return False
    if website_live(evidence):
        if source_verified_website_from_generic_token_factory(evidence):
            return False
        if source_verified_website_with_unresolved_discussion(evidence):
            return False
        return True
    if int(evidence.get("credible_address_mentions") or evidence.get("address_mentions") or 0) > 0:
        return True
    if reason == "profile_project_identity":
        if third_party_profile_identity_only(evidence):
            return has_strong_onchain
        return True
    if reason == "account_crypto_project_context":
        return has_strong_onchain and account_crypto_context_has_medium_support(evidence, followers)
    if reason == "profile_crypto_identity":
        return has_strong_onchain and followers >= 500
    if evidence.get("twitter_verified") and has_strong_onchain:
        return True
    if followers >= 500 and has_strong_onchain:
        return True
    return False


def strong_onchain_support(sources: set[str], observation_count: int) -> bool:
    return observation_count >= 2 and any(
        dex_liquidity_source(source) or "mint" in source or activity_source(source)
        for source in sources
    )


def account_crypto_context_has_high_support(evidence: dict[str, Any], followers: int) -> bool:
    if account_crypto_context_has_direct_support(evidence):
        return True
    return followers >= 100 or bool(evidence.get("twitter_verified"))


def account_crypto_context_has_medium_support(evidence: dict[str, Any], followers: int) -> bool:
    if account_crypto_context_has_direct_support(evidence):
        return True
    if followers >= 100 or bool(evidence.get("twitter_verified")):
        return True
    return int(evidence.get("official_account_chain_project_mentions") or evidence.get("official_account_ethereum_project_mentions") or 0) > 0


def account_crypto_context_has_direct_support(evidence: dict[str, Any]) -> bool:
    return int(evidence.get("official_account_address_mentions") or 0) > 0


def dex_liquidity_source(source: str) -> bool:
    return source.startswith("uniswap_") or source == "balancer_v2_tokens_registered"


def activity_source(source: str) -> bool:
    return source.startswith("activity_")


def website_live(evidence: dict[str, Any]) -> bool:
    return bool(evidence.get("website")) and not website_check_failed(evidence)


def website_check_failed(evidence: dict[str, Any]) -> bool:
    return str(evidence.get("website_check_status") or "").lower() == "fail"


def source_verified_website_with_unresolved_discussion(evidence: dict[str, Any]) -> bool:
    if str(evidence.get("official_website_reason") or "") != "verified_source_url":
        return False
    if evidence.get("twitter_account"):
        return False
    if not evidence.get("discussion_only"):
        return False
    return int(evidence.get("credible_address_mentions") or evidence.get("address_mentions") or 0) <= 0


def source_verified_website_from_generic_token_factory(evidence: dict[str, Any]) -> bool:
    if str(evidence.get("official_website_reason") or "") != "verified_source_url":
        return False
    website = str(evidence.get("website") or "")
    if not website:
        return False
    host = (urlparse(website).hostname or "").lower().removeprefix("www.")
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in GENERIC_TOKEN_FACTORY_HOSTS)


def twitter_only_foreign_chain_conflict(contract: dict[str, Any], evidence: dict[str, Any]) -> bool:
    if not evidence.get("twitter_account") or website_live(evidence):
        return False
    if int(evidence.get("credible_address_mentions") or evidence.get("address_mentions") or 0) > 0:
        return False
    foreign_mentions = max(
        int(evidence.get("foreign_chain_project_mentions") or 0),
        inferred_project_context_mentions(contract, evidence, foreign_chain_context_in_text),
    )
    if foreign_mentions < 2:
        return False
    chain_mentions = max(
        int(evidence.get("chain_project_mentions") or evidence.get("ethereum_project_mentions") or 0),
        int(evidence.get("official_account_chain_project_mentions") or evidence.get("official_account_ethereum_project_mentions") or 0),
        inferred_project_context_mentions(
            contract,
            evidence,
            lambda text: chain_context_in_text(text, contract.get("chain_slug") or contract.get("chain")),
        ),
    )
    return foreign_mentions > chain_mentions


def third_party_profile_identity_only(evidence: dict[str, Any]) -> bool:
    if str(evidence.get("official_identity_reason") or "") != "profile_project_identity":
        return False
    if not evidence.get("twitter_account") or evidence.get("website"):
        return False
    if int(evidence.get("credible_address_mentions") or evidence.get("address_mentions") or 0) > 0:
        return False
    context = official_account_context(evidence)
    if not context.get("found"):
        return False
    return (
        int(context.get("address_mentions") or 0) == 0
        and int(context.get("own_project_mentions") or 0) == 0
        and int(context.get("own_crypto_project_mentions") or 0) == 0
    )


def official_account_context(evidence: dict[str, Any]) -> dict[str, Any]:
    account = str(evidence.get("twitter_account") or "").strip().lstrip("@").lower()
    if not account:
        return {"found": False}
    top_level_keys = {
        "address_mentions": "official_account_address_mentions",
        "project_mentions": "official_account_project_mentions",
        "own_project_mentions": "official_account_own_project_mentions",
        "own_crypto_project_mentions": "official_account_own_crypto_project_mentions",
        "chain_project_mentions": "official_account_chain_project_mentions",
        "ethereum_project_mentions": "official_account_ethereum_project_mentions",
        "foreign_chain_project_mentions": "official_account_foreign_chain_project_mentions",
        "flags": "official_account_flags",
    }
    if any(key in evidence for key in top_level_keys.values()):
        return {
            "found": True,
            **{field: evidence.get(key) for field, key in top_level_keys.items()},
        }
    for author in evidence.get("top_authors") or []:
        if not isinstance(author, dict):
            continue
        if str(author.get("username") or "").strip().lstrip("@").lower() != account:
            continue
        return {
            "found": True,
            "address_mentions": author.get("address_mentions"),
            "project_mentions": author.get("project_mentions"),
            "own_project_mentions": author.get("own_project_mentions"),
            "own_crypto_project_mentions": author.get("own_crypto_project_mentions"),
            "chain_project_mentions": author.get("chain_project_mentions"),
            "ethereum_project_mentions": author.get("ethereum_project_mentions"),
            "foreign_chain_project_mentions": author.get("foreign_chain_project_mentions"),
            "flags": author.get("flags"),
        }
    return {"found": False}


def inferred_project_context_mentions(
    contract: dict[str, Any],
    evidence: dict[str, Any],
    context_predicate,
) -> int:
    count = 0
    seen_texts: set[str] = set()
    for text in evidence_texts(evidence):
        normalized = " ".join(text.lower().split())
        if not normalized or normalized in seen_texts:
            continue
        seen_texts.add(normalized)
        if project_context_in_text(text, contract) and context_predicate(text):
            count += 1
    return count


def evidence_texts(evidence: dict[str, Any]) -> list[str]:
    texts = []
    for tweet in evidence.get("sample_tweets") or []:
        if isinstance(tweet, dict):
            texts.append(str(tweet.get("text") or ""))
    for author in evidence.get("top_authors") or []:
        if not isinstance(author, dict):
            continue
        example = author.get("example")
        if isinstance(example, dict):
            texts.append(str(example.get("text") or ""))
    return texts


def project_context_in_text(text: str, contract: dict[str, Any]) -> bool:
    lowered = str(text or "").lower()
    name = str(meaningful_name(contract.get("name")) or "").lower()
    symbol = str(meaningful_symbol(contract.get("symbol")) or "").lower()
    if name and name in lowered:
        return True
    if symbol and (f"${symbol}" in lowered or f" {symbol} " in f" {lowered} "):
        return True
    return False


def ethereum_context_in_text(text: str) -> bool:
    return chain_context_in_text(text, "ethereum")


def foreign_chain_context_in_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return bool(
        SOLANA_PUMP_ADDRESS_RE.search(lowered)
        or any(
            cue in lowered
            for cue in (
                "solana",
                "#solanatokens",
                "pump.fun",
                "pumpfun",
                "moonshot",
                "raydium",
                "jupiter",
                "dexscreener.com/solana",
                "birdeye.so",
                "meteora",
                "letsbonk",
            )
        )
    )
