"""Convert raw_listings docs into structured animals docs via Bedrock.

Each raw doc is expected to represent one animal (as produced by scraper.py).
Bedrock extracts typed fields; the animals index uses copy_to so EIS handles
embedding automatically on index.

Usage:
    python -m ingest.structurer [--limit N]
"""

import argparse
import hashlib
import json
import logging
import os
import random
import sys
import time
from datetime import date
from zoneinfo import ZoneInfo

import boto3
from dotenv import load_dotenv

load_dotenv()

from backend.es_client import get_es  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ingest.structurer")

_BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-6")
_AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Org-specific base coords + small jitter so pins spread on the map
_ORG_BASE_LOCATIONS: dict[str, tuple[float, float]] = {
    "Charlie's Animal Rescue": (12.9254, 77.6012),  # south Bangalore
    "CUPA Bengaluru": (12.9716, 77.6219),           # Ulsoor
}
_BLR_LAT, _BLR_LON = 12.9716, 77.5946  # city centre fallback
_JITTER = 0.04  # ~4 km spread so pins don't pile on one point

# Delay between Bedrock calls to stay within token rate limits
_BEDROCK_DELAY_S = 0.5


def _org_location(source_org: str) -> dict:
    lat, lon = _ORG_BASE_LOCATIONS.get(source_org, (_BLR_LAT, _BLR_LON))
    return {
        "lat": round(lat + random.uniform(-_JITTER, _JITTER), 6),
        "lon": round(lon + random.uniform(-_JITTER, _JITTER), 6),
    }


def _doc_id(source_url: str) -> str:
    return hashlib.md5(source_url.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Bedrock extraction
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT = """\
You are given a raw animal listing from a shelter website.
Extract the fields below as a JSON object. Return ONLY the JSON object — no prose.

Fields:
- name (string): animal's given name
- species (string): "dog", "cat", or "other"
- breed (string): best guess or "Unknown"
- age_months (integer): estimate; use 12 for "1 year", 6 for "puppy/kitten" if unknown
- sex (string): "male", "female", or "unknown"
- size (string): "small", "medium", or "large"
- vaccinated (boolean): true/false/null if not mentioned
- neutered (boolean): true/false/null if not mentioned
- good_with_kids (boolean): true/false/null if not mentioned
- good_with_dogs (boolean): true/false/null if not mentioned
- temperament_tags (list of strings): up to 5 short lowercase tags, e.g. ["calm","playful"]
- description (string): a clean 1-2 sentence description suitable for an adoption card

If this text does NOT describe an adoptable animal, return: {{"skip": true}}

Raw listing title: {title}
Raw listing body:
{body}
"""


def _call_bedrock(client, title: str, body: str) -> dict | None:
    prompt = _EXTRACTION_PROMPT.format(title=title, body=body)
    try:
        resp = client.converse(
            modelId=_BEDROCK_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 512, "temperature": 0},
        )
        text = resp["output"]["message"]["content"][0]["text"].strip()
        # Strip markdown fences if Bedrock wraps JSON in ```json ... ```
        if text.startswith("```"):
            text = "\n".join(text.splitlines()[1:])
            text = text.rstrip("`").strip()
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Bedrock returned non-JSON for '%s'", title)
        return None
    except Exception:
        logger.exception("Bedrock call failed for '%s'", title)
        return None


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(limit: int) -> None:
    es = get_es()
    bedrock = boto3.client("bedrock-runtime", region_name=_AWS_REGION)

    today = date.today().isoformat()

    # Fetch raw docs where structured is false or missing.
    query = {
        "query": {
            "bool": {
                "should": [
                    {"term": {"structured": False}},
                    {"bool": {"must_not": {"exists": {"field": "structured"}}}},
                ],
                "minimum_should_match": 1,
            }
        },
        "size": limit,
    }
    resp = es.search(index="raw_listings", body=query)
    hits = resp["hits"]["hits"]
    logger.info("starting (limit=%d): found %d unstructured raw docs", limit, len(hits))

    total_indexed = 0
    total_skipped = 0

    for hit in hits:
        raw_id = hit["_id"]
        src = hit["_source"]
        title = src.get("title", "")
        body = src.get("body", "")
        source_org = src.get("source_org", "")
        source_url = src.get("url", "")

        if not body or len(body) < 50:
            logger.warning("raw doc %s has empty/short body — skipping", raw_id)
            total_skipped += 1
            _mark_structured(es, raw_id)
            continue

        extracted = _call_bedrock(bedrock, title, body)
        time.sleep(_BEDROCK_DELAY_S)

        if extracted is None or extracted.get("skip"):
            logger.info("skipping raw doc '%s' (Bedrock said skip or error)", title)
            total_skipped += 1
            _mark_structured(es, raw_id)
            continue

        photo_url = src.get("photo_url", "")

        animal_doc = {
            "name": extracted.get("name") or title,
            "species": extracted.get("species", "dog"),
            "breed": extracted.get("breed", "Unknown"),
            "age_months": _safe_int(extracted.get("age_months"), 12),
            "sex": extracted.get("sex", "unknown"),
            "size": extracted.get("size", "medium"),
            "vaccinated": extracted.get("vaccinated"),
            "neutered": extracted.get("neutered"),
            "good_with_kids": extracted.get("good_with_kids"),
            "good_with_dogs": extracted.get("good_with_dogs"),
            "temperament_tags": extracted.get("temperament_tags", [])[:5],
            "description": extracted.get("description") or body[:300],
            "photo_url": photo_url,
            "source_org": source_org,
            "source_url": source_url,
            "location": _org_location(source_org),
            "date_listed": today,
        }

        animal_id = _doc_id(source_url)
        try:
            es.index(index="animals", id=animal_id, document=animal_doc)
            total_indexed += 1
        except Exception:
            logger.exception("failed to index animal '%s'", title)
            total_skipped += 1
            continue

        _mark_structured(es, raw_id)

    logger.info(
        "processed %d raw docs, indexed %d animals, skipped %d",
        len(hits),
        total_indexed,
        total_skipped,
    )


def _mark_structured(es, raw_id: str) -> None:
    try:
        es.update(index="raw_listings", id=raw_id, doc={"structured": True})
    except Exception:
        logger.warning("could not mark raw doc %s as structured", raw_id)


def _safe_int(val, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Structure raw_listings → animals via Bedrock.")
    parser.add_argument("--limit", type=int, default=30, help="max raw docs to process (default 30)")
    parser.add_argument("--dry-run", action="store_true", help="extract fields but do not write to ES")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        run(args.limit)
    except Exception:
        logger.exception("structurer failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
