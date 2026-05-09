import os

from elasticsearch import Elasticsearch

_ES: Elasticsearch | None = None


def get_es() -> Elasticsearch:
    global _ES
    if _ES is None:
        _ES = Elasticsearch(
            cloud_id=os.environ["ELASTIC_CLOUD_ID"],
            api_key=os.environ["ELASTIC_API_KEY"],
            request_timeout=30,
        )
    return _ES
