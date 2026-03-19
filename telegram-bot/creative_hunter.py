"""
creative_hunter.py - Per-cluster creative discovery.

For each product cluster that has fewer than 5 creatives, generates
localised keyword variations and searches Meta Ads Library for more ads
showing the same product from different advertisers.

Stopping rules per cluster:
  - STOP when cluster has 5 creatives (media URLs)
  - STOP after 10 keyword attempts regardless
"""

import asyncio
import logging
import re
from urllib.parse import urlparse

from ads_scraper import scrape_ads
from cluster_builder import ProductCluster, _domain
from localized_query_generator import generate_localized_queries

logger = logging.getLogger(__name__)

MAX_CREATIVES = 5
MAX_KEYWORD_ATTEMPTS = 10


async def hunt_creatives_for_cluster(
    cluster: ProductCluster,
    countries: list[str],
    media_type: str,
    seen_ad_ids: set[str],
) -> int:
    """
    Search for additional creatives for a single product cluster.

    Args:
        cluster:      The ProductCluster to enrich.
        countries:    List of country names to search (user-selected).
        media_type:   "image", "video", or "both".
        seen_ad_ids:  Global set of already-seen ad_library_urls (dedup across all clusters).

    Returns:
        Number of new creatives added to this cluster.
    """
    initial_creatives = len(cluster.media_urls)

    if initial_creatives >= MAX_CREATIVES:
        logger.info(
            f"[creative_hunter] cluster {cluster.sku} already has "
            f"{initial_creatives} creatives — skipping"
        )
        return 0

    # Gather product page data from the cluster's ads
    prod_name  = cluster.canonical_name
    page_title = next(
        (a.get("page_title", "") for a in cluster.ads if a.get("page_title")), ""
    )
    description = next(
        (a.get("_page_description", "") for a in cluster.ads if a.get("_page_description")), ""
    )
    bullets = []
    for ad in cluster.ads:
        bullets.extend(ad.get("_page_bullets") or [])
    bullets = list(dict.fromkeys(bullets))[:6]  # deduplicate, keep 6

    # The primary product domain (to recognise relevant results)
    cluster_domain = next(
        (_domain(a.get("landing_page_url", "")) for a in cluster.ads
         if a.get("landing_page_url")),
        ""
    )

    logger.info(
        f"[creative_hunter] cluster {cluster.sku}: "
        f"name='{prod_name[:60]}' domain='{cluster_domain}' "
        f"creatives={initial_creatives}/{MAX_CREATIVES}"
    )

    # Generate localised queries for each selected country
    all_queries: list[tuple[str, str]] = []  # (query, country)
    for country in countries:
        queries = generate_localized_queries(
            product_title=prod_name,
            page_title=page_title,
            description_text=description,
            bullet_points=bullets,
            country=country,
            max_queries=MAX_KEYWORD_ATTEMPTS,
        )
        logger.info(
            f"[creative_hunter] cluster {cluster.sku} / {country}: "
            f"generated {len(queries)} localised queries"
        )
        for q in queries:
            all_queries.append((q, country))

    # Interleave by country so we alternate regions
    all_queries = _interleave_by_country(all_queries, countries)

    attempts = 0
    new_creatives = 0
    consecutive_failures = 0  # tracks back-to-back login walls

    for query, country in all_queries:
        if len(cluster.media_urls) >= MAX_CREATIVES:
            logger.info(
                f"[creative_hunter] cluster {cluster.sku}: reached {MAX_CREATIVES} creatives — stopping"
            )
            break
        if attempts >= MAX_KEYWORD_ATTEMPTS:
            logger.info(
                f"[creative_hunter] cluster {cluster.sku}: reached {MAX_KEYWORD_ATTEMPTS} attempts — stopping"
            )
            break

        attempts += 1
        logger.info(
            f"[creative_hunter] cluster {cluster.sku}: attempt {attempts}/{MAX_KEYWORD_ATTEMPTS} "
            f"query='{query[:60]}' country={country} creatives={len(cluster.media_urls)}/{MAX_CREATIVES}"
        )

        # Use "both" (active + inactive) in a single browser session instead of two loops
        try:
            raw_ads = await scrape_ads(query, country, media_type, "both", None)
        except Exception as e:
            logger.debug(f"[creative_hunter] scrape error: {e}")
            raw_ads = []

        # Track consecutive login-wall failures and abort early to save time
        if not raw_ads:
            consecutive_failures += 1
            logger.info(
                f"[creative_hunter] cluster {cluster.sku}: 0 ads (consecutive failures: {consecutive_failures})"
            )
            if consecutive_failures >= 3:
                logger.warning(
                    f"[creative_hunter] cluster {cluster.sku}: aborting after "
                    f"{consecutive_failures} consecutive empty results — likely rate-limited"
                )
                break
        else:
            consecutive_failures = 0

        for ad in raw_ads:
            ad_id = ad.get("ad_library_url", "")

            # Skip globally seen ads
            if ad_id and ad_id in seen_ad_ids:
                logger.debug(f"[creative_hunter] duplicate skipped (global): {ad_id[:60]}")
                continue

            # Check if this ad is relevant to the cluster
            ad_domain = _domain(ad.get("landing_page_url", ""))
            if cluster_domain and ad_domain and ad_domain != cluster_domain:
                logger.debug(
                    f"[creative_hunter] domain mismatch: "
                    f"ad={ad_domain} vs cluster={cluster_domain}"
                )
                continue

            # Mark as seen globally
            if ad_id:
                seen_ad_ids.add(ad_id)

            before = len(cluster.media_urls)
            cluster.add_ad(ad)
            after = len(cluster.media_urls)

            if after > before:
                new_creatives += 1
                logger.info(
                    f"[creative_hunter] cluster {cluster.sku}: "
                    f"new creative added ({after}/{MAX_CREATIVES}) "
                    f"from advertiser='{ad.get('advertiser_name','')[:40]}'"
                )
            else:
                logger.debug(
                    f"[creative_hunter] duplicate creative skipped "
                    f"(advertiser='{ad.get('advertiser_name','')[:40]}')"
                )

            if len(cluster.media_urls) >= MAX_CREATIVES:
                break

    final_creatives = len(cluster.media_urls)
    if attempts >= MAX_KEYWORD_ATTEMPTS and final_creatives < MAX_CREATIVES:
        logger.info(
            f"[creative_hunter] cluster {cluster.sku}: stopping reason=10_attempts_reached "
            f"creatives_found={final_creatives}"
        )
    logger.info(
        f"[creative_hunter] cluster {cluster.sku}: done — "
        f"{new_creatives} new creatives added, total={final_creatives}"
    )
    return new_creatives


