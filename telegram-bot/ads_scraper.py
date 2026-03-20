"""
ads_scraper.py - Meta Ads Library scraper using Playwright with stealth mode.

Strategy:
1. Use playwright-stealth to bypass bot detection
2. Intercept GraphQL API responses for structured JSON data  
3. Navigate realistically (homepage first, then Ads Library)
4. Multiple fallback approaches to extract ad data
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import quote_plus, quote, unquote

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

from config import HEADLESS, MAX_ADS_TO_SCAN_PER_KEYWORD
from utils import normalize_url, normalize_text, safe_str, truncate
import fb_auth
from fb_auth import apply_stealth

logger = logging.getLogger(__name__)

MAX_HTML_VIDEO_SECONDS = 180  # 3 minutes — hard ceiling for Playwright-validated videos


class LoginWallError(Exception):
    """Raised when the Ads Library shows a login wall (cookies expired)."""


_BLOCKED_LANDING_DOMAINS: frozenset[str] = frozenset({
    "metastatus.com",
    "www.metastatus.com",
    "ads.metastatus.com",
})


def _is_blocked_landing_page(url: str) -> bool:
    """Return True if the landing page URL is on a known junk/redirect domain."""
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower().lstrip("www.")
        return host in _BLOCKED_LANDING_DOMAINS or f"www.{host}" in _BLOCKED_LANDING_DOMAINS
    except Exception:
        return False


async def scrape_ads(
    keyword: str,
    country: str,
    media_type_filter: str,
    active_filter: str,
    progress_callback=None,
    max_ads: int = 0,
    validate_html_media: bool = False,
    scroll_rounds: int = 0,
    on_ad_found=None,
) -> list[dict]:
    """
    Scrape Meta Ads Library for the given keyword + country.
    max_ads overrides MAX_ADS_TO_SCAN_PER_KEYWORD for this call (0 = use config).
    scroll_rounds overrides the auto-calculated scroll count (0 = auto).
    on_ad_found: optional async callback(ad_dict) called for each ad as it is found
                 during scrolling (before the full scrape completes).
    Returns a list of raw ad dicts.
    """
    _max = max_ads or MAX_ADS_TO_SCAN_PER_KEYWORD
    results: list[dict] = []
    country_code = _country_to_code(country)

    async with async_playwright() as p:
        browser, context = await fb_auth.build_playwright_context(p)

        page = await context.new_page()
        await apply_stealth(page)

        # Intercept GraphQL responses
        collected_json: list[dict] = []
        async def handle_response(response):
            if "api/graphql" in response.url:
                try:
                    body = await response.body()
                    text = body.decode("utf-8", errors="replace")
                    if "ad_archive_id" in text or "collated_results" in text:
                        data = json.loads(text)
                        collected_json.append(data)
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            if progress_callback:
                await progress_callback(f"Opening Ads Library for '{keyword}' in {country}…")

            # Warm up the session by visiting facebook.com first.
            # Going cold directly to the Ads Library triggers the login wall even with valid cookies.
            await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(0.5)
            await _handle_dialogs(page)

            # Navigate directly to the Ads Library search URL
            search_url = _build_search_url(keyword, country_code, active_filter)
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            await asyncio.sleep(0.5)

            # Handle cookie/login dialogs
            await _handle_dialogs(page)
            await asyncio.sleep(0.2)

            # Click "All ads" category if a category selector is visible
            await _select_all_ads_category(page)
            await asyncio.sleep(0.4)

            # Scroll to trigger GraphQL pagination.
            # Uses saturation detection: stop when Meta hasn't returned new GraphQL
            # responses for 3 consecutive scrolls (i.e. no more ads to load).
            # In streaming mode (on_ad_found set) use a lower cap so the first
            # results reach the user quickly; full mode keeps the higher cap.
            if scroll_rounds > 0:
                _hard_cap = scroll_rounds
            elif on_ad_found:
                _hard_cap = 12          # streaming: fast first results
            else:
                _hard_cap = max(40, _max // 5)
            _scroll_sleep    = 0.4 if on_ad_found else 0.7
            _no_new_streak   = 0   # consecutive scrolls with 0 new GraphQL responses
            _SATURATION_STOP = 3   # stop after this many dry scrolls in a row
            _last_json_count = len(collected_json)
            _streamed_count  = 0   # how many collected_json entries already streamed via on_ad_found
            _streamed_urls: set = set()  # dedup for streaming callback

            # ── Early-stream flush ──────────────────────────────────────────
            # GraphQL responses that arrived during page load are already in
            # collected_json. Emit them NOW before any scrolling so the user
            # sees the first creative as fast as possible.
            if on_ad_found and collected_json:
                for data in collected_json[_streamed_count:]:
                    try:
                        batch_ads = _extract_ads_from_graphql(data, keyword, country)
                        for ad in batch_ads:
                            if not _matches_filter(ad, media_type_filter, active_filter):
                                continue
                            url = str(ad.get("ad_library_url", "")).strip()
                            if url and url not in _streamed_urls:
                                _streamed_urls.add(url)
                                await on_ad_found(ad)
                    except Exception:
                        pass
                _streamed_count = len(collected_json)

            for i in range(_hard_cap):
                await _scroll_down(page)
                await asyncio.sleep(_scroll_sleep)

                # Stream newly arrived GraphQL batches via on_ad_found callback
                if on_ad_found and len(collected_json) > _streamed_count:
                    for data in collected_json[_streamed_count:]:
                        try:
                            batch_ads = _extract_ads_from_graphql(data, keyword, country)
                            for ad in batch_ads:
                                if not _matches_filter(ad, media_type_filter, active_filter):
                                    continue
                                url = str(ad.get("ad_library_url", "")).strip()
                                if url and url not in _streamed_urls:
                                    _streamed_urls.add(url)
                                    await on_ad_found(ad)
                        except Exception:
                            pass
                    _streamed_count = len(collected_json)

                _current_json_count = len(collected_json)
                if _current_json_count == _last_json_count:
                    _no_new_streak += 1
                    # Only stop early on saturation when we've actually received
                    # some GraphQL data. If GraphQL never fired at all (collected_json
                    # is still empty) keep scrolling — the page may just load slowly.
                    if _no_new_streak >= _SATURATION_STOP and _current_json_count > 0:
                        logger.info(
                            f"[scraper] Saturation reached after {i+1} scrolls "
                            f"({_no_new_streak} dry in a row) — stopping early"
                        )
                        break
                else:
                    _no_new_streak = 0
                _last_json_count = _current_json_count

                if progress_callback and (i % 5 == 0):
                    await progress_callback(
                        f"Loading ads for '{keyword}' / {country}… ({i+1}/{_hard_cap})"
                    )

            # Extra pause to let final GraphQL responses arrive
            await asyncio.sleep(1.5)

            # Stream any final batches that arrived after the loop
            if on_ad_found and len(collected_json) > _streamed_count:
                for data in collected_json[_streamed_count:]:
                    try:
                        batch_ads = _extract_ads_from_graphql(data, keyword, country)
                        for ad in batch_ads:
                            url = str(ad.get("ad_library_url", "")).strip()
                            if url and url not in _streamed_urls:
                                _streamed_urls.add(url)
                                await on_ad_found(ad)
                    except Exception:
                        pass

            # Parse all collected GraphQL responses for the return list
            for data in collected_json:
                ads = _extract_ads_from_graphql(data, keyword, country)
                results.extend(ads)
            collected_json.clear()

            logger.info(f"GraphQL interception got {len(results)} ads for '{keyword}'/{country}")

            # ── Resolve landing pages for GraphQL ads that have no URL in snapshot ──
            # Some ads (esp. video-only) don't include link_url in the GraphQL snapshot.
            # We visit each ad's individual Library page to extract the URL via DOM/JSON.
            no_lp_graphql = [a for a in results if not (a.get("landing_page_url") or "").strip()]
            if no_lp_graphql and results:
                logger.info(
                    f"[scraper] {len(no_lp_graphql)}/{len(results)} GraphQL ads have no landing_page_url "
                    f"— resolving via individual ad pages…"
                )
                if progress_callback:
                    await progress_callback(
                        f"🔗 Resolving landing pages for *{len(no_lp_graphql)}* ads missing URL…"
                    )
                resolved_ads = await _resolve_landing_pages(context, no_lp_graphql, max_concurrent=5)
                # Merge resolved ads back into results (matched by ad_library_url)
                resolved_map = {a.get("ad_library_url"): a for a in resolved_ads}
                results = [resolved_map.get(a.get("ad_library_url"), a) for a in results]
                still_no_lp = sum(1 for a in results if not (a.get("landing_page_url") or "").strip())
                logger.info(
                    f"[scraper] After resolution: {len(results) - still_no_lp} have landing page, "
                    f"{still_no_lp} still missing"
                )

            # Fallback: extract ad IDs from page HTML
            if not results:
                logger.info("Trying HTML fallback for ad IDs…")
                html_ads = await _html_fallback(page, keyword, country, max_ads=_max)
                if html_ads:
                    logger.info(f"HTML fallback got {len(html_ads)} ads")
                    # Resolve landing pages using the same authenticated browser context
                    if progress_callback:
                        await progress_callback(
                            f"🔗 Resolving landing pages for {len(html_ads)} ads…"
                        )
                    html_ads = await _resolve_landing_pages(context, html_ads, max_concurrent=5)
                    # Step 2 — Playwright media validation (only when requested)
                    if validate_html_media:
                        if progress_callback:
                            await progress_callback(
                                f"🎬 Validating media type for {len(html_ads)} candidate(s)…"
                            )
                        html_ads = await _playwright_validate_media(context, html_ads)
                    results.extend(html_ads)
                    # Stream HTML fallback results via on_ad_found so the user
                    # sees them immediately instead of waiting for scrape_ads() to return
                    if on_ad_found:
                        for ad in html_ads:
                            if not _matches_filter(ad, media_type_filter, active_filter):
                                continue
                            url = str(ad.get("ad_library_url", "")).strip()
                            if url and url not in _streamed_urls:
                                _streamed_urls.add(url)
                                await on_ad_found(ad)
                else:
                    # Diagnose why we got 0 — log page title + check for known no-results patterns
                    try:
                        page_title = await page.title()
                        page_text  = (await page.inner_text("body"))[:600].replace("\n", " ")
                        logger.info(f"[scraper] 0 ads — page title: '{page_title}'")
                        logger.info(f"[scraper] 0 ads — page snippet: {page_text[:300]}")

                        login_signals = [
                            "log in", "log in to continue", "connexion", "anmelden",
                            "iniciar sesión",
                        ]
                        no_result_signals = [
                            "no results", "aucun résultat", "keine ergebnisse",
                            "no ads match", "we couldn't find",
                        ]
                        page_lower = page_text.lower()
                        is_login_wall = any(s in page_lower for s in login_signals)
                        if is_login_wall:
                            logger.warning(
                                "[scraper] LOGIN WALL detected — cookies have expired"
                            )
                            raise LoginWallError("Facebook login wall detected — cookies expired")
                        elif any(s in page_lower for s in no_result_signals):
                            logger.warning(
                                f"[scraper] no-results page for '{keyword}' / {country}"
                            )
                        else:
                            logger.warning(
                                "[scraper] 0 ads but no obvious signal — "
                                "Facebook may have blocked the request or the keyword has no ads"
                            )
                    except LoginWallError:
                        raise
                    except Exception as diag_err:
                        logger.debug(f"[scraper] diagnostic failed: {diag_err}")

        except PlaywrightTimeout:
            logger.warning(f"Timeout for '{keyword}' / {country}")
        except LoginWallError:
            raise
        except Exception as e:
            logger.warning(f"Scraper error for '{keyword}' / {country}: {e}")
        else:
            # Successful scrape — persist the refreshed session for next run
            await fb_auth.save_auth_state(context)
        finally:
            await fb_auth.close_browser_context(browser, context)

    filtered = [ad for ad in results if _matches_filter(ad, media_type_filter, active_filter)]
    return filtered[:_max]


# ── Sync wrapper for ProcessPoolExecutor ──────────────────────────────────

def scrape_ads_sync(
    keyword: str,
    country: str,
    media_type_filter: str,
    active_filter: str,
    max_ads: int = 0,
    scroll_rounds: int = 0,
) -> list[dict]:
    """
    Synchronous wrapper around scrape_ads.
    Runs its own asyncio event loop so it is safe to call from a
    ProcessPoolExecutor worker (the worker has no running loop).
    progress_callback is intentionally omitted — progress messages are
    sent by _do_extraction in the main process before/after this call.
    """
    return asyncio.run(
        scrape_ads(
            keyword, country, media_type_filter, active_filter,
            progress_callback=None,
            max_ads=max_ads,
            scroll_rounds=scroll_rounds,
        )
    )


# ── GraphQL parsing ────────────────────────────────────────────────────────

def _extract_ads_from_graphql(data: dict, keyword: str, country: str) -> list[dict]:
    nodes: list[dict] = []
    _find_ad_nodes(data, nodes)
    ads = []
    for node in nodes:
        try:
            ad = _parse_ad_node(node, keyword, country)
            if ad:
                ads.append(ad)
        except Exception as e:
            logger.debug(f"Node parse error: {e}")
    return ads


def _find_ad_nodes(obj, results: list, depth: int = 0):
    if depth > 12:
        return
    if isinstance(obj, dict):
        if "ad_archive_id" in obj:
            results.append(obj)
            return
        for key in ("edges", "collated_results", "results"):
            if key in obj:
                items = obj[key] or []
                for item in items:
                    if isinstance(item, dict):
                        node = item.get("node", item)
                        _find_ad_nodes(node, results, depth + 1)
                return
        for v in obj.values():
            _find_ad_nodes(v, results, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _find_ad_nodes(item, results, depth + 1)


def _parse_ad_node(node: dict, keyword: str, country: str) -> Optional[dict]:
    ad_id = safe_str(node.get("ad_archive_id", ""))
    if not ad_id:
        return None

    page_name = safe_str(
        node.get("page_name") or node.get("advertiser_name") or
        _dig(node, "snapshot", "page_name") or ""
    )
    snapshot = node.get("snapshot") or {}
    cards = snapshot.get("cards") or []
    images = snapshot.get("images") or []
    videos = snapshot.get("videos") or []
    link_url = (
        snapshot.get("link_url") or
        _dig(snapshot, "link_og_object", "url") or
        (cards[0].get("link_url") if cards else "") or
        snapshot.get("website_url") or
        snapshot.get("cta_url") or
        node.get("click_through_url") or
        (cards[0].get("website_url") if cards else "") or
        (cards[0].get("cta_url") if cards else "") or
        ""
    )

    # Ad text
    body_text = ""
    body = snapshot.get("body") or {}
    if isinstance(body, dict):
        body_text = body.get("text") or re.sub(r"<[^>]+>", " ", _dig(body, "markup", "__html") or "")
    if not body_text:
        body_text = snapshot.get("caption") or snapshot.get("title") or ""

    # Media
    media_type, media_url, thumbnail_url, video_duration = "unknown", "", "", 0
    if videos:
        v = videos[0] if isinstance(videos[0], dict) else {}
        media_type = "video"
        media_url = v.get("video_hd_url") or v.get("video_sd_url") or ""
        thumbnail_url = v.get("video_preview_image_url") or ""
        video_duration = int(v.get("length") or v.get("duration") or 0)
    elif images:
        img = images[0] if isinstance(images[0], dict) else {}
        media_type = "image"
        media_url = img.get("original_image_url") or img.get("resized_image_url") or ""
        thumbnail_url = media_url
    elif cards:
        card = cards[0] if isinstance(cards[0], dict) else {}
        if card.get("video_hd_url") or card.get("video_sd_url"):
            media_type = "video"
            media_url = card.get("video_hd_url") or card.get("video_sd_url") or ""
            thumbnail_url = card.get("video_preview_image_url") or ""
            video_duration = int(card.get("length") or card.get("duration") or 0)
        elif card.get("original_image_url"):
            media_type = "image"
            media_url = card.get("original_image_url") or ""
            thumbnail_url = media_url

    is_active = node.get("is_active")
    active_status = "active" if is_active is True else ("inactive" if is_active is False else "unknown")
    product_name = _extract_product_name(body_text, snapshot.get("title", ""), page_name)

    return {
        "keyword": keyword,
        "country": country,
        "advertiser_name": safe_str(page_name),
        "ad_library_url": f"https://www.facebook.com/ads/library/?id={ad_id}",
        "landing_page_url": safe_str(link_url),
        "ad_text": truncate(normalize_text(body_text), 500),
        "media_type": media_type,
        "video_duration": video_duration,
        "media_url": safe_str(media_url),
        "thumbnail_url": safe_str(thumbnail_url),
        "extracted_product_name": safe_str(product_name),
        "normalized_product_name": normalize_text(product_name),
        "main_image_url": safe_str(thumbnail_url or media_url),
        "page_title": safe_str(snapshot.get("title", "")),
        "duplicate_group_id": "",
        "duplicates_count": 1,
        "active_status": active_status,
        "status": "NEW",
        "created_at": datetime.utcnow().isoformat(),
    }


# ── HTML fallback ──────────────────────────────────────────────────────────

async def _html_fallback(page: Page, keyword: str, country: str, max_ads: int = 0) -> list[dict]:
    """Extract ad IDs from rendered HTML when GraphQL interception fails."""
    _limit = max_ads or MAX_ADS_TO_SCAN_PER_KEYWORD
    ads = []
    try:
        html = await page.content()
        # Find ad archive IDs embedded in the page (they always appear as ?id=NNNN)
        ad_ids = list(dict.fromkeys(re.findall(r'(?:id=|ad_archive_id["\s:]+)(\d{10,})', html)))
        # Also search JSON blobs
        json_ids = re.findall(r'"ad_archive_id"\s*:\s*"?(\d+)"?', html)
        all_ids = list(dict.fromkeys(ad_ids + json_ids))

        for ad_id in all_ids[:_limit]:
            ads.append({
                "keyword": keyword,
                "country": country,
                "advertiser_name": "",
                "ad_library_url": f"https://www.facebook.com/ads/library/?id={ad_id}",
                "landing_page_url": "",
                "ad_text": "",
                "media_type": "unknown",
                "media_url": "",
                "thumbnail_url": "",
                "extracted_product_name": "",
                "normalized_product_name": "",
                "main_image_url": "",
                "page_title": "",
                "duplicate_group_id": "",
                "duplicates_count": 1,
                "active_status": "unknown",
                "status": "NEW",
                "created_at": datetime.utcnow().isoformat(),
            })
    except Exception as e:
        logger.debug(f"HTML fallback error: {e}")
    return ads


# ── Landing page resolver ─────────────────────────────────────────────────

async def _resolve_landing_pages(context, ads: list[dict], max_concurrent: int = 3) -> list[dict]:
    """
    Visit each ad's individual Ads Library page to extract:
      - The real destination / landing page URL (decoded from l.facebook.com redirect links)
      - The advertiser name from the page header

    Runs up to `max_concurrent` pages in parallel.
    Limits to the first 30 ads to avoid excessive delay.
    """
    ads_to_resolve = ads[:30]
    sem = asyncio.Semaphore(max_concurrent)
    resolved = 0
    failed = 0

    async def resolve_one(ad: dict) -> dict:
        nonlocal resolved, failed
        ad_id = ""
        lib_url = ad.get("ad_library_url", "")
        m_id = re.search(r"id=(\d+)", lib_url)
        if m_id:
            ad_id = m_id.group(1)
        if not ad_id:
            return ad

        async with sem:
            page = None
            try:
                page = await context.new_page()
                await apply_stealth(page)

                # ── Intercept GraphQL responses on the individual ad page ──────────
                # Facebook makes a GraphQL call when rendering the ad detail page.
                # That response contains the full ad snapshot including link_url /
                # website_url / cta fields that are NOT always in the search results.
                intercepted_url: list = []

                async def _on_response(response):
                    try:
                        if "graphql" not in response.url.lower():
                            return
                        if response.status != 200:
                            return
                        body = await response.body()
                        text = body.decode("utf-8", errors="replace")
                        if ad_id not in text:
                            return
                        # Search for any non-facebook URL in the response
                        for pat in [
                            r'"link_url"\s*:\s*"(https?://(?!(?:www\.)?(?:facebook|instagram|fbcdn))[^"\\]+)"',
                            r'"website_url"\s*:\s*"(https?://(?!(?:www\.)?(?:facebook|instagram|fbcdn))[^"\\]+)"',
                            r'"cta_url"\s*:\s*"(https?://(?!(?:www\.)?(?:facebook|instagram|fbcdn))[^"\\]+)"',
                            r'"click_through_url"\s*:\s*"(https?://(?!(?:www\.)?(?:facebook|instagram|fbcdn))[^"\\]+)"',
                            r'"external_url"\s*:\s*"(https?://(?!(?:www\.)?(?:facebook|instagram|fbcdn))[^"\\]+)"',
                        ]:
                            mm = re.search(pat, text)
                            if mm:
                                url_found = unquote(mm.group(1).replace("\\/", "/"))
                                if url_found not in intercepted_url:
                                    intercepted_url.append(url_found)
                    except Exception:
                        pass

                page.on("response", _on_response)

                await page.goto(
                    f"https://www.facebook.com/ads/library/?id={ad_id}",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                # Wait for JS rendering and GraphQL calls to complete
                await asyncio.sleep(4)

                # Check if we were redirected to login
                current_url = page.url
                if "login" in current_url or "checkpoint" in current_url:
                    logger.warning(f"[resolve] redirected to login for ad {ad_id} — skipping")
                    failed += 1
                    return ad

                # Check if we were redirected off Facebook entirely
                # (e.g. French ads redirect to metastatus.com/ads-transparency for unauthed sessions)
                if "facebook.com" not in current_url:
                    logger.warning(
                        f"[resolve] redirected off Facebook for ad {ad_id} "
                        f"→ {current_url[:80]} — skipping (stale/missing session?)"
                    )
                    failed += 1
                    return ad

                landing_found = False

                # ── Priority 1: URL from intercepted GraphQL response ─────────────
                if intercepted_url:
                    best = next((u for u in intercepted_url if not _is_blocked_landing_page(u)), None)
                    if best:
                        ad["landing_page_url"] = best
                        logger.info(f"[resolve] ✓ GraphQL intercept for {ad_id}: {best[:80]}")
                        resolved += 1
                        landing_found = True

                # ── Priority 2 & 3: JS evaluation — catch ALL URL formats ────────
                # Facebook renders CTAs as divs with onclick, data-href, or
                # l.facebook.com / lm.facebook.com / direct href.
                # A single JS evaluate call finds ALL of them.
                if not landing_found:
                    try:
                        js_urls = await page.evaluate("""
                            () => {
                                const BAD = ['facebook.com', 'instagram.com', 'fbcdn.net',
                                             'fb.com', 'fb.me', 'about:', 'metastatus.com'];
                                const isBad = u => !u || BAD.some(b => u.includes(b));
                                const decode = u => {
                                    // unwrap l.php / lm redirect
                                    const m = u.match(/[?&]u=(https?[^&"]+)/i);
                                    if (m) {
                                        try { return decodeURIComponent(m[1]); } catch(e) {}
                                    }
                                    return u;
                                };
                                const seen = new Set();
                                const add = (u) => {
                                    if (!u || !u.startsWith('http')) return;
                                    const d = decode(u);
                                    if (d.startsWith('http') && !isBad(d) && d.length > 10)
                                        seen.add(d);
                                };
                                // <a href>
                                document.querySelectorAll('a[href]').forEach(el => add(el.href));
                                // data-href
                                document.querySelectorAll('[data-href]').forEach(el => add(el.dataset.href));
                                // data-url
                                document.querySelectorAll('[data-url]').forEach(el => add(el.dataset.url));
                                // onclick handlers containing https://
                                document.querySelectorAll('[onclick]').forEach(el => {
                                    const m = el.getAttribute('onclick').match(/https?:\\/\\/[^'"\\\\)\\s]+/g);
                                    if (m) m.forEach(add);
                                });
                                return [...seen];
                            }
                        """)
                        clean = [u for u in (js_urls or []) if not _is_blocked_landing_page(u)]
                        if clean:
                            ad["landing_page_url"] = clean[0]
                            logger.info(f"[resolve] ✓ JS eval for {ad_id}: {clean[0][:80]}")
                            resolved += 1
                            landing_found = True
                        else:
                            logger.debug(f"[resolve] JS eval: no external URLs found for {ad_id}")
                    except Exception as js_err:
                        logger.debug(f"[resolve] JS eval error for {ad_id}: {js_err}")

                # ── Priority 4: Embedded JSON in page HTML ───────────────────────
                if not landing_found:
                    html = await page.content()
                    for pat in [
                        r'"link_url"\s*:\s*"(https?://(?!(?:www\.)?(?:facebook|instagram|fbcdn))[^"\\]+)"',
                        r'"website_url"\s*:\s*"(https?://(?!(?:www\.)?(?:facebook|instagram|fbcdn))[^"\\]+)"',
                        r'"external_url"\s*:\s*"(https?://(?!(?:www\.)?(?:facebook|instagram|fbcdn))[^"\\]+)"',
                        r'"cta_url"\s*:\s*"(https?://(?!(?:www\.)?(?:facebook|instagram|fbcdn))[^"\\]+)"',
                        r'"click_through_url"\s*:\s*"(https?://(?!(?:www\.)?(?:facebook|instagram|fbcdn))[^"\\]+)"',
                    ]:
                        mm = re.search(pat, html)
                        if mm:
                            url_found = unquote(mm.group(1).replace("\\/", "/"))
                            if _is_blocked_landing_page(url_found):
                                continue
                            ad["landing_page_url"] = url_found
                            logger.info(f"[resolve] ✓ HTML JSON for {ad_id}: {url_found[:80]}")
                            resolved += 1
                            landing_found = True
                            break
                    if not landing_found:
                        # Log ALL hrefs on the page so we can debug what Facebook renders
                        try:
                            all_hrefs = await page.evaluate(
                                "() => [...document.querySelectorAll('[href]')].map(e=>e.getAttribute('href')||'').filter(Boolean).slice(0,8)"
                            )
                        except Exception:
                            all_hrefs = []
                        logger.info(
                            f"[resolve] ✗ no LP for {ad_id} "
                            f"page_url={current_url[:70]} "
                            f"page_hrefs={all_hrefs}"
                        )
                        failed += 1
                else:
                    html = await page.content()

                # ── Extract media URL from embedded JSON (video/image CDN path) ──────
                # We already have the page HTML — mining the actual video/image URL here
                # enables Layer 4 CDN-path dedup with ZERO extra network requests.
                if not ad.get("media_url") or not ad.get("thumbnail_url"):
                    # Video: prefer HD, fall back to SD; also grab the static preview frame
                    video_hd  = _extract_json_field(html, "video_hd_url")
                    video_sd  = _extract_json_field(html, "video_sd_url")
                    vid_thumb = _extract_json_field(html, "video_preview_image_url")
                    img_orig  = _extract_json_field(html, "original_image_url")
                    img_rsz   = _extract_json_field(html, "resized_image_url")

                    media_url = video_hd or video_sd or img_orig or img_rsz or ""
                    thumb_url = vid_thumb or img_orig or img_rsz or ""

                    # Fallback: <video poster="..."> tag
                    if not thumb_url:
                        vm = re.search(r'<video[^>]+poster=["\']([^"\']+)["\']', html)
                        if vm:
                            thumb_url = vm.group(1)

                    if media_url:
                        ad["media_url"] = media_url
                        logger.debug(f"[resolve] media_url for {ad_id}: {media_url[:60]}")
                    if thumb_url:
                        ad["thumbnail_url"] = thumb_url
                        logger.debug(f"[resolve] thumbnail_url for {ad_id}: {thumb_url[:60]}")

                # ── Extract advertiser name from the page ──
                if not ad.get("advertiser_name"):
                    try:
                        # The advertiser name appears as a prominent heading
                        for sel in ("h2", "[data-testid='page-name']", "h1"):
                            el = await page.query_selector(sel)
                            if el:
                                name = (await el.inner_text()).strip()
                                # Filter out generic navigation text
                                if name and 2 < len(name) < 100 and "Ad Library" not in name:
                                    ad["advertiser_name"] = name
                                    break
                    except Exception:
                        pass

            except Exception as e:
                logger.debug(f"[resolve] error for ad {ad_id}: {e}")
                failed += 1
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
        return ad

    try:
        resolved_ads = list(
            await asyncio.wait_for(
                asyncio.gather(*[resolve_one(ad) for ad in ads_to_resolve]),
                timeout=90,
            )
        )
    except asyncio.TimeoutError:
        logger.warning("[resolve] _resolve_landing_pages timed out after 90s — returning partial results")
        resolved_ads = ads_to_resolve
    logger.info(
        f"[resolve] complete — {resolved}/{len(ads_to_resolve)} landing pages resolved, "
        f"{failed} failed"
    )
    return resolved_ads


# ── Playwright media validation (Step 2 of HTML fallback pipeline) ─────────

async def _playwright_validate_media(
    context,
    html_ads: list[dict],
    max_video_seconds: int = MAX_HTML_VIDEO_SECONDS,
    max_concurrent: int = 2,
) -> list[dict]:
    """
    Step 2 of the HTML fallback pipeline.

    For each candidate ad (discovered via HTML fallback with media_type='unknown'):
    1. Open the ad's Ads Library page in a real Playwright browser page
    2. Wait for dynamic media to render
    3. Inspect the DOM for a real <video> element
    4. Read video.duration from the browser using JS evaluation (retry up to 3×)
    5. Reject if no video element found
    6. Reject if duration cannot be determined
    7. Reject if duration > max_video_seconds
    8. Accept and set media_type='video', video_duration=<seconds>

    Ads that fail validation are removed from the returned list.
    """
    if not html_ads:
        return html_ads

    sem = asyncio.Semaphore(max_concurrent)

    async def validate_one(ad: dict) -> dict:
        lib_url = ad.get("ad_library_url", "")
        m_id = re.search(r"id=(\d+)", lib_url)
        ad_id = m_id.group(1) if m_id else lib_url

        logger.info(f"[validate] Opening candidate ad {ad_id} in Playwright")

        async with sem:
            page = None
            try:
                page = await context.new_page()
                await apply_stealth(page)

                await page.goto(lib_url, wait_until="load", timeout=20000)
                await asyncio.sleep(3)

                # ── Step 1: check for a real <video> element ──────────────
                video_elem = await page.query_selector("video")
                if not video_elem:
                    logger.info(
                        f"[validate] ✗ No video element found for ad {ad_id} — rejected"
                    )
                    ad["media_type"] = "rejected"
                    return ad

                logger.info(f"[validate] ✓ Video element found for ad {ad_id}")

                # Grab src for media_url if not already set
                try:
                    src = await video_elem.get_attribute("src") or ""
                    if src and src.startswith("http") and not ad.get("media_url"):
                        ad["media_url"] = src
                except Exception:
                    pass

                # ── Step 2: read video.duration from the browser ──────────
                duration = 0.0
                for attempt in range(3):
                    try:
                        raw = await page.evaluate(
                            "() => { const v = document.querySelector('video'); "
                            "return v ? v.duration : null; }"
                        )
                        # NaN shows up as float('nan') — raw==raw is False for NaN
                        if raw is not None and raw == raw and float(raw) > 0:
                            duration = float(raw)
                            break
                    except Exception:
                        pass
                    if attempt < 2:
                        await asyncio.sleep(2)

                if duration <= 0:
                    logger.info(
                        f"[validate] ✗ Could not determine duration for ad {ad_id} — rejected"
                    )
                    ad["media_type"] = "rejected"
                    return ad

                logger.info(f"[validate] Duration detected: {duration:.0f}s for ad {ad_id}")

                if duration > max_video_seconds:
                    logger.info(
                        f"[validate] ✗ Duration {duration:.0f}s > {max_video_seconds}s "
                        f"for ad {ad_id} — rejected"
                    )
                    ad["media_type"] = "rejected"
                    return ad

                logger.info(
                    f"[validate] ✓ Accepted ad {ad_id} as valid video creative "
                    f"({duration:.0f}s)"
                )
                ad["media_type"] = "video"
                ad["video_duration"] = duration
                return ad

            except Exception as e:
                logger.warning(f"[validate] Error validating ad {ad_id}: {e}")
                ad["media_type"] = "rejected"
                return ad
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass

    validated = list(await asyncio.gather(*[validate_one(ad) for ad in html_ads]))
    kept = [a for a in validated if a.get("media_type") != "rejected"]
    logger.info(
        f"[validate] {len(kept)}/{len(html_ads)} HTML ads passed media validation"
    )
    return kept


# ── Page interaction helpers ───────────────────────────────────────────────

async def _handle_dialogs(page: Page):
    """Try to dismiss cookie banners and any overlays."""
    selectors = [
        'button[title="Allow all cookies"]',
        'button:has-text("Allow all cookies")',
        'button:has-text("Accept All")',
        'button:has-text("Accept")',
        '[data-cookiebanner="accept_button"]',
        'button[data-testid="cookie-policy-banner-accept"]',
    ]
    for sel in selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click(timeout=2000)
                await asyncio.sleep(1)
                return
        except Exception:
            pass
    # Try by text role
    for text in ["Allow all cookies", "Accept All", "Accept Cookies"]:
        try:
            btn = page.get_by_role("button", name=re.compile(text, re.IGNORECASE))
            if await btn.count() > 0:
                await btn.first.click()
                await asyncio.sleep(1)
                return
        except Exception:
            pass


async def _type_keyword_search(page: Page, keyword: str) -> bool:
    """
    Find the Ads Library search input, clear it, type the keyword, and press Enter.
    Uses JS evaluation to locate any focusable input/combobox regardless of element type.
    Returns True if the search was submitted successfully, False otherwise.
    """
    # Debug: log all inputs and comboboxes visible on the page
    try:
        debug_info = await page.evaluate("""() => {
            const inputs = Array.from(document.querySelectorAll(
                'input, [role="combobox"], [role="searchbox"], [contenteditable="true"]'
            ));
            return inputs.map(el => ({
                tag: el.tagName,
                type: el.type || '',
                placeholder: el.placeholder || el.getAttribute('placeholder') || '',
                ariaLabel: el.getAttribute('aria-label') || '',
                role: el.getAttribute('role') || '',
                visible: el.offsetParent !== null,
                id: el.id || '',
                className: el.className ? el.className.toString().slice(0, 60) : ''
            }));
        }""")
        logger.info(f"[scraper] Page inputs found: {debug_info}")
    except Exception as ex:
        logger.debug(f"[scraper] debug eval failed: {ex}")

    # Use JS to find and focus the search input, then use keyboard to type
    try:
        found = await page.evaluate("""(keyword) => {
            const isVisible = el => el.offsetParent !== null || el.offsetWidth > 0;

            // Pass 1: explicit searchbox role (the keyword input in Ads Library)
            const searchboxes = Array.from(document.querySelectorAll('[role="searchbox"]'));
            for (const el of searchboxes) {
                if (isVisible(el)) {
                    el.focus(); el.click();
                    return { found: true, tag: el.tagName, role: 'searchbox', pass: 1 };
                }
            }

            // Pass 2: input with search-related placeholder or type
            const inputs = Array.from(document.querySelectorAll('input, textarea'));
            const patterns = [/search by keyword/i, /keyword or advertiser/i, /keyword/i, /advertiser/i, /search/i];
            for (const el of inputs) {
                if (!isVisible(el)) continue;
                const ph = (el.placeholder || el.getAttribute('placeholder') || '');
                const al = (el.getAttribute('aria-label') || '');
                if (el.type === 'search' || patterns.some(p => p.test(ph) || p.test(al))) {
                    el.focus(); el.click();
                    return { found: true, tag: el.tagName, ph, al, type: el.type, pass: 2 };
                }
            }

            // Pass 3: last resort — any visible combobox
            const combos = Array.from(document.querySelectorAll('[role="combobox"]'));
            for (const el of combos) {
                if (isVisible(el)) {
                    el.focus(); el.click();
                    return { found: true, tag: el.tagName, role: 'combobox', pass: 3 };
                }
            }

            return { found: false };
        }""", keyword)
        logger.info(f"[scraper] JS search element result: {found}")
        if found.get("found"):
            await asyncio.sleep(0.4)
            # Select all + delete any existing text, then type
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Backspace")
            await asyncio.sleep(0.2)
            # Type keyword with human-like delays
            for char in keyword:
                await page.keyboard.type(char)
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.4)
            await page.keyboard.press("Enter")
            logger.info(f"[scraper] Typed '{keyword}' via JS-focused element ({found})")
            return True
    except Exception as ex:
        logger.warning(f"[scraper] JS search approach failed: {ex}")

    # CSS selector fallback — try broad selectors one by one
    fallback_selectors = [
        'input[placeholder*="keyword"]',
        'input[placeholder*="advertiser"]',
        'input[placeholder*="Search"]',
        'input[type="search"]',
        '[role="combobox"]',
        '[role="searchbox"]',
        'input',
    ]
    for sel in fallback_selectors:
        try:
            elements = await page.query_selector_all(sel)
            for el in elements:
                if await el.is_visible():
                    await el.click(timeout=3000)
                    await el.triple_click()
                    await el.press("Control+a")
                    await el.press("Backspace")
                    await asyncio.sleep(0.3)
                    await el.type(keyword, delay=55)
                    await asyncio.sleep(0.4)
                    await el.press("Enter")
                    logger.info(f"[scraper] Typed '{keyword}' via CSS selector '{sel}'")
                    return True
        except Exception as ex:
            logger.debug(f"[scraper] CSS selector '{sel}' failed: {ex}")
            continue

    logger.warning(f"[scraper] Could not find any search box for '{keyword}'")
    return False


async def _select_all_ads_category(page: Page):
    """Select 'All ads' category if the category dropdown is visible."""
    try:
        # The ads library sometimes shows a category filter on the left
        # Try to find and click "All ads" if a category is selected
        sel = 'span:has-text("Issues, elections or politics")'
        el = await page.query_selector(sel)
        if el:
            # There might be a way to switch to "All ads"
            all_ads_btn = await page.query_selector('span:has-text("All ads")')
            if all_ads_btn:
                await all_ads_btn.click(timeout=2000)
                await asyncio.sleep(2)
    except Exception:
        pass


async def _scroll_down(page: Page):
    await page.evaluate("window.scrollBy(0, 1500)")
    await asyncio.sleep(1.5)


# ── URL and country helpers ────────────────────────────────────────────────

def _build_search_url(keyword: str, country_code: str, active_filter: str) -> str:
    active_status = "active" if active_filter == "active" else "all"
    encoded_kw = quote(keyword, safe='')
    return (
        f"https://www.facebook.com/ads/library/"
        f"?active_status={active_status}"
        f"&ad_type=all"
        f"&country={country_code}"
        f"&is_targeted_country=false"
        f"&media_type=all"
        f"&q={encoded_kw}"
        f"&search_type=keyword_unordered"
        f"&sort_data[direction]=desc"
        f"&sort_data[mode]=total_impressions"
        f"&source=page-transparency-widget"
    )


def _build_page_search_url(page_name: str, country_code: str, active_filter: str) -> str:
    """Build an Ads Library URL that searches for ads by advertiser name.
    Uses search_type=page which renders in the standard list layout (same as keyword
    search) so GraphQL interception works reliably."""
    active_status = "active" if active_filter == "active" else "all"
    encoded = quote(page_name, safe='')
    return (
        f"https://www.facebook.com/ads/library/"
        f"?active_status={active_status}"
        f"&ad_type=all"
        f"&country={country_code}"
        f"&is_targeted_country=false"
        f"&media_type=all"
        f"&q={encoded}"
        f"&search_type=page"
        f"&sort_data[direction]=desc"
        f"&sort_data[mode]=total_impressions"
    )


def _build_page_id_search_url(page_id: str, country_code: str, active_filter: str) -> str:
    """Fallback URL using view_all_page_id (gallery layout, harder to scrape)."""
    active_status = "ACTIVE" if active_filter == "active" else "ALL"
    return (
        f"https://www.facebook.com/ads/library/"
        f"?active_status={active_status}"
        f"&ad_type=ALL"
        f"&country={country_code}"
        f"&view_all_page_id={page_id}"
        f"&search_type=page"
        f"&media_type=all"
    )


def _extract_page_id_from_url(fb_page_url: str) -> Optional[str]:
    """
    Try to extract a numeric Facebook page ID directly from the URL.
    Works for:
      - https://www.facebook.com/profile.php?id=123456789
      - https://www.facebook.com/pages/Name/123456789
      - https://www.facebook.com/123456789   (pure numeric path)
    Returns None if the URL uses a named handle (e.g. /SomeBrand/).
    """
    # profile.php?id=DIGITS
    m = re.search(r"[?&]id=(\d+)", fb_page_url)
    if m:
        return m.group(1)
    # /pages/Any-Name/DIGITS
    m = re.search(r"/pages/[^/]+/(\d+)", fb_page_url)
    if m:
        return m.group(1)
    # Pure numeric path segment: /123456789/  or  /123456789
    m = re.search(r"/(\d{8,})[/?]?$", fb_page_url)
    if m:
        return m.group(1)
    return None


def _extract_page_handle_from_url(fb_page_url: str) -> str:
    """
    Extract a human-readable page handle/name from a Facebook URL for use as
    the Ads Library search term.  Returns "" if the URL is purely numeric.

    Examples:
      /SomeBrand/               → "SomeBrand"
      /people/Adele-hues5/...   → "Adele hues5"
      /profile.php?id=123       → ""  (numeric, need to visit page for name)
      /pages/Brand-Name/123     → "Brand Name"
    """
    from urllib.parse import urlparse as _up
    path = _up(fb_page_url).path.rstrip("/")

    # /people/HANDLE/NUMERIC_ID  → use HANDLE
    m = re.match(r"^/people/([^/]+)/\d+$", path)
    if m:
        return m.group(1).replace("-", " ").replace("_", " ")

    # /pages/HANDLE/NUMERIC_ID  → use HANDLE
    m = re.match(r"^/pages/([^/]+)/\d+$", path)
    if m:
        return m.group(1).replace("-", " ").replace("_", " ")

    # /HANDLE/ where HANDLE is not purely numeric
    segments = [s for s in path.split("/") if s]
    if segments and not segments[-1].isdigit() and segments[-1] != "profile.php":
        handle = segments[-1]
        if not handle.isdigit():
            return handle.replace("-", " ").replace("_", " ")

    return ""


async def scrape_ads_by_page_url(
    fb_page_url: str,
    country: str,
    media_type_filter: str,
    active_filter: str,
    progress_callback=None,
) -> list[dict]:
    """
    Scrape all ads from a specific Facebook page by visiting its Ads Library page.
    `fb_page_url` can be any public Facebook page URL (profile, named page, etc.).
    """
    results: list[dict] = []
    country_code = _country_to_code(country)

    async with async_playwright() as p:
        browser, context = await fb_auth.build_playwright_context(p)

        page = await context.new_page()
        await apply_stealth(page)

        collected_json: list[dict] = []

        async def handle_response(response):
            if "api/graphql" in response.url:
                try:
                    body = await response.body()
                    text = body.decode("utf-8", errors="replace")
                    if "ad_archive_id" in text or "collated_results" in text:
                        data = json.loads(text)
                        collected_json.append(data)
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            # Warm up session
            await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
            await _handle_dialogs(page)

            # ── Step 1: extract advertiser name to search by ───────────────
            # Strategy: search by page NAME with search_type=page, which uses the
            # standard list layout where GraphQL interception already works perfectly.
            # The view_all_page_id gallery layout cannot be scraped the same way.
            page_id = _extract_page_id_from_url(fb_page_url)
            page_name = _extract_page_handle_from_url(fb_page_url)  # may be "" if numeric URL

            # If we only have a numeric ID (no named handle), visit the page to get display name
            if not page_name:
                if progress_callback:
                    await progress_callback("🔍 Visiting the Facebook page to find its name…")
                await page.goto(fb_page_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)
                html = await page.content()

                # Try to get display name from page title ("Page Name | Facebook" or "Page Name")
                title_m = re.search(r"<title>([^|<]+?)(?:\s*[|·-]\s*Facebook)?</title>", html, re.IGNORECASE)
                if title_m:
                    page_name = title_m.group(1).strip()

                # Also try to grab page ID while we're here
                if not page_id:
                    for pattern in [
                        r'"page_id"\s*:\s*"?(\d+)"?',
                        r'"entity_id"\s*:\s*"?(\d+)"?',
                        r'"pageID"\s*:\s*"?(\d+)"?',
                        r'content_owner_id_new%22%3A(\d+)',
                    ]:
                        m = re.search(pattern, html)
                        if m:
                            page_id = m.group(1)
                            break

                # Fallback: try page h1/h2 heading text
                if not page_name:
                    for sel in ("h1", "h2"):
                        try:
                            el = await page.query_selector(sel)
                            if el:
                                t = (await el.inner_text()).strip()
                                if t and len(t) > 2:
                                    page_name = t
                                    break
                        except Exception:
                            pass

            if not page_name and not page_id:
                logger.warning(f"[page_scraper] Could not identify page from {fb_page_url}")
                if progress_callback:
                    await progress_callback(
                        "⚠️ Could not identify the Facebook page from that URL.\n"
                        "Make sure it is a valid public Facebook page URL."
                    )
                return []

            # Use page name for search; fall back to page ID as string
            search_term = page_name or page_id
            logger.info(f"[page_scraper] Searching by name='{search_term}' page_id={page_id} for {fb_page_url}")
            if progress_callback:
                await progress_callback(
                    f"🔎 Searching Ads Library for advertiser: *{search_term}*…"
                )

            # ── Step 2: search by advertiser name (standard list layout) ──
            search_url = _build_page_search_url(search_term, country_code, active_filter)
            await page.goto(search_url, wait_until="domcontentloaded", timeout=35000)
            await asyncio.sleep(5)
            await _handle_dialogs(page)
            await asyncio.sleep(2)
            await _select_all_ads_category(page)
            await asyncio.sleep(2)

            # Scroll to load ads — saturation-based (same as keyword search)
            _ps_hard_cap     = max(40, MAX_ADS_TO_SCAN_PER_KEYWORD // 5)
            _ps_no_new       = 0
            _ps_last_count   = len(collected_json)
            for i in range(_ps_hard_cap):
                await _scroll_down(page)
                await asyncio.sleep(2)
                _ps_cur = len(collected_json)
                if _ps_cur == _ps_last_count:
                    _ps_no_new += 1
                    if _ps_no_new >= 3:
                        logger.info(f"[page_scraper] Saturation after {i+1} scrolls — stopping")
                        break
                else:
                    _ps_no_new = 0
                _ps_last_count = _ps_cur
                if progress_callback and i % 5 == 0:
                    await progress_callback(f"  Loading ads… ({i+1}/{_ps_hard_cap})")

            # ── Method 1: GraphQL interception ─────────────────────────────
            for data in collected_json:
                ads = _extract_ads_from_graphql(data, fb_page_url, country)
                results.extend(ads)
            logger.info(f"[page_scraper] GraphQL got {len(results)} ads for page {page_id}")

            # ── Method 2: CSS-selector ad ID extraction (most reliable fallback) ──
            # Finds all "See ad details" / individual ad links rendered on the page.
            # Critically, we EXCLUDE the page_id itself so we only get real ad archive IDs.
            if not results:
                ad_ids_from_css = await _extract_ad_ids_from_rendered_page(page, exclude_id=page_id)
                logger.info(f"[page_scraper] CSS selector found {len(ad_ids_from_css)} ad IDs (page_id excluded)")

                if ad_ids_from_css:
                    css_ads = [
                        {
                            "keyword": fb_page_url,
                            "country": country,
                            "advertiser_name": "",
                            "ad_library_url": f"https://www.facebook.com/ads/library/?id={aid}",
                            "landing_page_url": "",
                            "ad_text": "",
                            "media_type": "unknown",
                            "media_url": "",
                            "thumbnail_url": "",
                            "extracted_product_name": "",
                            "normalized_product_name": "",
                            "main_image_url": "",
                            "page_title": "",
                            "duplicate_group_id": "",
                            "duplicates_count": 1,
                            "active_status": "unknown",
                            "status": "NEW",
                            "created_at": datetime.utcnow().isoformat(),
                        }
                        for aid in ad_ids_from_css
                    ]
                    if progress_callback:
                        await progress_callback(
                            f"🔗 Resolving landing pages for {len(css_ads)} ads…"
                        )
                    css_ads = await _resolve_landing_pages(context, css_ads)
                    results.extend(css_ads)

            # ── Method 3: raw HTML regex fallback (also page_id-safe) ─────
            if not results:
                html = await page.content()
                seen_ids: set[str] = set()
                raw_ads = []
                for pattern in [
                    r'"ad_archive_id"\s*:\s*"?(\d+)"?',
                    # Only match ?id= as a standalone query param, NOT as a suffix of view_all_page_id
                    r'[?&]id=(\d{10,})',
                ]:
                    for m in re.finditer(pattern, html):
                        aid = m.group(1)
                        if aid != page_id and aid not in seen_ids:
                            seen_ids.add(aid)
                            raw_ads.append(aid)
                logger.info(f"[page_scraper] HTML regex found {len(raw_ads)} ad IDs (page_id excluded)")
                if raw_ads:
                    html_ads = [
                        {
                            "keyword": fb_page_url,
                            "country": country,
                            "advertiser_name": "",
                            "ad_library_url": f"https://www.facebook.com/ads/library/?id={aid}",
                            "landing_page_url": "",
                            "ad_text": "",
                            "media_type": "unknown",
                            "media_url": "",
                            "thumbnail_url": "",
                            "extracted_product_name": "",
                            "normalized_product_name": "",
                            "main_image_url": "",
                            "page_title": "",
                            "duplicate_group_id": "",
                            "duplicates_count": 1,
                            "active_status": "unknown",
                            "status": "NEW",
                            "created_at": datetime.utcnow().isoformat(),
                        }
                        for aid in raw_ads[:MAX_ADS_TO_SCAN_PER_KEYWORD]
                    ]
                    if progress_callback:
                        await progress_callback(f"🔗 Resolving landing pages for {len(html_ads)} ads…")
                    html_ads = await _resolve_landing_pages(context, html_ads)
                    results.extend(html_ads)

            if not results:
                try:
                    page_title = await page.title()
                    page_text = (await page.inner_text("body"))[:400].replace("\n", " ")
                    logger.info(f"[page_scraper] 0 ads — title: '{page_title}'")
                    logger.warning(f"[page_scraper] snippet: {page_text[:200]}")
                except Exception:
                    pass

        except PlaywrightTimeout:
            logger.warning(f"[page_scraper] Timeout for page {fb_page_url}")
        except Exception as e:
            logger.warning(f"[page_scraper] Error: {e}")
        else:
            # Successful scrape — persist the refreshed session for next run
            await fb_auth.save_auth_state(context)
        finally:
            await fb_auth.close_browser_context(browser, context)

    filtered = [ad for ad in results if _matches_filter(ad, media_type_filter, active_filter)]
    return filtered[:MAX_ADS_TO_SCAN_PER_KEYWORD]


async def _extract_ad_ids_from_rendered_page(page: Page, exclude_id: str = "") -> list[str]:
    """
    Use CSS selectors to find all individual ad links on a rendered Ads Library page.
    Much more reliable than regex on raw HTML because it only sees DOM-visible links.
    Excludes `exclude_id` (the page ID) to avoid false matches.
    """
    ad_ids: list[str] = []
    seen: set[str] = set()

    try:
        # Primary: look for links that go to individual ad pages (?id=XXXX)
        links = await page.query_selector_all('a[href*="ads/library/?id="], a[href*="ads/library?id="]')
        for link in links:
            try:
                href = await link.get_attribute("href") or ""
                m = re.search(r'[?&]id=(\d+)', href)
                if m:
                    aid = m.group(1)
                    if aid != exclude_id and aid not in seen:
                        ad_ids.append(aid)
                        seen.add(aid)
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"[page_scraper] CSS selector error: {e}")

    if not ad_ids:
        # Fallback: search the full page text for ad_archive_id JSON values
        try:
            page_text = await page.evaluate("() => document.body.innerText")
            for m in re.finditer(r'"ad_archive_id"\s*:\s*"?(\d+)"?', page_text):
                aid = m.group(1)
                if aid != exclude_id and aid not in seen:
                    ad_ids.append(aid)
                    seen.add(aid)
        except Exception:
            pass

    logger.info(f"[page_scraper] _extract_ad_ids_from_rendered_page → {len(ad_ids)} unique ad IDs")
    return ad_ids


def _extract_json_field(html: str, field: str) -> str:
    """
    Extract a single string value from Facebook's embedded JSON blob.
    Handles both escaped (\\/...) and unescaped (https://...) URLs.
    Returns the first match or "" if not found.
    """
    pattern = rf'"{re.escape(field)}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"'
    m = re.search(pattern, html)
    if m:
        raw = m.group(1).replace("\\/", "/")
        if raw.startswith("http"):
            return raw
    return ""


def _country_to_code(country: str) -> str:
    mapping = {
        "united states": "US", "us": "US", "usa": "US",
        "united kingdom": "GB", "uk": "GB", "gb": "GB",
        "france": "FR", "fr": "FR", "germany": "DE", "de": "DE",
        "italy": "IT", "it": "IT", "spain": "ES", "es": "ES",
        "canada": "CA", "ca": "CA", "australia": "AU", "au": "AU",
        "netherlands": "NL", "nl": "NL", "belgium": "BE", "be": "BE",
        "all": "ALL",
    }
    code = mapping.get(country.strip().lower(), country.strip().upper())
    return code if (len(code) == 2 or code == "ALL") else "US"


def _extract_product_name(ad_text: str, title: str, page_name: str) -> str:
    if title and len(title) > 3:
        return title[:80]
    if ad_text:
        first = ad_text.split("\n")[0].strip()
        if len(first) > 3:
            return first[:80]
        return ad_text[:60]
    return page_name[:60] if page_name else ""


def _dig(obj: dict, *keys):
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _matches_filter(ad: dict, media_type_filter: str, active_filter: str) -> bool:
    mt = ad["media_type"]
    # When a specific media type is requested, pass confirmed matches AND unknowns
    # (HTML fallback can't detect media type, so "unknown" gets benefit of the doubt).
    if media_type_filter == "video":
        if mt == "image":
            return False
    elif media_type_filter == "image":
        if mt == "video":
            return False
    # "both" passes every media type including "unknown"

    # Reject videos longer than 3 minutes (180 seconds) when duration is known from GraphQL
    if mt == "video":
        duration = ad.get("video_duration") or 0
        if duration > MAX_HTML_VIDEO_SECONDS:
            logger.debug(f"[filter] Skipping video — duration {duration}s > {MAX_HTML_VIDEO_SECONDS}s")
            return False

    # "unknown" active status also passes all active filters
    ast = ad["active_status"]
    if ast != "unknown":
        if active_filter == "active" and ast == "inactive":
            return False
        if active_filter == "inactive" and ast == "active":
            return False
    return True


