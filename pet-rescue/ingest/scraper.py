"""Scrapling-based multi-site scraper.

Fetches animal listings from shelter websites, writes one raw_listings doc per
animal.  Doc IDs are deterministic (md5 of source_org + animal_name) so
re-runs are idempotent.

Usage:
    python -m ingest.scraper [--once] [--site SITE] [--interval SECONDS]
"""

import argparse
import hashlib
import logging
import re
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from elasticsearch import helpers

load_dotenv()

from backend.es_client import get_es  # noqa: E402  (after load_dotenv)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ingest.scraper")

# ---------------------------------------------------------------------------
# Charlie's Animal Rescue Centre (CARE)
# ---------------------------------------------------------------------------

_PAGE_URL = "https://www.charlies-care.com/passive-adoption"
_ORG_NAME = "Charlie's Animal Rescue"
_CARD_SELECTOR = ".wixui-repeater__item"
_USER_AGENT = "PetRescueCoordinator/0.1 (hackathon project; contact harshwardhanfartale.nith@gmail.com)"

# ---------------------------------------------------------------------------
# CUPA Bengaluru
# ---------------------------------------------------------------------------

_CUPA_URL = "https://cupabangalore.org/passive-adoption/"
_CUPA_ORG = "CUPA Bengaluru"
_CUPA_ANIMAL_PREFIXES = ("dog", "cat", "cow", "bull", "goat", "horse", "buffalo", "rabbit", "bird")


def _strip_wix_transforms(url: str) -> str:
    """Return base Wix media URL without resize parameters."""
    # e.g. https://static.wixstatic.com/media/abc~mv2.jpg/v1/fill/w_147,...
    #   → https://static.wixstatic.com/media/abc~mv2.jpg
    return re.sub(r"/v1/fill/.*", "", url)


def scrape_charlies() -> list[dict]:
    """Fetch and extract animal cards from Charlie's Animal Rescue."""
    from scrapling.fetchers import Fetcher  # local import keeps startup fast

    logger.info("scraping charlies (%s)", _PAGE_URL)
    try:
        f = Fetcher()
        page = f.get(_PAGE_URL, stealthy_headers=True)
    except Exception:
        logger.exception("failed to fetch charlies page")
        return []

    items = page.css(_CARD_SELECTOR)
    logger.info("found %d repeater items on charlies page", len(items))

    results: list[dict] = []
    for item in items:
        # Only process cards that have an h2 heading (the animal name).
        name_els = item.css("h2::text").getall()
        if not name_els:
            continue
        name = " ".join(name_els).strip()
        if not name:
            continue

        try:
            all_texts = item.css("*::text").getall()
            # Build description: everything except the name itself and the CTA.
            desc_parts = [
                t.strip()
                for t in all_texts
                if t.strip() and t.strip() != name and t.strip().lower() != "adopt now"
            ]
            description = " ".join(desc_parts)

            if len(description) < 50:
                logger.warning("skipping '%s' — description too short (%d chars)", name, len(description))
                continue

            photo_raw = item.css("img::attr(src)").get() or ""
            photo_url = _strip_wix_transforms(photo_raw) if photo_raw else ""

            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            animal_url = f"{_PAGE_URL}#{slug}"

            results.append(
                {
                    "animal_name": name,
                    "body": f"{name}. {description}",
                    "photo_url": photo_url,
                    "url": animal_url,
                }
            )
        except Exception:
            logger.warning("error extracting card for '%s', skipping", name, exc_info=True)

    return results


def _cupa_name_from_slug(slug: str) -> str:
    """'dog-gulabi' → 'Dog – Gulabi'"""
    parts = slug.split("-", 1)
    if len(parts) == 2:
        return f"{parts[0].capitalize()} – {parts[1].replace('-', ' ').title()}"
    return slug.replace("-", " ").title()