async def hunt_creatives_for_all_clusters(
    clusters: list[ProductCluster],
    countries: list[str],
    media_type: str,
    seen_ad_ids: set[str],
    progress_callback=None,
) -> int:
    """Run creative hunting for all clusters sequentially."""
    total_new = 0
    for i, cluster in enumerate(clusters):
        if len(cluster.media_urls) >= MAX_CREATIVES:
            continue

        if progress_callback:
            await progress_callback(
                f"  🎨 Hunting creatives for cluster {i+1}/{len(clusters)}: "
                f"*{cluster.canonical_name[:50] or cluster.sku}* "
                f"({len(cluster.media_urls)} creatives so far)"
            )

        added = await hunt_creatives_for_cluster(
            cluster, countries, media_type, seen_ad_ids
        )
        total_new += added

    return total_new


def _interleave_by_country(
    queries: list[tuple[str, str]], countries: list[str]
) -> list[tuple[str, str]]:
    """
    Interleave (query, country) pairs so we cycle through countries
    rather than exhausting one country before moving to the next.
    """
    by_country: dict[str, list[tuple[str, str]]] = {c: [] for c in countries}
    for q, c in queries:
        if c in by_country:
            by_country[c].append((q, c))

    result: list[tuple[str, str]] = []
    max_len = max((len(v) for v in by_country.values()), default=0)
    for i in range(max_len):
        for c in countries:
            if i < len(by_country[c]):
                result.append(by_country[c][i])
    return result
