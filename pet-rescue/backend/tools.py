import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from backend.es_client import get_es

logger = logging.getLogger(__name__)

_KOLKATA = ZoneInfo("Asia/Kolkata")
_WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _covers_time(hours: dict, dt: datetime) -> bool:
    """Return True if the hours dict covers the given datetime."""
    key = _WEEKDAY_KEYS[dt.weekday()]
    slot = hours.get(key, "closed")
    if not slot or slot == "closed":
        return False
    try:
        start_str, end_str = slot.split("-")
        sh, sm = int(start_str[:2]), int(start_str[3:])
        eh, em = int(end_str[:2]), int(end_str[3:])
        now_minutes = dt.hour * 60 + dt.minute
        return sh * 60 + sm <= now_minutes <= eh * 60 + em
    except Exception:
        return False


def search_animals(
    query: str,
    species: str | None = None,
    size: str | None = None,
    max_age_months: int | None = None,
    good_with_kids: bool | None = None,
    good_with_dogs: bool | None = None,
    top_k: int = 5,
) -> list[dict]:
    es = get_es()

    filters = []
    if species is not None:
        filters.append({"term": {"species": species}})
    if size is not None:
        filters.append({"term": {"size": size}})
    if max_age_months is not None:
        filters.append({"range": {"age_months": {"lte": max_age_months}}})
    if good_with_kids is True:
        filters.append({"term": {"good_with_kids": True}})
    if good_with_dogs is True:
        filters.append({"term": {"good_with_dogs": True}})

    body = {
        "size": top_k,
        "retriever": {
            "rrf": {
                "retrievers": [
                    {
                        "standard": {
                            "query": {
                                "bool": {
                                    "must": [
                                        {
                                            "multi_match": {
                                                "query": query,
                                                "fields": ["name^2", "breed^1.5", "description"],
                                            }
                                        }
                                    ],
                                    "filter": filters,
                                }
                            }
                        }
                    },
                    {
                        "standard": {
                            "query": {
                                "bool": {
                                    "must": [
                                        {
                                            "semantic": {
                                                "field": "description_semantic",
                                                "query": query,
                                            }
                                        }
                                    ],
                                    "filter": filters,
                                }
                            }
                        }
                    },
                ],
                "rank_window_size": 50,
                "rank_constant": 20,
            }
        },
    }

    resp = es.search(index="animals", body=body)
    results = []
    for hit in resp["hits"]["hits"]:
        doc = hit["_source"]
        doc["_score"] = hit.get("_score")
        results.append(doc)
    return results


def find_emergency_vet(
    lat: float,
    lon: float,
    radius_km: int = 5,
) -> list[dict]:
    es = get_es()

    body = {
        "size": 10,
        "query": {
            "bool": {
                "filter": [
                    {
                        "geo_distance": {
                            "distance": f"{radius_km}km",
                            "location": {"lat": lat, "lon": lon},
                        }
                    },
                    {"term": {"emergency_capable": True}},
                ]
            }
        },
        "sort": [
            {
                "_geo_distance": {
                    "location": {"lat": lat, "lon": lon},
                    "order": "asc",
                    "unit": "km",
                }
            }
        ],
    }

    resp = es.search(index="vets", body=body)
    now = datetime.now(_KOLKATA)

    results = []
    for hit in resp["hits"]["hits"]:
        doc = hit["_source"]
        if not _covers_time(doc.get("hours", {}), now):
            continue
        sort_vals = hit.get("sort", [])
        doc["distance_km"] = round(sort_vals[0], 2) if sort_vals else None
        results.append(doc)

    return results[:5]


def find_active_rescuers(
    area: str,
    time_iso: str,
) -> list[dict]:
    es = get_es()

    dt = datetime.fromisoformat(time_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_KOLKATA)

    def _query_rescuers(area_filter: str | None) -> list[dict]:
        if area_filter:
            q = {"bool": {"filter": [{"term": {"areas_covered": area_filter}}]}}
        else:
            q = {"match_all": {}}
        resp = es.search(index="rescuers", body={"size": 20, "query": q})
        return resp["hits"]["hits"]

    hits = _query_rescuers(area)
    on_call = [
        h["_source"] for h in hits
        if _covers_time(h["_source"].get("on_call_hours", {}), dt)
    ]

    if on_call:
        for doc in on_call:
            doc["area_match"] = True
        return on_call

    # Fallback: any on-call rescuer, no area filter
    fallback_hits = _query_rescuers(None)
    fallback = []
    for h in fallback_hits:
        doc = h["_source"]
        if _covers_time(doc.get("on_call_hours", {}), dt):
            doc["area_match"] = False
            fallback.append(doc)
        if len(fallback) >= 5:
            break
    return fallback


def get_protocol(scenario: str) -> dict | None:
    es = get_es()

    body = {
        "size": 1,
        "query": {
            "semantic": {
                "field": "body_semantic",
                "query": scenario,
            }
        },
    }

    resp = es.search(index="protocols", body=body)
    hits = resp["hits"]["hits"]
    return hits[0]["_source"] if hits else None


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    now_ist = datetime.now(_KOLKATA)

    tests = [
        (
            "search_animals",
            lambda: search_animals(
                "calm cuddly dog good with kids",
                size="small",
                good_with_kids=True,
                top_k=3,
            ),
        ),
        (
            "find_emergency_vet",
            lambda: find_emergency_vet(lat=12.9716, lon=77.5946, radius_km=5),
        ),
        (
            "find_active_rescuers",
            lambda: find_active_rescuers(
                area="Koramangala",
                time_iso=now_ist.isoformat(),
            ),
        ),
        (
            "get_protocol",
            lambda: get_protocol("found injured stray dog hit by vehicle"),
        ),
    ]

    for name, fn in tests:
        print(f"\n{'='*60}")
        print(f"TOOL: {name}")
        print("=" * 60)
        result = fn()
        if result is None or result == [] or result == {}:
            print(f"WARNING: {name} returned empty")
        else:
            print(json.dumps(result, indent=2, default=str))
