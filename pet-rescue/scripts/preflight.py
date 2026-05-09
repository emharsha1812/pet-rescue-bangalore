"""Pre-flight connectivity check. Run before any other code.

Tests Elasticsearch, AWS Bedrock, and Elastic Inference Service (EIS).
Exits non-zero if anything fails. Do not proceed past this until all three pass.
"""
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()


def check_elastic() -> str:
    from elasticsearch import Elasticsearch

    cloud_id = os.environ["ELASTIC_CLOUD_ID"]
    api_key = os.environ["ELASTIC_API_KEY"]
    es = Elasticsearch(cloud_id=cloud_id, api_key=api_key, request_timeout=30)
    info = es.info()
    return f"cluster={info['cluster_name']} version={info['version']['number']}"


def check_bedrock() -> str:
    import boto3

    region = os.environ["AWS_REGION"]
    model_id = os.environ["BEDROCK_MODEL_ID"]
    client = boto3.client("bedrock-runtime", region_name=region)
    resp = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": "Reply with just: OK"}]}],
        inferenceConfig={"maxTokens": 10},
    )
    text = resp["output"]["message"]["content"][0]["text"].strip()
    return f"model='{model_id}' reply='{text}'"


_EIS_INFERENCE_ID = ".jina-embeddings-v5-text-small"


def check_eis() -> str:
    from elasticsearch import Elasticsearch

    es = Elasticsearch(
        cloud_id=os.environ["ELASTIC_CLOUD_ID"],
        api_key=os.environ["ELASTIC_API_KEY"],
        request_timeout=30,
    )
    resp = es.inference.inference(
        inference_id=_EIS_INFERENCE_ID,
        body={"input": ["preflight test"]},
    )
    data = dict(resp)
    embeddings = data.get("text_embedding", [])
    if not embeddings or "embedding" not in embeddings[0]:
        raise ValueError(f"no embedding returned from {_EIS_INFERENCE_ID}")
    dim = len(embeddings[0]["embedding"])
    return f"inference_id={_EIS_INFERENCE_ID} dim={dim}"


def main() -> None:
    checks = [
        ("Elasticsearch", check_elastic),
        ("Bedrock      ", check_bedrock),
        ("EIS          ", check_eis),
    ]
    failed = False
    for name, fn in checks:
        t0 = time.time()
        try:
            detail = fn()
            ms = int((time.time() - t0) * 1000)
            print(f"[OK]   {name}  ({ms:>5}ms)  {detail}")
        except KeyError as e:
            failed = True
            print(f"[FAIL] {name}  missing env var: {e}")
        except Exception as e:
            failed = True
            print(f"[FAIL] {name}  {type(e).__name__}: {e}")
    if failed:
        print("\nPre-flight FAILED. Fix the above before proceeding.")
        sys.exit(1)
    print("\nAll three OK. You may proceed to ingest/create_indices.py.")


if __name__ == "__main__":
    main()