def scrape_cupa() -> list[dict]:
    """Fetch animal listings from CUPA Bengaluru passive adoption page."""
    from scrapling.fetchers import Fetcher  # local import keeps startup fast

    logger.info("scraping CUPA (%s)", _CUPA_URL)
    try:
        page = Fetcher().get(_CUPA_URL, stealthy_headers=True)
    except Exception:
        logger.exception("failed to fetch CUPA page")
        return []

    # Collect all unique /donation/ links — one per animal on the page
    seen_hrefs: set[str] = set()
    link_elements: list[tuple[str, object]] = []
    for a in page.css("a[href*='/donation/']"):
        href = (a.attrib.get("href") or "").rstrip("/")
        if href and href not in seen_hrefs:
            seen_hrefs.add(href)
            link_elements.append((href, a))

    logger.info("CUPA: found %d donation links", len(link_elements))

    results: list[dict] = []
    seen_names: set[str] = set()

    for href, a_el in link_elements:
        slug = href.rstrip("/").split("/")[-1]
        name = ""
        img_src = ""
        description = ""

        # Walk up ancestor divs to find the block that owns this link.
        # CUPA uses WordPress + Elementor so containers are nested divs.
        # Try progressively broader ancestors until we find one with a heading.
        for xpath_expr in (
            "ancestor::div[.//h3 or .//h4][1]",
            "ancestor::div[.//h2][1]",
            "ancestor::section[1]",
        ):
            try:
                containers = a_el.xpath(xpath_expr)
                if not containers:
                    continue
                c = containers[0] if hasattr(containers, "__getitem__") else containers
                heading_texts = c.css("h2::text, h3::text, h4::text").getall()
                if heading_texts:
                    name = " ".join(heading_texts).strip()
                img_src = c.css("img::attr(src)").get() or ""
                description = " ".join(c.css("p::text, p *::text").getall()).strip()
                if name:
                    break
            except Exception:
                continue

        # Fallback: derive name from URL slug when DOM traversal fails
        if not name:
            name = _cupa_name_from_slug(slug)
            # Also try to find the image by matching the slug in the src
            slug_hint = slug.split("-", 1)[-1].split("-")[0] if "-" in slug else slug
            for img in page.css("img"):
                src = img.attrib.get("src", "")
                if slug_hint and slug_hint in src.lower():
                    img_src = src
                    break

        if name in seen_names:
            continue
        seen_names.add(name)

        body = f"{name}. {description}" if len(description) > 20 else name
        results.append({
            "animal_name": name,
            "body": body[:600],
            "photo_url": img_src,
            "url": href or _CUPA_URL,
        })

    logger.info("CUPA: extracted %d animals", len(results))
    return results


# ---------------------------------------------------------------------------
# Site registry
# ---------------------------------------------------------------------------

SITES: dict[str, dict] = {
    "charlies": {
        "name": _ORG_NAME,
        "url": _PAGE_URL,
        "scraper_fn": scrape_charlies,
    },
    "cupa": {
        "name": _CUPA_ORG,
        "url": _CUPA_URL,
        "scraper_fn": scrape_cupa,
    },
}


# ---------------------------------------------------------------------------
# Indexing helpers
# ---------------------------------------------------------------------------

def _doc_id(source_org: str, animal_name: str) -> str:
    key = f"{source_org}::{animal_name}"
    return hashlib.md5(key.encode()).hexdigest()


def _now_ist() -> str:
    return datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()


def run_one_site(site_key: str) -> int:
    """Scrape one site and bulk-index to raw_listings. Returns count indexed."""
    cfg = SITES.get(site_key)
    if cfg is None:
        logger.error("unknown site key: %s", site_key)
        return 0

    cards = cfg["scraper_fn"]()
    if not cards:
        logger.warning("%s: no cards extracted", site_key)
        return 0

    crawled_at = _now_ist()
    source_org = cfg["name"]

    def _actions():
        for card in cards:
            doc_id = _doc_id(source_org, card["animal_name"])
            yield {
                "_index": "raw_listings",
                "_id": doc_id,
                "_source": {
                    "url": card["url"],
                    "title": card["animal_name"],
                    "body": card["body"],
                    "photo_url": card.get("photo_url", ""),
                    "source_org": source_org,
                    "crawled_at": crawled_at,
                    "structured": False,
                },
            }

    es = get_es()
    successes, errors = helpers.bulk(es, _actions(), raise_on_error=False, stats_only=False)
    for err in errors:
        logger.error("bulk error: %s", err)

    logger.info("scraped %s: %d cards, indexed %d to raw_listings", site_key, len(cards), successes)
    return successes


def run_all_sites() -> dict[str, int]:
    """Run every configured site once. Returns {site_key: count}."""
    totals: dict[str, int] = {}
    for key in SITES:
        try:
            totals[key] = run_one_site(key)
        except Exception:
            logger.exception("unhandled error scraping site '%s'", key)
            totals[key] = 0

    parts = ", ".join(f"{k}={v}" for k, v in totals.items())
    logger.info("summary: %s", parts)
    return totals


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrapling-based pet shelter scraper.")
    parser.add_argument("--once", action="store_true", help="run all sites once then exit")
    parser.add_argument("--site", default=None, help="scrape only this site key (default: all)")
    parser.add_argument("--interval", type=int, default=300, help="seconds between periodic runs (default 300)")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        while True:
            if args.site:
                run_one_site(args.site)
            else:
                run_all_sites()

            if args.once:
                break

            logger.info("next run in %ds (Ctrl+C to stop)", args.interval)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("scraper stopped by user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
