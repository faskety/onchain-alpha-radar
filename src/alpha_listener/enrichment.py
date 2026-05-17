from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any
from urllib.parse import urlparse

from .chains import chain_context_in_text, chain_search_term
from .opentwitter import (
    OpenTwitterClient,
    choose_website,
    dedupe_urls,
    expand_short_urls,
    extract_urls_from_value,
    normalize_search_items,
    normalize_user,
)
from .project_meta import augment_project_metadata, contract_miner_identity, miner_project_signal
from .scoring import score_project
from .signals import meaningful_name, meaningful_symbol, social_aggregator_profile, social_signal_tweet

MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{1,15})")
ETH_ADDRESS_RE = re.compile(r"0x[a-f0-9]{40}")
SOLANA_PUMP_ADDRESS_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{28,48}pump\b", re.IGNORECASE)
GENERIC_HOSTED_SITE_SUFFIXES = {
    "github.io",
    "netlify.app",
    "pages.dev",
    "vercel.app",
}
GENERIC_TOKEN_FACTORY_HOSTS = {
    "eth-token.com",
}


def build_queries(contract: dict[str, Any]) -> list[str]:
    address = contract["address"]
    name = meaningful_name(contract.get("name")) or meaningful_name(contract.get("contract_name")) or ""
    symbol = meaningful_symbol(contract.get("symbol")) or ""
    chain_term = chain_search_term(contract.get("chain_slug") or contract.get("chain"))
    queries = [address]
    if name and symbol:
        queries.append(f"{name} {symbol}")
        queries.append(f"{name} official")
    elif name:
        queries.append(name)
        queries.append(f"{name} {chain_term}")
    elif symbol:
        queries.append(f"${symbol}")
        queries.append(f"{symbol} {chain_term}")
    if contract_miner_identity(contract):
        if name and symbol:
            queries.append(f"{name} {symbol} mining")
            queries.append(f"${symbol} miner")
        elif name:
            queries.append(f"{name} mining")
            queries.append(f"{name} miner")
        elif symbol:
            queries.append(f"${symbol} mining")
            queries.append(f"${symbol} miner")
    return dedupe_queries(queries)[:6 if contract_miner_identity(contract) else 4]


def should_run_deep_project_queries(contract: dict[str, Any], evidence: dict[str, Any]) -> bool:
    return contract_miner_identity(contract) or miner_project_signal(contract, evidence)


