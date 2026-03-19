"""
physical_classifier.py - Classify a product page as physical, digital, or unclear.

Validates that a discovered product is a real shippable physical product
before proceeding with creative discovery, clustering, and sheet writing.
"""

import logging
import re

logger = logging.getLogger(__name__)

# ── Digital product signals ──────────────────────────────────────────────────
_DIGITAL_PATTERNS = [
    # English
    r"\bebook\b", r"\bpdf\b", r"\bpdf guide\b",
    r"\bonline course\b", r"\bvideo course\b", r"\bonline program\b",
    r"\bcourse\b", r"\bcoaching\b", r"\bmasterclass\b",
    r"\bwebinar\b", r"\bworkshop\b", r"\bonline class\b",
    r"\bsubscription\b", r"\bmembership\b",
    r"\bsoftware\b", r"\bapplication\b",
    r"\bdigital download\b", r"\bdownload\b", r"\binstant access\b",
    r"\binstant download\b", r"\bdigital product\b",
    r"\btemplate\b", r"\bnotion template\b", r"\bcanva template\b",
    r"\bconsultation\b", r"\bprestation\b", r"\bservice\b",
    r"\bvirtual\b", r"\bstreaming\b", r"\baccess\b",
    # French
    r"\blivre numérique\b", r"\blivre numerique\b",
    r"\btéléchargement\b", r"\btelechargement\b",
    r"\baccès immédiat\b", r"\bacces immediat\b",
    r"\bformation\b", r"\bprogramme en ligne\b",
    r"\babonnement\b", r"\blogiciel\b",
    r"\bmodèle\b", r"\bmodele\b",
    r"\bmentorat\b", r"\baccompagnement\b",
    r"\bconseil\b", r"\baccès\b",
    # German
    r"\bonlinekurs\b", r"\bonline-kurs\b",
    r"\bdownload\b", r"\blizenz\b",
    r"\bmitgliedschaft\b", r"\babo\b",
    r"\bdigitaler download\b",
    # Spanish
    r"\bcurso online\b", r"\bdescarga\b",
    r"\bacceso inmediato\b", r"\bservicio\b",
    r"\bsuscripción\b", r"\bsuscripcion\b",
    # Italian
    r"\bcorso online\b", r"\bscaricare\b",
    r"\baccesso immediato\b", r"\babboname\b",
]

# ── Physical product signals ─────────────────────────────────────────────────
_PHYSICAL_PATTERNS = [
    # English
    r"\bfree shipping\b", r"\bshipping\b", r"\bdelivery\b",
    r"\bships?\b", r"\bordered?\b",
    r"\bweight\b", r"\bdimensions?\b", r"\bmaterial\b",
    r"\bcolor\b", r"\bcolour\b", r"\bsize\b", r"\bsizes?\b",
    r"\badd to (cart|bag)\b", r"\bbuy now\b",
    r"\bcheck\s?out\b", r"\bin stock\b",
    r"\bpackaging\b", r"\bwarranty\b",
    r"\btrack(ing)?\b", r"\btrack your order\b",
    r"\bfabric\b", r"\bstainless steel\b", r"\bwood\b",
    r"\bwaterproof\b", r"\bportable\b",
    # French
    r"\blivraison\b", r"\blivraison gratuite\b",
    r"\bexpédition\b", r"\bexpedition\b",
    r"\bajouter au panier\b", r"\bcommander\b",
    r"\bmatière\b", r"\bmatiere\b",
    r"\bdimensions?\b", r"\bpoids\b",
    r"\bcouleur\b", r"\btaille\b",
    r"\bstock\b", r"\ben stock\b", r"\bgarantie\b",
    r"\blivré\b", r"\blivre\b", r"\bcolis\b",
    r"\bsuivi\b", r"\bdélai\b", r"\bdelai\b",
    r"\binox\b", r"\bbois\b", r"\bcuir\b",
    r"\bimperméable\b", r"\bportatif\b", r"\bportatif\b",
    # German
    r"\bversand\b", r"\bkostenloser versand\b",
    r"\blieferung\b", r"\bin den warenkorb\b",
    r"\bgewicht\b", r"\bmaße\b", r"\bmasse\b",
    r"\bmaterial\b", r"\bfarbe\b", r"\bgröße\b", r"\bgroesse\b",
    r"\blagernd\b", r"\bauf lager\b",
    # Spanish
    r"\benvío\b", r"\benvio\b", r"\benvío gratis\b",
    r"\bentrega\b", r"\bagregar al carrito\b",
    r"\bmaterial\b", r"\btamaño\b", r"\btamano\b",
    r"\bcolor\b", r"\bpeso\b", r"\bgarantía\b",
    # Italian
    r"\bspedizione\b", r"\bconsegna\b",
    r"\baggiungi al carrello\b",
    r"\bmateriale\b", r"\bdimensioni\b", r"\bcolore\b",
    r"\bgaranzia\b", r"\bpeso\b",
]

_DIGITAL_RE  = [re.compile(p, re.IGNORECASE) for p in _DIGITAL_PATTERNS]
_PHYSICAL_RE = [re.compile(p, re.IGNORECASE) for p in _PHYSICAL_PATTERNS]


