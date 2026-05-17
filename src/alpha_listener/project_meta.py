from __future__ import annotations

import re
from typing import Any

from .signals import meaningful_name, meaningful_symbol

ETH_ADDRESS_RE = re.compile(r"0x[a-f0-9]{40}", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

MINER_CUES = (
    "mine",
    "mined",
    "miner",
    "miners",
    "mining",
    "mineable",
    "hash",
    "hashrate",
    "hashpower",
    "hash power",
    "proof of work",
    "proof-of-work",
    "pow",
    "proof of generation",
    "proof-of-generation",
)
STRONG_MINER_CUES = (
    "miner",
    "miners",
    "mining",
    "mineable",
    "hashrate",
    "hashpower",
    "hash power",
    "proof of work",
    "proof-of-work",
    "proof of generation",
    "proof-of-generation",
)
NFT_CUES = ("nft", "mint", "collection", "erc721", "erc1155", "opensea")
DEFI_CUES = ("swap", "dex", "liquidity", "pool", "amm", "defi", "yield", "staking", "rwa")
AI_SECURITY_CUES = ("ai agent", "api key", "api keys", "identity", "auth", "credential", "security", "key")
GAME_CUES = ("game", "guess", "lottery", "pot", "vrf", "rewards", "winner")
GAME_CORE_CUES = ("game", "guess", "lottery", "pot", "vrf", "winner")
MEME_CUES = ("meme", "mascot", "pepe", "dog", "cat", "frog", "wif")

POSITIONING = {
    "miner_coin": "Mineable or mining-themed EVM token",
    "nft_collection": "NFT collection or mint",
    "defi": "DeFi trading or liquidity protocol",
    "ai_security": "AI, identity, or security tooling",
    "game_or_lottery": "On-chain game, lottery, or reward mechanic",
    "meme_token": "Meme or culture token",
    "token": "ERC20 token project",
    "contract_app": "Application contract or protocol component",
    "infrastructure_artifact": "Infrastructure/helper contract",
    "unknown": "Unclassified on-chain project",
}


def augment_project_metadata(
    contract: dict[str, Any],
    evidence: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    result = dict(evidence or {})
    category = infer_project_category(contract, result) if force else str(result.get("project_category") or infer_project_category(contract, result))
    result["project_category"] = category
    result["project_positioning"] = POSITIONING.get(category, POSITIONING["unknown"])
    result["project_description"] = project_description(contract, result, category)
    result["miner_signal"] = category == "miner_coin"
    if category == "miner_coin":
        result["miner_signal_terms"] = matched_cues(project_text(contract, result), MINER_CUES)
    return result


def contract_miner_identity(contract: dict[str, Any]) -> bool:
    identity = contract_identity_text(contract)
    if matched_cues(identity, STRONG_MINER_CUES):
        return True
    if matched_cues(identity, ("hash token", "mining token", "miner token", "pow token")):
        return True
    symbol = str(meaningful_symbol(contract.get("symbol")) or "").lower()
    return symbol in {"hash", "pow"}


def miner_project_signal(contract: dict[str, Any], evidence: dict[str, Any]) -> bool:
    text = project_text(contract, evidence)
    identity_signal = contract_miner_identity(contract)
    strong_terms = matched_cues(text, STRONG_MINER_CUES)
    weak_terms = matched_cues(text, ("mine", "mined", "hash", "pow"))
    if not identity_signal and not strong_terms:
        return False
    if not identity_signal and not project_surface_present(evidence):
        return False
    tokenish = (
        str(contract.get("kind") or "") == "erc20"
        or meaningful_symbol(contract.get("symbol")) is not None
        or bool(matched_cues(text, ("token", "erc20", "contract")))
        or "$" in text
    )
    return tokenish and (identity_signal or bool(strong_terms) or len(weak_terms) >= 2)


def infer_project_category(contract: dict[str, Any], evidence: dict[str, Any]) -> str:
    text = project_text(contract, evidence)
    if str(evidence.get("skip_reason") or "") in {"infrastructure_contract_artifact", "liquidity_pool_artifact"}:
        return "infrastructure_artifact"
    nft_signal = nft_project_signal(contract, evidence)
    if nft_signal and not contract_miner_identity(contract):
        return "nft_collection"
    if miner_project_signal(contract, evidence):
        return "miner_coin"
    if nft_signal:
        return "nft_collection"
    if game_project_signal(contract, evidence):
        return "game_or_lottery"
    if matched_cues(text, DEFI_CUES):
        return "defi"
    if matched_cues(text, AI_SECURITY_CUES):
        return "ai_security"
    if matched_cues(text, MEME_CUES):
        return "meme_token"
    if str(contract.get("kind") or "") == "erc20":
        return "token"
    if str(contract.get("kind") or "") == "named_contract":
        return "contract_app"
    return "unknown"


def nft_project_signal(contract: dict[str, Any], evidence: dict[str, Any]) -> bool:
    if str(contract.get("kind") or "") in {"erc721", "erc1155"}:
        return True
    text = project_text(contract, evidence)
    return bool(matched_cues(text, NFT_CUES))


def game_project_signal(contract: dict[str, Any], evidence: dict[str, Any]) -> bool:
    text = project_text(contract, evidence)
    return bool(matched_cues(text, GAME_CORE_CUES))


def project_surface_present(evidence: dict[str, Any]) -> bool:
    if evidence.get("twitter_account") or evidence.get("website"):
        return True
    if evidence.get("website_check_title") or evidence.get("website_check_description"):
        return True
    return False


def project_description(contract: dict[str, Any], evidence: dict[str, Any], category: str) -> str:
    label = project_label(contract, evidence)
    source_sentence = best_source_sentence(contract, evidence, category)
    if source_sentence:
        return compact_sentence(f"{label}: {source_sentence}", 260)
    positioning = POSITIONING.get(category, POSITIONING["unknown"])
    sources = str(contract.get("observation_sources") or contract.get("discovery_source") or "").replace(",", ", ")
    if sources:
        return compact_sentence(f"{label} is categorized as {positioning.lower()} based on on-chain signals: {sources}.", 260)
    return compact_sentence(f"{label} is categorized as {positioning.lower()} from the currently available contract evidence.", 260)


def project_label(contract: dict[str, Any], evidence: dict[str, Any]) -> str:
    for value in (
        meaningful_name(contract.get("name")),
        evidence.get("twitter_name"),
        meaningful_name(contract.get("contract_name")),
        meaningful_symbol(contract.get("symbol")),
        contract.get("address"),
    ):
        text = str(value or "").strip()
        if text:
            return text[:80]
    return "Unknown project"


def best_source_sentence(contract: dict[str, Any], evidence: dict[str, Any], category: str) -> str:
    candidates: list[tuple[int, str]] = []

    def add_candidates(value: Any, source_bonus: int) -> None:
        for sentence in text_sentences(value):
            candidates.append((source_bonus, sentence))

    add_candidates(evidence.get("twitter_description"), 20)
    add_candidates(evidence.get("website_check_description"), 16)
    add_candidates(evidence.get("website_check_title"), 14)
    twitter_account = str(evidence.get("twitter_account") or "").strip().lower()
    for tweet in evidence.get("sample_tweets") or []:
        if isinstance(tweet, dict):
            screen_name = str(tweet.get("userScreenName") or "").strip().lower()
            source_bonus = 10 if twitter_account and screen_name == twitter_account else 0
            add_candidates(tweet.get("text"), source_bonus)
    if not candidates:
        return ""

    category_cues = category_keywords(category)
    identity = contract_identity_terms(contract)

    def score(item: tuple[int, str]) -> tuple[int, int]:
        source_bonus, sentence = item
        lowered = sentence.lower()
        points = source_bonus
        points += sum(4 for cue in category_cues if cue in lowered)
        points += sum(3 for term in identity if term and term in lowered)
        points += 2 if any(cue in lowered for cue in ("ethereum", "base", "bnb", "bsc", "token", "contract", "mint", "launch")) else 0
        points -= 4 if len(sentence) < 18 else 0
        return points, min(len(sentence), 240)

    best = max(candidates, key=score)
    return compact_sentence(best[1], 220)


def text_sentences(value: Any) -> list[str]:
    text = clean_text(value)
    if not text:
        return []
    parts = re.split(r"[\n\r]+|(?<=[.!?])\s+", text)
    result = []
    for part in parts:
        sentence = compact_sentence(part, 220)
        if len(sentence) >= 12:
            result.append(sentence)
    return result[:8]


def clean_text(value: Any) -> str:
    text = str(value or "")
    text = URL_RE.sub("", text)
    text = ETH_ADDRESS_RE.sub("", text)
    text = re.sub(r"@(\w+)", r"\1", text)
    text = " ".join(text.replace("\u0000", " ").split())
    return text.strip(" -:;,.")


def compact_sentence(value: Any, limit: int) -> str:
    text = clean_text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip(" ,.;:") + "..."


def project_text(contract: dict[str, Any], evidence: dict[str, Any]) -> str:
    twitter_account = str(evidence.get("twitter_account") or "").strip().lower()
    parts = [
        contract_identity_text(contract),
        str(evidence.get("twitter_name") or ""),
        str(evidence.get("twitter_description") or ""),
        str(evidence.get("website_check_title") or ""),
        str(evidence.get("website_check_description") or ""),
    ]
    for tweet in evidence.get("sample_tweets") or []:
        if isinstance(tweet, dict):
            if twitter_account:
                screen_name = str(tweet.get("userScreenName") or "").strip().lower()
                if screen_name and screen_name != twitter_account:
                    continue
            parts.append(str(tweet.get("text") or ""))
    return " ".join(parts).lower()


def contract_identity_text(contract: dict[str, Any]) -> str:
    return " ".join(
        str(contract.get(field) or "")
        for field in ("name", "symbol", "contract_name")
    ).lower()


def contract_identity_terms(contract: dict[str, Any]) -> list[str]:
    terms = []
    for value in (contract.get("name"), contract.get("symbol"), contract.get("contract_name")):
        text = str(value or "").strip().lower()
        if len(text) >= 3:
            terms.append(text)
    return terms


def matched_cues(text: str, cues: tuple[str, ...]) -> list[str]:
    lowered = str(text or "").lower()
    return [cue for cue in cues if cue_matches(lowered, cue)]


def cue_matches(lowered: str, cue: str) -> bool:
    pattern = re.escape(cue.lower())
    pattern = pattern.replace(r"\ ", r"[\s_-]+").replace(r"\-", r"[\s_-]+")
    return re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", lowered) is not None


def category_keywords(category: str) -> tuple[str, ...]:
    if category == "miner_coin":
        return MINER_CUES + ("token", "contract")
    if category == "nft_collection":
        return NFT_CUES
    if category == "defi":
        return DEFI_CUES
    if category == "ai_security":
        return AI_SECURITY_CUES
    if category == "game_or_lottery":
        return GAME_CUES
    if category == "meme_token":
        return MEME_CUES
    return ("ethereum", "base", "bnb", "bsc", "token", "contract", "protocol", "project")
