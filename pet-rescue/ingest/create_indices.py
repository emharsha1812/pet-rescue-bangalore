import argparse
import logging
import sys
from typing import Any

from dotenv import load_dotenv

from backend.es_client import get_es

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

INDEX_MAPPINGS: dict[str, dict[str, Any]] = {
    "animals": {
        "mappings": {
            "properties": {
                "name": {"type": "text"},
                "species": {"type": "keyword"},
                "breed": {"type": "keyword"},
                "age_months": {"type": "integer"},
                "sex": {"type": "keyword"},
                "size": {"type": "keyword"},
                "vaccinated": {"type": "boolean"},
                "neutered": {"type": "boolean"},
                "good_with_kids": {"type": "boolean"},
                "good_with_dogs": {"type": "boolean"},
                "temperament_tags": {"type": "keyword"},
                "description": {"type": "text", "copy_to": "description_semantic"},
                "description_semantic": {
                    "type": "semantic_text",
                    "inference_id": ".jina-embeddings-v5-text-small",
                },
                "photo_url": {"type": "keyword"},
                "source_org": {"type": "keyword"},
                "source_url": {"type": "keyword"},
                "location": {"type": "geo_point"},
                "date_listed": {"type": "date"},
            }
        }
    },
    "vets": {
        "mappings": {
            "properties": {
                "name": {"type": "text"},
                "address": {"type": "text"},
                "location": {"type": "geo_point"},
                "phone": {"type": "keyword"},
                "hours": {"type": "object", "enabled": True},
                "emergency_capable": {"type": "boolean"},
                "has_surgery": {"type": "boolean"},
                "specialties": {"type": "keyword"},
            }
        }
    },
    "rescuers": {
        "mappings": {
            "properties": {
                "name": {"type": "text"},
                "contact": {"type": "keyword"},
                "areas_covered": {"type": "keyword"},
                "on_call_hours": {"type": "object", "enabled": True},
                "capabilities": {"type": "keyword"},
                "animals_handled": {"type": "keyword"},
            }
        }
    },
    "protocols": {
        "mappings": {
            "properties": {
                "scenario": {"type": "keyword"},
                "title": {"type": "text"},
                "body": {"type": "text", "copy_to": "body_semantic"},
                "body_semantic": {
                    "type": "semantic_text",
                    "inference_id": ".jina-embeddings-v5-text-small",
                },
                "severity": {"type": "keyword"},
            }
        }
    },
    "raw_listings": {
        "mappings": {
            "properties": {
                "url": {"type": "keyword"},
                "title": {"type": "text"},
                "body": {"type": "text"},
                "source_org": {"type": "keyword"},
                "crawled_at": {"type": "date"},
                "structured": {"type": "boolean"},
            }
        }
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create Elasticsearch indices.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing indices before creating them.",
    )
    return parser.parse_args()


def create_indices(reset: bool) -> None:
    es = get_es()
    for name, mapping in INDEX_MAPPINGS.items():
        exists = es.indices.exists(index=name)

        if reset:
            if exists:
                es.indices.delete(index=name)
            es.indices.create(index=name, **mapping)
            logger.info("recreated: %s", name)
            continue

        if exists:
            logger.info("skipped (exists): %s", name)
            continue

        es.indices.create(index=name, **mapping)
        logger.info("created: %s", name)


def main() -> int:
    args = parse_args()
    try:
        create_indices(reset=args.reset)
    except Exception:
        logger.exception("unexpected error creating indices")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