def classify_product(ad: dict) -> str:
    """
    Classify a product as 'physical', 'digital', or 'unclear'.

    Uses all available ad data: page content, ad body text, URL path, and
    the advertiser name. Falls back to 'physical' when there is no evidence
    either way — absence of digital signals is treated as physical.
    Returns one of: 'physical', 'digital', 'unclear'
    """
    from urllib.parse import urlparse, unquote

    title    = (ad.get("extracted_product_name") or ad.get("page_title") or "").lower()
    desc     = (ad.get("_page_description") or "").lower()
    bullets  = " ".join(ad.get("_page_bullets") or []).lower()
    headings = " ".join(ad.get("_page_headings") or []).lower()
    ad_text  = (ad.get("ad_text") or ad.get("ad_body") or "").lower()

    # Extract words from the landing page URL path — e.g.
    # /Attractive%20Chiffon%20Women's%20Dupatta/p/LKOT → "attractive chiffon women dupatta"
    url_path = ""
    raw_url = ad.get("landing_page_url", "") or ""
    if raw_url:
        try:
            path = urlparse(raw_url).path
            url_path = re.sub(r"[^a-z0-9 ]", " ", unquote(path).lower())
        except Exception:
            pass

    full_text = f"{title} {desc} {bullets} {headings} {ad_text} {url_path}"

    digital_score  = sum(1 for r in _DIGITAL_RE  if r.search(full_text))
    physical_score = sum(1 for r in _PHYSICAL_RE if r.search(full_text))

    logger.info(f"[physical_check] started — title='{title[:60]}'")
    logger.info(
        f"[physical_check] physical_signals={physical_score} "
        f"digital_signals={digital_score}"
    )

    # No content at all — can't detect digital, so treat as physical
    if physical_score == 0 and digital_score == 0:
        logger.info("[physical_check] => PHYSICAL (no signals either way — defaulting to physical)")
        return "physical"

    # Clear digital
    if digital_score >= 2 and physical_score == 0:
        reason = f"digital_signals={digital_score}, no physical signals"
        logger.info(f"[physical_check] => DIGITAL ({reason})")
        return "digital"

    if digital_score >= 3 and physical_score <= 1:
        reason = f"strong digital_signals={digital_score}, weak physical={physical_score}"
        logger.info(f"[physical_check] => DIGITAL ({reason})")
        return "digital"

    # Clear physical
    if physical_score >= 2:
        logger.info(f"[physical_check] => PHYSICAL (physical_signals={physical_score})")
        return "physical"

    if physical_score >= 1 and digital_score == 0:
        logger.info(f"[physical_check] => PHYSICAL (1 signal, no digital signals)")
        return "physical"

    logger.info(
        f"[physical_check] => UNCLEAR "
        f"(physical={physical_score}, digital={digital_score})"
    )
    return "unclear"


def filter_physical_ads(ads: list[dict]) -> tuple[list[dict], int, int]:
    """
    Filter a list of ads, keeping only those from physical product pages.

    Returns:
        (physical_ads, digital_count, unclear_count)
    """
    physical: list[dict] = []
    digital_count = 0
    unclear_count = 0

    # Classify per unique product domain (cache to avoid re-classifying same domain)
    from urllib.parse import urlparse
    domain_cache: dict[str, str] = {}

    for ad in ads:
        url = ad.get("landing_page_url", "")
        if not url:
            # No landing page — can't classify, include it (might cluster with others)
            physical.append(ad)
            continue

        try:
            domain = urlparse(url).netloc.lower().lstrip("www.")
        except Exception:
            domain = ""

        if domain and domain in domain_cache:
            classification = domain_cache[domain]
        else:
            classification = classify_product(ad)
            if domain:
                domain_cache[domain] = classification

        if classification == "physical":
            physical.append(ad)
        elif classification == "digital":
            digital_count += 1
            logger.info(f"[physical_check] rejected digital product: {url[:80]}")
        else:  # unclear — second pass
            # Re-check with a broader text including ad body and URL path
            from urllib.parse import unquote as _unquote
            title   = (ad.get("extracted_product_name") or ad.get("page_title") or "").lower()
            desc    = (ad.get("_page_description") or "").lower()
            ad_text = (ad.get("ad_text") or ad.get("ad_body") or "").lower()
            try:
                import re as _re
                from urllib.parse import urlparse as _urlparse
                _path = _urlparse(url).path
                url_path = _re.sub(r"[^a-z0-9 ]", " ", _unquote(_path).lower())
            except Exception:
                url_path = ""
            text  = f"{title} {desc} {ad_text} {url_path}"
            phys  = sum(1 for r in _PHYSICAL_RE if r.search(text))
            digit = sum(1 for r in _DIGITAL_RE  if r.search(text))
            if phys >= 1 or (phys == 0 and digit == 0):
                # Keep if any physical signal, or if no signals at all (benefit of doubt)
                physical.append(ad)
                reason = "weak physical signal" if phys >= 1 else "no signals — benefit of doubt"
                logger.info(f"[physical_check] UNCLEAR → KEPT ({reason}): {url[:60]}")
            else:
                unclear_count += 1
                logger.info(f"[physical_check] UNCLEAR → REJECTED: {url[:60]}")

    return physical, digital_count, unclear_count
