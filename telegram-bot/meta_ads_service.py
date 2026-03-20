"""
meta_ads_service.py - Meta Marketing API integration for the Ads Launch bot.

Handles: media download/upload, campaign/adset/creative/ad creation, scheduling.
"""

import logging
import os
import re
import sys
import json
import mimetypes
import tempfile
import time

import httpx

sys.path.insert(0, os.path.dirname(__file__))

logger = logging.getLogger(__name__)

META_API_VERSION = "v21.0"
GRAPH_URL        = f"https://graph.facebook.com/{META_API_VERSION}"
VIDEO_GRAPH_URL  = f"https://graph-video.facebook.com/{META_API_VERSION}"

_BLOCKED_DOMAINS = {"metastatus.com", "www.metastatus.com", "ads.metastatus.com"}

_DL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _fb_cookie_header() -> dict:
    """
    Build a Cookie header from fb_auth_state.json (preferred — updated by scraper)
    or FACEBOOK_COOKIES env var (fallback).
    Returns empty dict if neither is available.
    """
    # Prefer the saved browser storage state — it's refreshed after every scrape session
    state_path = os.path.join(os.path.dirname(__file__), "fb_auth_state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                state = json.load(f)
            cookies = state.get("cookies", [])
            if cookies:
                pairs = [f"{c['name']}={c['value']}" for c in cookies if c.get("name") and c.get("value")]
                if pairs:
                    return {"Cookie": "; ".join(pairs)}
        except Exception:
            pass
    # Fallback: raw cookie string from environment variable
    raw = os.environ.get("FACEBOOK_COOKIES", "").strip()
    if raw:
        return {"Cookie": raw}
    return {}


def _extract_html_field(html: str, field: str) -> str:
    """Extract a URL value from Facebook's embedded JSON in a page's HTML source."""
    pattern = rf'"{re.escape(field)}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"'
    m = re.search(pattern, html)
    if m:
        raw = m.group(1).replace("\\/", "/")
        if raw.startswith("http"):
            return raw
    return ""


def resolve_library_url(library_url: str) -> str:
    """
    Fetch a Facebook Ads Library page and extract the real CDN media URL from its HTML.
    Returns the CDN URL string, or "" if extraction fails.
    """
    headers = {
        **_DL_HEADERS,
        **_fb_cookie_header(),
        "Referer":        "https://www.facebook.com/",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
    }
    try:
        resp = httpx.get(library_url, headers=headers, timeout=30, follow_redirects=True)
        if resp.status_code != 200:
            logger.warning(f"[meta] resolve_library_url: HTTP {resp.status_code} for {library_url}")
            return ""
        html = resp.text
        # Try video first (HD → SD), then image (original → resized)
        cdn_url = (
            _extract_html_field(html, "video_hd_url")
            or _extract_html_field(html, "video_sd_url")
            or _extract_html_field(html, "original_image_url")
            or _extract_html_field(html, "resized_image_url")
        )
        if cdn_url:
            logger.info(f"[meta] Resolved library URL → {cdn_url[:80]}")
        else:
            logger.warning(f"[meta] resolve_library_url: no CDN URL found in page source for {library_url}")
        return cdn_url
    except Exception as e:
        logger.error(f"[meta] resolve_library_url failed: {e}")
        return ""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _token() -> str:
    return os.environ.get("META_ACCESS_TOKEN", "")


def _get(path: str, params: dict | None = None) -> dict:
    p = {"access_token": _token()}
    if params:
        p.update(params)
    r = httpx.get(f"{GRAPH_URL}/{path}", params=p, timeout=30)
    if not r.is_success:
        try:
            body = r.json()
            err = body.get("error", {})
            logger.error(f"[meta] GET /{path} HTTP {r.status_code} — code={err.get('code')} subcode={err.get('error_subcode')} msg={err.get('message')} type={err.get('type')}")
        except Exception:
            logger.error(f"[meta] GET /{path} HTTP {r.status_code} — body={r.text[:300]}")
        r.raise_for_status()
    result = r.json()
    if "error" in result:
        err = result["error"]
        msg = err.get("message", str(err))
        subcode = err.get("error_subcode", "")
        logger.error(f"[meta] GET /{path} API error — code={err.get('code')} subcode={subcode} msg={msg}")
        raise RuntimeError(f"Meta API error: {msg}")
    return result


def _post(path: str, data: dict, files=None, base: str | None = None) -> dict:
    data = dict(data)
    data["access_token"] = _token()
    url = f"{base or GRAPH_URL}/{path}"
    if files:
        r = httpx.post(url, data=data, files=files, timeout=180)
    else:
        r = httpx.post(url, data=data, timeout=60)
    try:
        result = r.json()
    except Exception:
        r.raise_for_status()
        raise
    if "error" in result:
        err = result["error"]
        msg = err.get("message", str(err))
        subcode = err.get("error_subcode", "")
        user_msg = err.get("error_user_msg", "")
        logger.error(f"[meta] API error — code={err.get('code')} subcode={subcode} msg={msg} user_msg={user_msg}")
        raise RuntimeError(f"Meta API error: {msg}")
    return result


# ── Credential validation ─────────────────────────────────────────────────────

def validate_credentials() -> tuple[bool, str]:
    tok = _token()
    if not tok:
        return False, "META_ACCESS_TOKEN is not set"
    try:
        info = _get("me", {"fields": "id,name"})
        return True, f"Token valid — authenticated as: {info.get('name', '?')} (id={info.get('id', '?')})"
    except Exception as e:
        return False, str(e)


# ── Media download ────────────────────────────────────────────────────────────

def download_media(url: str) -> tuple[str | None, str]:
    """
    Download media from url.
    Returns (local_temp_path, content_type) on success, (None, "") on failure.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)

    # Ads Library page URLs are HTML pages, not direct media.
    # Auto-resolve by fetching the page and extracting the embedded CDN URL.
    if parsed.hostname in ("www.facebook.com", "facebook.com") and "/ads/library" in (parsed.path or ""):
        logger.info(f"[meta] Ads Library page URL detected — attempting CDN resolve: {url}")
        cdn_url = resolve_library_url(url)
        if not cdn_url:
            logger.warning(f"[meta] Could not resolve CDN URL from library page: {url}")
            return None, "ads_library_page"
        # Recurse with the real CDN URL
        return download_media(cdn_url)

    if parsed.hostname in _BLOCKED_DOMAINS:
        logger.warning(f"[meta] Blocked domain: {parsed.hostname}")
        return None, ""

    try:
        with httpx.stream(
            "GET", url, headers=_DL_HEADERS, timeout=60, follow_redirects=True
        ) as resp:
            if resp.status_code != 200:
                logger.warning(f"[meta] Download {url} status={resp.status_code}")
                return None, ""

            final_host = urlparse(str(resp.url)).hostname or ""
            if final_host in _BLOCKED_DOMAINS:
                logger.warning(f"[meta] Redirect to blocked domain: {final_host}")
                return None, ""

            ctype = resp.headers.get("content-type", "").split(";")[0].strip()
            if not ctype:
                ctype = "application/octet-stream"

            ext = mimetypes.guess_extension(ctype) or ".bin"
            if ext in (".jpe", ".jpeg"):
                ext = ".jpg"

            tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
            for chunk in resp.iter_bytes(chunk_size=65536):
                tmp.write(chunk)
            tmp.close()

            size = os.path.getsize(tmp.name)
            if size < 2000:
                os.unlink(tmp.name)
                logger.warning(f"[meta] File too small ({size} bytes), likely error page: {url}")
                return None, ""

            logger.info(f"[meta] Downloaded {url} → {tmp.name} ({size} bytes, {ctype})")
            return tmp.name, ctype

    except Exception as e:
        logger.error(f"[meta] download_media failed: {e}")
        return None, ""


# ── Media upload ─────────────────────────────────────────────────────────────

def upload_image(ad_account_id: str, file_path: str) -> str | None:
    """Upload image. Returns hash or None."""
    try:
        with open(file_path, "rb") as f:
            fname = os.path.basename(file_path)
            result = _post(
                f"{ad_account_id}/adimages",
                data={},
                files={"filename": (fname, f, "image/jpeg")},
            )
        for _key, val in result.get("images", {}).items():
            h = val.get("hash")
            if h:
                logger.info(f"[meta] Uploaded image hash={h}")
                return h
        logger.error(f"[meta] upload_image: unexpected response {result}")
        return None
    except Exception as e:
        logger.error(f"[meta] upload_image failed: {e}")
        return None


def upload_video(ad_account_id: str, file_path: str) -> str | None:
    """Upload video. Returns video_id or None."""
    try:
        with open(file_path, "rb") as f:
            fname = os.path.basename(file_path)
            result = _post(
                f"{ad_account_id}/advideos",
                data={"title": fname},
                files={"source": (fname, f, "video/mp4")},
                base=VIDEO_GRAPH_URL,
            )
        vid = result.get("id")
        if vid:
            logger.info(f"[meta] Uploaded video id={vid}")
            wait_for_video_ready(vid)
        return vid
    except Exception as e:
        logger.error(f"[meta] upload_video failed: {e}")
        return None


def wait_for_video_ready(video_id: str, timeout: int = 120, interval: int = 5) -> bool:
    """Poll video status until ready or timeout. Returns True if ready."""
    token = _token()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(
                f"{GRAPH_URL}/{video_id}",
                params={"fields": "status", "access_token": token},
                timeout=15,
            )
            status_obj = resp.json().get("status", {})
            video_status = status_obj.get("video_status", "")
            logger.info(f"[meta] Video {video_id} status={video_status}")
            if video_status == "ready":
                return True
            if video_status == "error":
                logger.error(f"[meta] Video {video_id} processing failed")
                return False
        except Exception as e:
            logger.warning(f"[meta] wait_for_video_ready poll error: {e}")
        time.sleep(interval)
    logger.warning(f"[meta] Video {video_id} not ready after {timeout}s")
    return False


def get_video_thumbnail_url(video_id: str) -> str | None:
    """Fetch the preferred thumbnail URL for an uploaded video."""
    try:
        token = _token()
        resp = httpx.get(
            f"{GRAPH_URL}/{video_id}",
            params={"fields": "thumbnails", "access_token": token},
            timeout=15,
        )
        data = resp.json()
        thumbs = data.get("thumbnails", {}).get("data", [])
        preferred = next((t for t in thumbs if t.get("is_preferred")), None)
        if preferred:
            return preferred.get("uri")
        if thumbs:
            return thumbs[0].get("uri")
    except Exception as e:
        logger.warning(f"[meta] get_video_thumbnail_url failed for {video_id}: {e}")
    return None


def prepare_media_assets(ad_account_id: str, media_urls: list[str]) -> list[dict]:
    """
    Download and upload each media URL.
    Returns list of dicts:
      success → {"url": ..., "type": "image"|"video", "hash": ...|None, "video_id": ...|None}
      failure → {"url": ..., "error": "reason"}
    """
    assets = []
    for url in media_urls:
        path, ctype = download_media(url)
        if not path:
            if ctype == "ads_library_page":
                assets.append({"url": url, "error": "Could not extract media from Ads Library page (cookies may be expired)"})
            else:
                assets.append({"url": url, "error": "Download failed or blocked"})
            continue

        is_video = ctype.startswith("video/")
        try:
            if is_video:
                vid = upload_video(ad_account_id, path)
                if vid:
                    thumb = get_video_thumbnail_url(vid)
                    thumb_hash = None
                    if thumb:
                        thumb_path, _ = download_media(thumb)
                        if thumb_path:
                            try:
                                thumb_hash = upload_image(ad_account_id, thumb_path)
                                logger.info(f"[meta] Uploaded video thumbnail hash={thumb_hash}")
                            finally:
                                try:
                                    os.unlink(thumb_path)
                                except Exception:
                                    pass
                    assets.append({"url": url, "type": "video", "video_id": vid, "hash": None, "thumbnail_url": thumb, "thumbnail_hash": thumb_hash})
                else:
                    assets.append({"url": url, "error": "Video upload failed"})
            else:
                h = upload_image(ad_account_id, path)
                if h:
                    assets.append({"url": url, "type": "image", "hash": h, "video_id": None})
                else:
                    assets.append({"url": url, "error": "Image upload failed"})
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    return assets


# ── Campaign creation ─────────────────────────────────────────────────────────

def create_campaign(ad_account_id: str, name: str, objective: str, daily_budget_cents: int = 0) -> str:
    data: dict = {
        "name":                                name,
        "objective":                           objective,
        "status":                              "ACTIVE",
        "special_ad_categories":               json.dumps([]),
        "campaign_budget_optimization":        "true",
        "bid_strategy":                        "LOWEST_COST_WITHOUT_CAP",
    }
    if daily_budget_cents > 0:
        data["daily_budget"] = str(daily_budget_cents)
    result = _post(f"{ad_account_id}/campaigns", data)
    cid = result.get("id")
    if not cid:
        raise RuntimeError(f"No campaign id in response: {result}")
    logger.info(f"[meta] Created campaign id={cid} name={name!r}")
    return cid


def create_adset(
    ad_account_id:    str,
    campaign_id:      str,
    name:             str,
    country:          str,
    pixel_id:         str,
    conversion_event: str,
    start_time_iso:   str | None = None,
    is_dynamic:       bool = False,
) -> str:
    targeting = json.dumps({
        "geo_locations": {"countries": [country]},
        "age_min": 18,
        "age_max": 65,
    })
    data: dict = {
        "name":              name,
        "campaign_id":       campaign_id,
        "billing_event":     "IMPRESSIONS",
        "optimization_goal": "OFFSITE_CONVERSIONS",
        "targeting":         targeting,
        "status":            "ACTIVE",
        "promoted_object":   json.dumps({
            "pixel_id":          pixel_id,
            "custom_event_type": conversion_event.upper(),
        }),
    }
    if is_dynamic:
        data["is_dynamic"] = "1"
    if start_time_iso:
        data["start_time"] = start_time_iso

    result = _post(f"{ad_account_id}/adsets", data)
    aid = result.get("id")
    if not aid:
        raise RuntimeError(f"No adset id in response: {result}")
    logger.info(f"[meta] Created adset id={aid} name={name!r}")
    return aid


def create_creative_single(
    ad_account_id: str,
    name:          str,
    page_id:       str,
    asset:         dict,
    landing_url:   str,
    primary_texts,
    headlines,
    cta:           str,
) -> str:
    """Single image or video creative. Accepts str or list for texts/headlines (uses first of each)."""
    primary_text = primary_texts[0] if isinstance(primary_texts, list) else primary_texts
    headline     = headlines[0]     if isinstance(headlines,     list) else headlines
    cta_value = json.dumps({"type": cta, "value": {"link": landing_url}})

    if asset.get("type") == "video":
        video_data = {
            "video_id":       asset["video_id"],
            "message":        primary_text,
            "title":          headline,
            "call_to_action": cta_value,
        }
        if asset.get("thumbnail_url"):
            video_data["image_url"] = asset["thumbnail_url"]
        story = {"page_id": page_id, "video_data": video_data}
    else:
        link_data = {
            "image_hash":     asset["hash"],
            "link":           landing_url,
            "message":        primary_text,
            "name":           headline,
            "call_to_action": cta_value,
        }
        story = {"page_id": page_id, "link_data": link_data}

    result = _post(f"{ad_account_id}/adcreatives", {
        "name":               name,
        "object_story_spec":  json.dumps(story),
    })
    cid = result.get("id")
    if not cid:
        raise RuntimeError(f"No creative id in response: {result}")
    logger.info(f"[meta] Created single creative id={cid}")
    return cid


def create_creative_flexible(
    ad_account_id: str,
    name:          str,
    page_id:       str,
    assets:        list[dict],
    landing_url:   str,
    primary_texts,
    headlines,
    cta:           str,
) -> str:
    """Dynamic/flexible creative with multiple images or videos. Accepts str or list for texts/headlines."""
    texts_list = primary_texts if isinstance(primary_texts, list) else [primary_texts]
    heads_list = headlines     if isinstance(headlines,     list) else [headlines]
    images = [{"hash": a["hash"]}     for a in assets if a.get("type") == "image"]
    videos = [
        {
            "video_id": a["video_id"],
            **({"thumbnail_hash": a["thumbnail_hash"]} if a.get("thumbnail_hash") else {}),
        }
        for a in assets if a.get("type") == "video"
    ]

    feed: dict = {
        "bodies":              [{"text": t} for t in texts_list],
        "titles":              [{"text": h} for h in heads_list],
        "link_urls":           [{"website_url": landing_url, "display_url": landing_url}],
        "call_to_action_types": [cta],
    }
    if images and videos:
        feed["images"]     = images
        feed["videos"]     = videos
        feed["ad_formats"] = ["SINGLE_IMAGE", "SINGLE_VIDEO"]
    elif videos:
        feed["videos"]     = videos
        feed["ad_formats"] = ["SINGLE_VIDEO"]
    else:
        feed["images"]     = images
        feed["ad_formats"] = ["SINGLE_IMAGE"]

    logger.info(f"[meta] create_creative_flexible feed={json.dumps(feed)[:500]}")
    result = _post(f"{ad_account_id}/adcreatives", {
        "name":              name,
        "object_story_spec": json.dumps({"page_id": page_id}),
        "asset_feed_spec":   json.dumps(feed),
    })
    cid = result.get("id")
    if not cid:
        raise RuntimeError(f"No creative id in response: {result}")
    logger.info(f"[meta] Created flexible creative id={cid}")
    return cid


def create_ad(
    ad_account_id: str,
    adset_id:      str,
    name:          str,
    creative_id:   str,
) -> str:
    result = _post(f"{ad_account_id}/ads", {
        "name":      name,
        "adset_id":  adset_id,
        "creative":  json.dumps({"creative_id": creative_id}),
        "status":    "ACTIVE",
    })
    aid = result.get("id")
    if not aid:
        raise RuntimeError(f"No ad id in response: {result}")
    logger.info(f"[meta] Created ad id={aid}")
    return aid


def pause_campaign(campaign_id: str) -> bool:
    try:
        _post(campaign_id, {"status": "PAUSED"})
        logger.info(f"[meta] Paused campaign {campaign_id}")
        return True
    except Exception as e:
        logger.error(f"[meta] pause_campaign failed: {e}")
        return False


def force_stop_campaign(campaign_id: str) -> bool:
    """
    Pauses a campaign at every level (campaign → ad sets → ads).
    Returns True if the campaign-level pause succeeded.
    Ad set / ad level failures are logged but do not affect the return value.
    """
    ok = pause_campaign(campaign_id)

    # Pause every ad set inside the campaign
    try:
        adsets = _get(f"{campaign_id}/adsets", {"fields": "id,status", "limit": "100"})
        for adset in adsets.get("data", []):
            asid = adset.get("id")
            if not asid:
                continue
            try:
                _post(asid, {"status": "PAUSED"})
                logger.info(f"[meta] Paused ad set {asid}")
            except Exception as e:
                logger.warning(f"[meta] Could not pause ad set {asid}: {e}")

            # Pause every ad inside this ad set
            try:
                ads = _get(f"{asid}/ads", {"fields": "id,status", "limit": "100"})
                for ad in ads.get("data", []):
                    adid = ad.get("id")
                    if not adid:
                        continue
                    try:
                        _post(adid, {"status": "PAUSED"})
                        logger.info(f"[meta] Paused ad {adid}")
                    except Exception as e:
                        logger.warning(f"[meta] Could not pause ad {adid}: {e}")
            except Exception as e:
                logger.warning(f"[meta] Could not fetch ads for ad set {asid}: {e}")
    except Exception as e:
        logger.warning(f"[meta] Could not fetch ad sets for campaign {campaign_id}: {e}")

    return ok


# ── Scheduling helper ─────────────────────────────────────────────────────────

def today_at_2359_iso(timezone_name: str) -> str:
    """Return today 23:59:00 in the given timezone as ISO 8601 string."""
    try:
        from datetime import datetime, time as dtime
        import zoneinfo
        tz     = zoneinfo.ZoneInfo(timezone_name)
        now    = datetime.now(tz)
        target = datetime.combine(now.date(), dtime(23, 59, 0), tzinfo=tz)
        return target.isoformat()
    except Exception as e:
        logger.error(f"[meta] today_at_2359_iso({timezone_name}) failed: {e}")
        return ""


# ── Account discovery ────────────────────────────────────────────────────────

def fetch_ad_accounts() -> list[dict]:
    """
    Return all ad accounts the token has access to.
    Each dict: {id, name, currency, timezone_name}
    """
    result = _get("me/adaccounts", {
        "fields": "id,name,currency,timezone_name",
        "limit":  "100",
    })
    return result.get("data", [])


def fetch_pages() -> list[dict]:
    """
    Return all Facebook Pages the token user manages.
    Each dict: {id, name}
    """
    result = _get("me/accounts", {
        "fields": "id,name",
        "limit":  "100",
    })
    return result.get("data", [])


def fetch_pixels(ad_account_id: str) -> list[dict]:
    """
    Return all pixels linked to the given ad account.
    Each dict: {id, name}
    """
    result = _get(f"{ad_account_id}/adspixels", {
        "fields": "id,name",
        "limit":  "100",
    })
    return result.get("data", [])


def fetch_instagram_accounts(page_id: str) -> list[dict]:
    """
    Return Instagram accounts linked to the given Facebook Page.
    Each dict: {id, username}
    """
    try:
        result = _get(f"{page_id}/instagram_accounts", {
            "fields": "id,username",
            "limit":  "20",
        })
        return result.get("data", [])
    except Exception:
        return []


# ── Delivery status ───────────────────────────────────────────────────────────

def get_delivery_status(campaign_id: str) -> dict:
    """
    Fetch effective_status for campaign, all ad sets, and all ads.
    Returns:
      {
        "campaign":  {"id": ..., "effective_status": ..., "status": ...},
        "adsets":    [{"id": ..., "name": ..., "effective_status": ..., "status": ...}, ...],
        "ads":       [{"id": ..., "name": ..., "effective_status": ..., "status": ...}, ...],
      }
    """
    try:
        fields = "effective_status,status"
        logger.info(f"[meta] get_delivery_status: fetching campaign {campaign_id}")
        camp = _get(campaign_id, {"fields": fields})
        logger.info(f"[meta] get_delivery_status campaign raw: {camp}")
        adsets_raw = _get(f"{campaign_id}/adsets", {"fields": fields + ",name,start_time", "limit": "50"})
        logger.info(f"[meta] get_delivery_status adsets raw: {adsets_raw}")
        ads_raw    = _get(f"{campaign_id}/ads",    {"fields": fields + ",name", "limit": "50"})
        logger.info(f"[meta] get_delivery_status ads raw: {ads_raw}")

        # If all adsets have a future start_time and status=ACTIVE, the campaign
        # is scheduled — Meta API still returns effective_status=ACTIVE in this case
        # but FB UI correctly shows "Programmé". Detect it here.
        from datetime import datetime, timezone as _tz
        now_utc = datetime.now(_tz.utc)
        def _resolve_status(eff: str, status: str, start_time_str: str | None = None) -> str:
            if eff.upper() == "ACTIVE" and start_time_str:
                try:
                    from datetime import datetime
                    import re
                    # parse ISO 8601 with optional +HH:MM offset
                    st = datetime.fromisoformat(re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", start_time_str))
                    if st.tzinfo is None:
                        st = st.replace(tzinfo=_tz.utc)
                    if st > now_utc:
                        return "SCHEDULED"
                except Exception:
                    pass
            return eff

        result = {
            "campaign": {
                "id":               camp.get("id", campaign_id),
                "effective_status": camp.get("effective_status", "UNKNOWN"),
                "status":           camp.get("status", "UNKNOWN"),
            },
            "adsets": [
                {
                    "id":               a.get("id"),
                    "name":             a.get("name", ""),
                    "effective_status": _resolve_status(
                        a.get("effective_status", "UNKNOWN"),
                        a.get("status", "UNKNOWN"),
                        a.get("start_time"),
                    ),
                    "status":           a.get("status", "UNKNOWN"),
                    "start_time":       a.get("start_time"),
                }
                for a in adsets_raw.get("data", [])
            ],
            "ads": [
                {
                    "id":               a.get("id"),
                    "name":             a.get("name", ""),
                    "effective_status": a.get("effective_status", "UNKNOWN"),
                    "status":           a.get("status", "UNKNOWN"),
                }
                for a in ads_raw.get("data", [])
            ],
        }
        # If campaign shows ACTIVE but all adsets are SCHEDULED → campaign is also scheduled
        if result["campaign"]["effective_status"].upper() == "ACTIVE" and result["adsets"]:
            if all(a["effective_status"].upper() == "SCHEDULED" for a in result["adsets"]):
                result["campaign"]["effective_status"] = "SCHEDULED"
        logger.info(f"[meta] get_delivery_status result: {result}")
        return result
    except Exception as e:
        logger.error(f"[meta] get_delivery_status failed for {campaign_id}: {e}", exc_info=True)
        return {}


# ── Insights ──────────────────────────────────────────────────────────────────

def get_campaign_insights(campaign_id: str) -> dict:
    """Fetch basic insights for a campaign. Returns dict or empty on failure."""
    fields = ",".join([
        "spend", "impressions", "cpm", "ctr",
        "clicks", "cpc", "actions", "action_values", "purchase_roas",
    ])
    try:
        result = _get(f"{campaign_id}/insights", {"fields": fields, "date_preset": "maximum"})
        data = result.get("data", [])
        return data[0] if data else {}
    except Exception as e:
        logger.error(f"[meta] get_campaign_insights failed: {e}")
        return {}
