from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ChainProfile:
    slug: str
    display_name: str
    chainid: str
    explorer_base_url: str
    data_dir_name: str
    block_time_seconds: float
    default_confirmations: int
    default_interval_seconds: int
    default_lookback_blocks: int
    default_max_blocks_per_cycle: int
    default_coverage_window_blocks: int
    default_new_contract_max_age_blocks: int
    default_discovery_mode: str
    default_activity_min_observations: int
    default_activity_max_candidates_per_cycle: int
    default_activity_transfer_log_span: int
    search_term: str
    env_prefixes: tuple[str, ...]
    context_cues: tuple[str, ...]
    context_regexes: tuple[str, ...] = ()


ETH_ADDRESS_RE = re.compile(r"0x[a-f0-9]{40}", re.IGNORECASE)

CHAIN_PROFILES: dict[str, ChainProfile] = {
    "ethereum": ChainProfile(
        slug="ethereum",
        display_name="Ethereum",
        chainid="1",
        explorer_base_url="https://etherscan.io",
        data_dir_name="data",
        block_time_seconds=12.0,
        default_confirmations=6,
        default_interval_seconds=1800,
        default_lookback_blocks=300,
        default_max_blocks_per_cycle=300,
        default_coverage_window_blocks=600,
        default_new_contract_max_age_blocks=7200,
        default_discovery_mode="activity",
        default_activity_min_observations=100,
        default_activity_max_candidates_per_cycle=50,
        default_activity_transfer_log_span=50,
        search_term="ethereum",
        env_prefixes=("ETHEREUM", "ETH"),
        context_cues=(
            "ethereum",
            "uniswap",
            "etherscan",
            "dextools.io/app/en/ether",
            "dexscreener.com/ethereum",
            "erc20",
            "erc721",
            "erc1155",
            "weth",
        ),
        context_regexes=(r"\beth\b",),
    ),
    "base": ChainProfile(
        slug="base",
        display_name="Base",
        chainid="8453",
        explorer_base_url="https://basescan.org",
        data_dir_name="data/base",
        block_time_seconds=2.0,
        default_confirmations=15,
        default_interval_seconds=1800,
        default_lookback_blocks=1200,
        default_max_blocks_per_cycle=300,
        default_coverage_window_blocks=1200,
        default_new_contract_max_age_blocks=43200,
        default_discovery_mode="activity",
        default_activity_min_observations=100,
        default_activity_max_candidates_per_cycle=50,
        default_activity_transfer_log_span=100,
        search_term="base",
        env_prefixes=("BASE",),
        context_cues=(
            "base chain",
            "on base",
            "basescan",
            "base.org",
            "aerodrome",
            "dexscreener.com/base",
            "dextools.io/app/en/base",
            "erc20",
            "erc721",
            "erc1155",
        ),
        context_regexes=(r"\bbase\b",),
    ),
    "bsc": ChainProfile(
        slug="bsc",
        display_name="BNB Smart Chain",
        chainid="56",
        explorer_base_url="https://bscscan.com",
        data_dir_name="data/bsc",
        block_time_seconds=0.45,
        default_confirmations=64,
        default_interval_seconds=1800,
        default_lookback_blocks=5000,
        default_max_blocks_per_cycle=5000,
        default_coverage_window_blocks=10000,
        default_new_contract_max_age_blocks=192000,
        default_discovery_mode="activity",
        default_activity_min_observations=100,
        default_activity_max_candidates_per_cycle=75,
        default_activity_transfer_log_span=100,
        search_term="bnb chain",
        env_prefixes=("BSC", "BNB"),
        context_cues=(
            "bnb chain",
            "binance smart chain",
            "bscscan",
            "pancakeswap",
            "dexscreener.com/bsc",
            "dextools.io/app/en/bnb",
            "dextools.io/app/en/bsc",
            "wBNB",
            "erc20",
            "erc721",
            "erc1155",
        ),
        context_regexes=(r"\bbsc\b", r"\bbnb\b"),
    ),
}

CHAIN_ALIASES = {
    "1": "ethereum",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "mainnet": "ethereum",
    "8453": "base",
    "base": "base",
    "56": "bsc",
    "bnb": "bsc",
    "bnbchain": "bsc",
    "bsc": "bsc",
    "binance": "bsc",
}

SUPPORTED_CHAIN_CHOICES = tuple(sorted(CHAIN_ALIASES))


def normalize_chain_slug(value: str | None) -> str:
    raw = str(value or "ethereum").strip().lower().replace("-", "").replace("_", "")
    slug = CHAIN_ALIASES.get(raw)
    if not slug:
        choices = ", ".join(sorted({"ethereum", "base", "bsc", "bnb"}))
        raise ValueError(f"Unsupported chain '{value}'. Supported chains: {choices}")
    return slug


def chain_profile(value: str | None) -> ChainProfile:
    return CHAIN_PROFILES[normalize_chain_slug(value)]


def chain_context_in_text(text: object, chain_slug: str | None = None) -> bool:
    lowered = str(text or "").lower()
    if ETH_ADDRESS_RE.search(lowered):
        return True
    profile = chain_profile(chain_slug)
    if any(cue.lower() in lowered for cue in profile.context_cues):
        return True
    return any(re.search(pattern, lowered) for pattern in profile.context_regexes)


def chain_search_term(chain_slug: str | None = None) -> str:
    return chain_profile(chain_slug).search_term