def deep_project_queries(contract: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    name = meaningful_name(contract.get("name")) or meaningful_name(contract.get("contract_name")) or ""
    symbol = meaningful_symbol(contract.get("symbol")) or ""
    account = str(evidence.get("twitter_account") or "").strip().lstrip("@")
    seeds = []
    if name:
        seeds.extend([f"{name} mining", f"{name} miner", f"{name} official"])
    if symbol:
        seeds.extend([f"${symbol} mining", f"${symbol} miner", f"${symbol} official"])
    if account:
        if name:
            seeds.append(f"from:{account} {name}")
        if symbol:
            seeds.append(f"from:{account} ${symbol}")
        seeds.extend([f"from:{account} mining", f"from:{account} miner", f"from:{account} website"])
    return dedupe_queries(seeds)[:6]


def enrich_contract(
    twitter: OpenTwitterClient,
    contract: dict[str, Any],
    max_results: int,
) -> dict[str, Any]:
    queries = build_queries(contract)
    tweets: list[dict[str, Any]] = []
    errors: list[str] = []
    for query in queries:
        try:
            response = twitter.search(query, max_results=max_results, product="Latest")
            tweets.extend(normalize_search_items(response, max_results))
        except Exception as exc:
            errors.append(f"{query}: {exc}")
    tweets = dedupe_tweets(tweets)

    evidence = build_evidence(twitter, contract, tweets)
    if should_run_deep_project_queries(contract, evidence):
        for query in deep_project_queries(contract, evidence):
            if query in queries:
                continue
            try:
                response = twitter.search(query, max_results=max_results, product="Latest")
                tweets.extend(normalize_search_items(response, max_results))
                queries.append(query)
            except Exception as exc:
                errors.append(f"{query}: {exc}")
        tweets = dedupe_tweets(tweets)
        evidence = build_evidence(twitter, contract, tweets)
    score = score_project(contract, evidence)
    status = "ok" if not errors else "partial"
    if errors and not tweets:
        status = "retry"
    return {
        "status": status,
        "queries": queries,
        "errors": errors,
        "evidence": evidence,
        "score": score,
    }


def build_evidence(
    twitter: OpenTwitterClient,
    contract: dict[str, Any],
    tweets: list[dict[str, Any]],
) -> dict[str, Any]:
    address = str(contract["address"]).lower()
    symbol = str(meaningful_symbol(contract.get("symbol")) or "").lower()
    meaningful_contract_name = str(meaningful_name(contract.get("name")) or "").lower()

    author_scores: dict[str, int] = defaultdict(int)
    author_address_mentions: dict[str, int] = defaultdict(int)
    author_project_mentions: dict[str, int] = defaultdict(int)
    author_own_project_mentions: dict[str, int] = defaultdict(int)
    author_own_crypto_project_mentions: dict[str, int] = defaultdict(int)
    author_chain_project_mentions: dict[str, int] = defaultdict(int)
    author_foreign_chain_project_mentions: dict[str, int] = defaultdict(int)
    author_project_urls: dict[str, list[str]] = defaultdict(list)
    profile_bonuses: dict[str, int] = defaultdict(int)
    author_flags: dict[str, list[str]] = defaultdict(list)
    author_examples: dict[str, dict[str, Any]] = {}
    urls: list[str] = []
    address_mentions = 0
    credible_address_mentions = 0
    signal_address_mentions = 0
    chain_project_mentions = 0
    foreign_chain_project_mentions = 0
    chain_slug = contract.get("chain_slug") or contract.get("chain")

    for tweet in tweets:
        text = str(tweet.get("text") or tweet.get("fullText") or "")
        lower_text = text.lower()
        screen_name = str(tweet.get("userScreenName") or tweet.get("username") or "").lstrip("@")
        if not screen_name:
            continue
        tweet_urls = extract_urls_from_value(tweet)
        signal_author = social_aggregator_profile(screen_name, tweet.get("userName")) or social_signal_tweet(text)
        if signal_author and "aggregator_or_signal_account" not in author_flags[screen_name]:
            author_flags[screen_name].append("aggregator_or_signal_account")
        if address in lower_text:
            author_address_mentions[screen_name] += 1
            address_mentions += 1
            if signal_author:
                signal_address_mentions += 1
            else:
                credible_address_mentions += 1
                author_scores[screen_name] += 5
        own_project_context = False
        if meaningful_contract_name and meaningful_contract_name in lower_text and not signal_author:
            author_scores[screen_name] += 3
            author_project_mentions[screen_name] += 1
            author_own_project_mentions[screen_name] += 1
            own_project_context = True
        if symbol and (f"${symbol}" in lower_text or f" {symbol} " in f" {lower_text} ") and not signal_author:
            author_scores[screen_name] += 3
            author_project_mentions[screen_name] += 1
            author_own_project_mentions[screen_name] += 1
            own_project_context = True
        if own_project_context and crypto_context_in_text(lower_text, symbol, chain_slug):
            author_own_crypto_project_mentions[screen_name] += 1
            author_project_urls[screen_name].extend(tweet_urls)
        elif not signal_author and address in lower_text and crypto_context_in_text(lower_text, symbol, chain_slug):
            author_project_urls[screen_name].extend(tweet_urls)
        has_project_context = project_context_in_text(lower_text, meaningful_contract_name, symbol)
        has_chain_context = chain_context_in_text(lower_text, chain_slug)
        if has_project_context and has_chain_context:
            chain_project_mentions += 1
            author_chain_project_mentions[screen_name] += 1
        if has_project_context and foreign_chain_context_in_text(lower_text) and not has_chain_context:
            foreign_chain_project_mentions += 1
            author_foreign_chain_project_mentions[screen_name] += 1
            if "foreign_chain_project_context" not in author_flags[screen_name]:
                author_flags[screen_name].append("foreign_chain_project_context")
        author_scores[screen_name] += 1
        author_examples.setdefault(screen_name, compact_tweet(tweet))
        urls.extend(tweet_urls)

        if (
            not signal_author
            and has_project_context
            and crypto_context_in_text(lower_text, symbol, chain_slug)
        ):
            for mentioned in extract_mentioned_handles(text):
                if mentioned.lower() == screen_name.lower():
                    continue
                author_scores[mentioned] += 2
                author_project_mentions[mentioned] += 1
                if "mentioned_by_project_context" not in author_flags[mentioned]:
                    author_flags[mentioned].append("mentioned_by_project_context")
                author_examples.setdefault(mentioned, compact_tweet(tweet))

    profiles: dict[str, dict[str, Any]] = {}
    profile_usernames = [
        username
        for username, _ in Counter(author_scores).most_common(5)
    ]
    for username in author_flags:
        if "mentioned_by_project_context" in author_flags[username] and username not in profile_usernames:
            profile_usernames.append(username)
    for username in profile_usernames[:10]:
        try:
            profile = normalize_user(twitter.user_info(username))
            if profile:
                profiles[username] = profile
                urls.extend(extract_urls_from_value(profile))
                if is_social_aggregator(username, profile):
                    if "aggregator_or_signal_account" not in author_flags[username]:
                        author_flags[username].append("aggregator_or_signal_account")
                    author_scores[username] -= 20
                    bonus = 0
                else:
                    bonus = profile_match_bonus(profile, contract)
                profile_bonuses[username] = bonus
                author_scores[username] += bonus
        except Exception:
            continue

    chosen_account = None
    chosen_reason = None
    if author_scores:
        for candidate, _score in sorted(author_scores.items(), key=lambda item: item[1], reverse=True):
            supported, reason = official_identity_supported(
                candidate,
                profiles.get(candidate, {}),
                contract,
                author_address_mentions,
                author_project_mentions,
                author_own_project_mentions,
                author_own_crypto_project_mentions,
            )
            if (
                not disqualifying_author_flags(author_flags.get(candidate, []))
                and profile_bonuses.get(candidate, 0) >= 8
                and supported
            ):
                chosen_account = candidate
                chosen_reason = reason
                break

    chosen_profile = profiles.get(chosen_account or "", {})
    official_profile_urls = expand_short_urls(extract_urls_from_value(chosen_profile)) if chosen_profile else []
    website = choose_website(official_profile_urls)
    website_reason = "official_profile_url" if website else None
    if not website and chosen_account:
        official_tweet_urls = expand_short_urls(author_project_urls.get(chosen_account, []))
        website = choose_matching_project_website(official_tweet_urls, contract)
        if website:
            website_reason = "official_project_tweet_url"
    source_website_urls = source_website_candidates(contract)
    if not website:
        for candidate in source_website_urls:
            if source_website_matches_contract(candidate, contract):
                website = candidate
                website_reason = "verified_source_url"
                break

    evidence = {
        "twitter_account": chosen_account,
        "twitter_name": chosen_profile.get("name") or author_examples.get(chosen_account or "", {}).get("userName"),
        "twitter_description": chosen_profile.get("description"),
        "twitter_followers": chosen_profile.get("followersCount")
        or author_examples.get(chosen_account or "", {}).get("userFollowers"),
        "twitter_verified": bool(
            chosen_profile.get("verified") or author_examples.get(chosen_account or "", {}).get("userVerified")
        ),
        "website": website,
        "official_website_reason": website_reason,
        "source_website_candidates": source_website_urls[:5],
        "tweet_count": len(tweets),
        "address_mentions": address_mentions,
        "credible_address_mentions": credible_address_mentions,
        "signal_address_mentions": signal_address_mentions,
        "chain_project_mentions": chain_project_mentions,
        "ethereum_project_mentions": chain_project_mentions,
        "foreign_chain_project_mentions": foreign_chain_project_mentions,
        "discussion_only": chosen_account is None and len(tweets) > 0,
        "official_identity_reason": chosen_reason,
        "official_account_address_mentions": author_address_mentions.get(chosen_account or "", 0),
        "official_account_project_mentions": author_project_mentions.get(chosen_account or "", 0),
        "official_account_own_project_mentions": author_own_project_mentions.get(chosen_account or "", 0),
        "official_account_own_crypto_project_mentions": author_own_crypto_project_mentions.get(chosen_account or "", 0),
        "official_account_chain_project_mentions": author_chain_project_mentions.get(chosen_account or "", 0),
        "official_account_ethereum_project_mentions": author_chain_project_mentions.get(chosen_account or "", 0),
        "official_account_foreign_chain_project_mentions": author_foreign_chain_project_mentions.get(chosen_account or "", 0),
        "official_account_flags": author_flags.get(chosen_account or "", []),
        "top_authors": [
            {
                "username": username,
                "score": score,
                "address_mentions": author_address_mentions.get(username, 0),
                "project_mentions": author_project_mentions.get(username, 0),
                "own_project_mentions": author_own_project_mentions.get(username, 0),
                "own_crypto_project_mentions": author_own_crypto_project_mentions.get(username, 0),
                "chain_project_mentions": author_chain_project_mentions.get(username, 0),
                "ethereum_project_mentions": author_chain_project_mentions.get(username, 0),
                "foreign_chain_project_mentions": author_foreign_chain_project_mentions.get(username, 0),
                "profile_match_bonus": profile_bonuses.get(username, 0),
                "flags": author_flags.get(username, []),
                "example": author_examples.get(username),
            }
            for username, score in sorted(author_scores.items(), key=lambda item: item[1], reverse=True)[:5]
        ],
        "sample_tweets": [compact_tweet(tweet) for tweet in tweets[:5]],
    }
    return augment_project_metadata(contract, evidence)


def choose_matching_project_website(urls: list[str], contract: dict[str, Any]) -> str | None:
    candidates = []
    for url in urls:
        website = choose_website([url])
        if website and source_website_matches_contract(website, contract):
            candidates.append(website)
    return choose_website(candidates)


def official_identity_supported(
    username: str,
    profile: dict[str, Any],
    contract: dict[str, Any],
    author_address_mentions: dict[str, int],
    author_project_mentions: dict[str, int],
    author_own_project_mentions: dict[str, int],
    author_own_crypto_project_mentions: dict[str, int],
) -> tuple[bool, str | None]:
    if author_address_mentions.get(username, 0) > 0:
        return True, "account_address_mention"
    if author_own_crypto_project_mentions.get(username, 0) > 0 and profile_has_crypto_identity(profile):
        return True, "account_crypto_project_context"
    if profile_has_crypto_identity(profile) and author_own_project_mentions.get(username, 0) > 0:
        return True, "profile_crypto_identity"
    if (
        profile_has_strong_project_identity(profile)
        and author_project_mentions.get(username, 0) > 0
        and not personal_affiliation_profile(profile, contract)
    ):
        return True, "profile_project_identity"
    return False, None


def source_website_candidates(contract: dict[str, Any]) -> list[str]:
    source = contract.get("source") if isinstance(contract, dict) else None
    if not isinstance(source, dict):
        return []
    urls = source.get("urls")
    if not isinstance(urls, list):
        return []
    candidates = []
    for url in urls:
        if not isinstance(url, str):
            continue
        website = choose_website([url])
        if website:
            candidates.append(website)
    return dedupe_urls(candidates)


def source_website_matches_contract(url: str, contract: dict[str, Any]) -> bool:
    markers = source_identity_markers(contract)
    if not markers:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host:
        return False
    if generic_token_factory_site(host):
        return False
    host_text = compact_identity_text(host)
    path_text = compact_identity_text(parsed.path)
    if any(marker in host_text for marker in markers):
        return True
    if generic_hosted_site(host):
        return False
    return any(marker in path_text for marker in markers)


def source_identity_markers(contract: dict[str, Any]) -> list[str]:
    markers = []
    for value in (contract.get("name"), contract.get("contract_name")):
        name = meaningful_name(value)
        if not name:
            continue
        for alias in project_name_aliases(name.lower()):
            markers.extend(source_marker_variants(alias))
    symbol = meaningful_symbol(contract.get("symbol"))
    if symbol and len(symbol) >= 4:
        markers.append(compact_identity_text(symbol))
    seen = set()
    result = []
    for marker in markers:
        if len(marker) < 4 or marker in seen:
            continue
        seen.add(marker)
        result.append(marker)
    return result


def source_marker_variants(value: str) -> list[str]:
    compact = compact_identity_text(value)
    variants = [compact]
    for suffix in ("contract", "token", "erc20", "erc721", "erc1155", "coin", "nft"):
        if compact.endswith(suffix) and len(compact) > len(suffix) + 3:
            variants.append(compact[: -len(suffix)])
    return variants


def compact_identity_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def generic_hosted_site(host: str) -> bool:
    normalized = host.lower().removeprefix("www.")
    return any(
        normalized == suffix or normalized.endswith(f".{suffix}")
        for suffix in GENERIC_HOSTED_SITE_SUFFIXES
    )


def generic_token_factory_site(host: str) -> bool:
    normalized = host.lower().removeprefix("www.")
    return any(
        normalized == suffix or normalized.endswith(f".{suffix}")
        for suffix in GENERIC_TOKEN_FACTORY_HOSTS
    )


def profile_has_crypto_identity(profile: dict[str, Any]) -> bool:
    text = " ".join(
        str(profile.get(field) or "").lower()
        for field in ("screenName", "name", "description")
    )
    compact = compact_identity_text(text)
    compact_cues = (
        ".eth",
        "eth",
        "ethereum",
        "oneth",
        "nft",
        "erc20",
        "erc721",
        "erc1155",
        "dao",
        "web3",
        "basescan",
        "bscscan",
        "pancakeswap",
        "aerodrome",
    )
    word_cues = (
        "token",
        "tokens",
        "tokenized",
        "tokenization",
        "protocol",
        "labs",
        "foundation",
        "network",
        "mint",
        "base",
        "bnb",
        "bsc",
    )
    return any(cue in text or cue in compact for cue in compact_cues) or has_word_cue(text, word_cues)


def profile_has_strong_project_identity(profile: dict[str, Any]) -> bool:
    text = " ".join(
        str(profile.get(field) or "").lower()
        for field in ("screenName", "name", "description")
    )
    compact = compact_identity_text(text)
    compact_cues = (
        "ethereum",
        "erc20",
        "erc721",
        "erc1155",
        "nft",
        "dao",
        "web3",
        "basescan",
        "bscscan",
        "pancakeswap",
        "aerodrome",
    )
    word_cues = (
        "token",
        "tokens",
        "tokenized",
        "tokenization",
        "protocol",
        "labs",
        "foundation",
        "network",
        "base",
        "bnb",
        "bsc",
    )
    weak_identity_cues = ("official", "account", "project", "community")
    return any(cue in text or cue in compact for cue in compact_cues) or has_word_cue(text, word_cues) or (
        any(cue in text for cue in weak_identity_cues) and profile_has_crypto_identity(profile)
    )


def has_word_cue(text: str, cues: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(cue)}\b", text, flags=re.I) for cue in cues)


def personal_affiliation_profile(profile: dict[str, Any], contract: dict[str, Any]) -> bool:
    screen_name = str(profile.get("screenName") or "").lower()
    display_name = str(profile.get("name") or "").lower()
    description = str(profile.get("description") or "").lower()
    screen_compact = compact_identity_text(screen_name)
    display_compact = compact_identity_text(display_name)
    markers = source_identity_markers(contract)
    if not markers:
        return False
    if any(marker in screen_compact for marker in markers):
        return False
    display_mentions_project = any(marker in display_compact for marker in markers)
    if ".eth" in f"{screen_name} {display_name}" and display_mentions_project:
        return True
    affiliation_separators = (" | ", " / ", " at ", "@")
    if display_mentions_project and any(separator in f" {display_name} {description} " for separator in affiliation_separators):
        return True
    return False


def profile_match_bonus(profile: dict[str, Any], contract: dict[str, Any]) -> int:
    name = str(meaningful_name(contract.get("name")) or "").lower()
    symbol = str(meaningful_symbol(contract.get("symbol")) or "").lower()
    screen_name = str(profile.get("screenName") or "").lower()
    display_name = str(profile.get("name") or "").lower()
    description = str(profile.get("description") or "").lower()
    identity = f"{screen_name} {display_name}"
    bonus = 0
    if any(alias in identity for alias in project_name_aliases(name)):
        bonus += 8
    if symbol and symbol in identity:
        bonus += 5
    if bonus and any(word in f"{identity} {description}" for word in ("official", "protocol", "labs", "foundation", "network")):
        bonus += 3
    return bonus


def project_name_aliases(name: str) -> list[str]:
    if not name:
        return []
    aliases = [name]
    for separator in (" - ", " | ", ": ", " ("):
        if separator in name:
            prefix = name.split(separator, 1)[0].strip()
            if len(prefix) >= 4:
                aliases.append(prefix)
    for suffix in (" hash token", " mining token", " miner token", " proof token", " token", " coin"):
        if name.endswith(suffix):
            prefix = name[: -len(suffix)].strip()
            if len(prefix) >= 4:
                aliases.append(prefix)
    result = []
    seen = set()
    for alias in aliases:
        if alias and alias not in seen:
            seen.add(alias)
            result.append(alias)
    return result


def extract_mentioned_handles(text: object) -> list[str]:
    seen = set()
    result = []
    for match in MENTION_RE.findall(str(text or "")):
        key = match.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(match)
    return result


def project_context_in_text(text: str, name: str, symbol: str) -> bool:
    if name and name in text:
        return True
    if symbol and (f"${symbol}" in text or f" {symbol} " in f" {text} "):
        return True
    return False


def crypto_context_in_text(text: str, symbol: str = "", chain_slug: str | None = None) -> bool:
    lowered = text.lower()
    if chain_context_in_text(lowered, chain_slug):
        return True
    if symbol and f"${symbol}" in lowered:
        return True
    padded = f" {lowered} "
    cues = (
        " eth",
        "ethereum",
        "uniswap",
        "dex",
        "dexscreener",
        "dextools",
        "token",
        "contract",
        " ca:",
        " ca ",
        "mint",
        "nft",
        "erc20",
        "erc721",
        "erc1155",
        "pool",
        "liquidity",
        "launch",
        "launched",
        "chain",
        "web3",
        "合约",
        "代币",
        "铸造",
    )
    return any(cue in padded for cue in cues)


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


def disqualifying_author_flags(flags: list[str]) -> bool:
    return any(flag != "mentioned_by_project_context" for flag in flags)


def is_social_aggregator(username: str, profile: dict[str, Any]) -> bool:
    return social_aggregator_profile(
        username,
        profile.get("screenName"),
        profile.get("name"),
        profile.get("description"),
    )


def compact_tweet(tweet: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": tweet.get("id") or tweet.get("twId"),
        "text": str(tweet.get("text") or tweet.get("fullText") or "")[:500],
        "createdAt": tweet.get("createdAt"),
        "userScreenName": tweet.get("userScreenName") or tweet.get("username"),
        "userName": tweet.get("userName"),
        "userFollowers": tweet.get("userFollowers"),
        "userVerified": tweet.get("userVerified"),
        "favoriteCount": tweet.get("favoriteCount"),
        "retweetCount": tweet.get("retweetCount"),
        "viewCount": tweet.get("viewCount"),
    }


def dedupe_queries(queries: list[str]) -> list[str]:
    seen = set()
    result = []
    for query in queries:
        normalized = " ".join(query.split())
        if normalized and normalized.lower() not in seen:
            seen.add(normalized.lower())
            result.append(normalized)
    return result


def dedupe_tweets(tweets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for tweet in tweets:
        key = tweet.get("id") or tweet.get("twId") or (
            tweet.get("userScreenName"),
            tweet.get("createdAt"),
            tweet.get("text") or tweet.get("fullText"),
        )
        marker = str(key)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(tweet)
    return result
