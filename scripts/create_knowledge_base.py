#!/usr/bin/env python3
"""create_knowledge_base.py — Create a Bedrock Knowledge Base with an OpenSearch Serverless vector store.

Polls until the collection and knowledge base are ACTIVE before returning IDs.
With --output-id-only, prints knowledge_base_id and data_source_id to stdout
(used by deploy.sh to write kb.auto.tfvars).

Usage:
    python3 scripts/create_knowledge_base.py \\
        --region us-west-2 \\
        --kb-name hashicorp-knowledge-base \\
        --kb-role-arn arn:aws:iam::123456789012:role/bedrock-kb-hashicorp-rag \\
        --collection-arn arn:aws:aoss:us-west-2:123456789012:collection/abc123 \\
        --collection-endpoint https://abc123.us-west-2.aoss.amazonaws.com \\
        --bucket-name hashicorp-rag-docs-a1b2c3d4 \\
        [--output-id-only]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

import boto3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

POLL_INTERVAL = 15  # seconds
MAX_POLLS = 40  # 10 minutes


def wait_for_collection(aoss_client: object, collection_id: str) -> None:
    """Poll until the OpenSearch Serverless collection is ACTIVE."""
    for _ in range(MAX_POLLS):
        resp = aoss_client.batch_get_collection(ids=[collection_id])
        details = resp.get("collectionDetails", [])
        if details and details[0].get("status") == "ACTIVE":
            log.info("Collection %s is ACTIVE", collection_id)
            return
        status = details[0].get("status", "UNKNOWN") if details else "UNKNOWN"
        log.info("Collection status: %s — waiting %ds...", status, POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Collection {collection_id} did not become ACTIVE in time")


def create_vector_index(endpoint: str, region: str, collection_name: str) -> None:
    """Create the vector index in OpenSearch Serverless.

    The index must exist before the first ingestion job.
    """
    try:
        from opensearchpy import OpenSearch, RequestsHttpConnection
        from requests_aws4auth import AWS4Auth
        import boto3

        session = boto3.Session()
        credentials = session.get_credentials()
        auth = AWS4Auth(
            credentials.access_key,
            credentials.secret_key,
            region,
            "aoss",
            session_token=credentials.token,
        )
        client = OpenSearch(
            hosts=[{"host": endpoint.replace("https://", ""), "port": 443}],
            http_auth=auth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
        )
        index_body = {
            "settings": {"index.knn": True},
            "mappings": {
                "properties": {
                    "bedrock-knowledge-base-default-vector": {
                        "type": "knn_vector",
                        "dimension": 1024,
                        "method": {
                            "name": "hnsw",
                            "space_type": "l2",
                            "engine": "faiss",
                        },
                    },
                    "AMAZON_BEDROCK_TEXT_CHUNK": {"type": "text"},
                    "AMAZON_BEDROCK_METADATA": {"type": "text", "index": False},
                }
            },
        }
        index_name = "bedrock-knowledge-base-default-index"
        if not client.indices.exists(index=index_name):
            client.indices.create(index=index_name, body=index_body)
            log.info("Created vector index: %s", index_name)
        else:
            log.info("Vector index already exists: %s", index_name)
    except ImportError:
        log.warning("opensearch-py not available — skipping vector index creation (install manually if needed)")


def wait_for_knowledge_base(bedrock_agent: object, kb_id: str) -> None:
    """Poll until the Knowledge Base is ACTIVE."""
    for _ in range(MAX_POLLS):
        resp = bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id)
        status = resp["knowledgeBase"]["status"]
        if status == "ACTIVE":
            log.info("Knowledge Base %s is ACTIVE", kb_id)
            return
        log.info("Knowledge Base status: %s — waiting %ds...", status, POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Knowledge Base {kb_id} did not become ACTIVE in time")


def create_or_get_knowledge_base(
    bedrock_agent: object,
    kb_name: str,
    kb_role_arn: str,
    embedding_model_arn: str,
    collection_arn: str,
) -> str:
    """Create the Bedrock Knowledge Base, or return the ID if it already exists."""
    paginator = bedrock_agent.get_paginator("list_knowledge_bases")
    for page in paginator.paginate():
        for kb in page.get("knowledgeBaseSummaries", []):
            if kb["name"] == kb_name:
                log.info("Knowledge Base already exists: %s (%s)", kb_name, kb["knowledgeBaseId"])
                return kb["knowledgeBaseId"]

    log.info("Creating Knowledge Base: %s", kb_name)
    resp = bedrock_agent.create_knowledge_base(
        name=kb_name,
        roleArn=kb_role_arn,
        knowledgeBaseConfiguration={
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {
                "embeddingModelArn": embedding_model_arn,
                "embeddingModelConfiguration": {
                    "bedrockEmbeddingModelConfiguration": {"dimensions": 1024}
                },
            },
        },
        storageConfiguration={
            "type": "OPENSEARCH_SERVERLESS",
            "opensearchServerlessConfiguration": {
                "collectionArn": collection_arn,
                "vectorIndexName": "bedrock-knowledge-base-default-index",
                "fieldMapping": {
                    "vectorField": "bedrock-knowledge-base-default-vector",
                    "textField": "AMAZON_BEDROCK_TEXT_CHUNK",
                    "metadataField": "AMAZON_BEDROCK_METADATA",
                },
            },
        },
    )
    return resp["knowledgeBase"]["knowledgeBaseId"]


def create_or_get_data_source(
    bedrock_agent: object,
    kb_id: str,
    bucket_name: str,
    chunk_size: int = 1024,
    chunk_overlap_pct: int = 20,
) -> str:
    """Create the S3 data source, or return the ID if it already exists."""
    paginator = bedrock_agent.get_paginator("list_data_sources")
    for page in paginator.paginate(knowledgeBaseId=kb_id):
        for ds in page.get("dataSourceSummaries", []):
            if ds["name"] == "hashicorp-docs-s3":
                log.info("Data source already exists: %s", ds["dataSourceId"])
                return ds["dataSourceId"]

    log.info("Creating S3 data source in KB %s", kb_id)
    resp = bedrock_agent.create_data_source(
        knowledgeBaseId=kb_id,
        name="hashicorp-docs-s3",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {"bucketArn": f"arn:aws:s3:::{bucket_name}"},
        },
        vectorIngestionConfiguration={
            "chunkingConfiguration": {
                "chunkingStrategy": "FIXED_SIZE",
                "fixedSizeChunkingConfiguration": {
                    "maxTokens": chunk_size,
                    "overlapPercentage": chunk_overlap_pct,
                },
            }
        },
    )
    return resp["dataSource"]["dataSourceId"]


def main() -> None:
    """Entry point — parse arguments and create/retrieve Knowledge Base and Data Source."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--kb-name", default="hashicorp-knowledge-base")
    parser.add_argument("--kb-role-arn", required=True)
    parser.add_argument("--collection-arn", required=True)
    parser.add_argument("--collection-endpoint", required=True)
    parser.add_argument("--collection-id", default="")
    parser.add_argument("--bucket-name", required=True)
    parser.add_argument("--chunk-size", type=int, default=1024)
    parser.add_argument("--chunk-overlap-pct", type=int, default=20)
    parser.add_argument("--output-id-only", action="store_true", help="Print IDs to stdout for deploy.sh")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    bedrock_agent = session.client("bedrock-agent")
    aoss_client = session.client("opensearchserverless")

    embedding_model_arn = f"arn:aws:bedrock:{args.region}::foundation-model/amazon.titan-embed-text-v2:0"

    # Wait for collection to be active (may still be provisioning)
    if args.collection_id:
        wait_for_collection(aoss_client, args.collection_id)

    # Create the vector index in OpenSearch
    create_vector_index(args.collection_endpoint, args.region, "hashicorp-rag-vectors")

    # Create or get the Knowledge Base
    kb_id = create_or_get_knowledge_base(
        bedrock_agent,
        args.kb_name,
        args.kb_role_arn,
        embedding_model_arn,
        args.collection_arn,
    )

    wait_for_knowledge_base(bedrock_agent, kb_id)

    ds_id = create_or_get_data_source(
        bedrock_agent,
        kb_id,
        args.bucket_name,
        args.chunk_size,
        args.chunk_overlap_pct,
    )

    if args.output_id_only:
        print(f"knowledge_base_id = \"{kb_id}\"")
        print(f"data_source_id    = \"{ds_id}\"")
    else:
        log.info("Knowledge Base ID: %s", kb_id)
        log.info("Data Source ID:    %s", ds_id)


if __name__ == "__main__":
    main()
