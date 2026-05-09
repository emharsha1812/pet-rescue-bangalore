"""Bulk-load curated JSON data files into Elasticsearch.

With EIS semantic_text fields, no explicit embedding step is needed —
indexing a document with `description` or `body` automatically triggers
EIS to populate the corresponding semantic_text field.
"""
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from elasticsearch import helpers

from backend.es_client import get_es

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"


def load_json(filename: str) -> list[dict]:
    path = DATA_DIR / filename
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def make_actions(index: str, docs: list[dict]):
    for doc in docs:
        yield {"_index": index, "_source": doc}


def bulk_index(index: str, docs: list[dict]) -> None:
    es = get_es()
    successes, errors = helpers.bulk(
        es,
        make_actions(index, docs),
        raise_on_error=False,
        stats_only=False,
    )
    if errors:
        for err in errors:
            logger.error("bulk error: %s", err)
    logger.info("indexed %d docs into '%s'", successes, index)


def main() -> int:
    try:
        animals = load_json("animals.json")
        bulk_index("animals", animals)

        vets = load_json("vets.json")
        bulk_index("vets", vets)

        rescuers = load_json("rescuers.json")
        bulk_index("rescuers", rescuers)

        protocols = load_json("protocols.json")
        bulk_index("protocols", protocols)

    except Exception:
        logger.exception("bulk load failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
