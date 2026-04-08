#!/usr/bin/env python3
"""
Ingest a Terraform workspace resource graph into Amazon Neptune.

Reads DOT output from `terraform graph`, extracts resource nodes and
dependency edges, then upserts them into Neptune via openCypher HTTP.
Optionally uploads the DOT snapshot to S3.

Usage:
    terraform graph > graph.dot
    python3 ingest_graph.py \\
        --dot-path graph.dot \\
        --repo-uri https://github.com/org/repo \\
        --endpoint <neptune-cluster-endpoint> \\
        --port 8182 --region us-west-2 \\
        --iam-auth true \\
        --bucket <staging-bucket> \\
        --snapshot-key snapshots/repo/20260101T000000Z.dot
"""

import argparse
import re
import sys
import json
import boto3
import requests
from requests_aws4auth import AWS4Auth


# Matches:   "[root] aws_iam_role.foo (expand)" [label = "aws_iam_role.foo", ...]
_NODE_RE = re.compile(r'"(\[.*?\])\s+(\S+)\s*(?:\(.*?\))?"')
# Matches edges:  "SRC" -> "DST"
_EDGE_RE = re.compile(r'"([^"]+)"\s*->\s*"([^"]+)"')


def _clean_addr(raw: str) -> str:
    """Strip DOT decorations: [root] / [module.x] prefix and (expand) suffix."""
    addr = re.sub(r'^\[.*?\]\s+', '', raw)
    addr = re.sub(r'\s*\(.*?\)\s*$', '', addr)
    return addr.strip()


def _leaf_addr(addr: str) -> str:
    """Strip leading module.X. prefixes to get the leaf resource address."""
    leaf = addr
    while leaf.startswith("module."):
        parts = leaf.split(".", 2)
        if len(parts) < 3:
            break
        leaf = parts[2]
    return leaf


def _is_resource(addr: str) -> bool:
    """True if the address looks like a real resource (not a meta-node)."""
    leaf = _leaf_addr(addr)
    skip = {"provider", "var.", "local.", "output.", "module.", "data."}
    return any(leaf.startswith(p) for p in ("aws_", "google_", "azurerm_", "vault_", "consul_", "nomad_", "hcp_")) or (
        "." in leaf and not any(leaf.startswith(s) for s in skip)
    )


def parse_dot(dot_text: str):
    """Return (nodes, edges) lists from terraform graph DOT output."""
    nodes = {}
    edges = []

    for line in dot_text.splitlines():
        edge_m = _EDGE_RE.search(line)
        if edge_m and "->" in line:
            src_raw, dst_raw = edge_m.group(1), edge_m.group(2)
            src = _clean_addr(src_raw)
            dst = _clean_addr(dst_raw)
            if src and dst and src != dst:
                edges.append((src, dst))
            continue

        # Node declaration line: pick up the label attribute for clean name
        label_m = re.search(r'label\s*=\s*"([^"]+)"', line)
        if label_m:
            label = label_m.group(1)
            # Find the DOT node key (the quoted identifier before [)
            key_m = re.match(r'\s*"([^"]+)"\s*\[', line)
            if key_m:
                nodes[key_m.group(1)] = label

    # Resolve resources: keep only resource-looking addresses
    resource_nodes = []
    resource_addrs = set()
    for raw_key, label in nodes.items():
        addr = label if label else _clean_addr(raw_key)
        if _is_resource(addr):
            leaf = _leaf_addr(addr)
            parts = leaf.split(".", 1)
            resource_nodes.append({
                "id": addr,
                "type": parts[0] if len(parts) == 2 else leaf,
                "name": parts[1] if len(parts) == 2 else leaf,
            })
            resource_addrs.add(raw_key)  # keep raw key for edge mapping

    # Also collect any edge endpoints not in the label list
    addr_by_raw: dict[str, str] = {}
    for raw_key, label in nodes.items():
        addr_by_raw[raw_key] = label if label else _clean_addr(raw_key)

    resource_addr_set = {n["id"] for n in resource_nodes}

    resource_edges = []
    for src_raw, dst_raw in edges:
        src = addr_by_raw.get(src_raw, _clean_addr(src_raw))
        dst = addr_by_raw.get(dst_raw, _clean_addr(dst_raw))
        if src in resource_addr_set and dst in resource_addr_set and src != dst:
            resource_edges.append({"from": src, "to": dst})

    return resource_nodes, resource_edges


def merge_into_neptune(nodes, edges, endpoint, port, region, iam_auth, repo_uri):
    """Upsert nodes and edges into Neptune via openCypher HTTP."""
    url = f"https://{endpoint}:{port}/openCypher"

    if iam_auth:
        creds = boto3.Session().get_credentials().get_frozen_credentials()
        auth = AWS4Auth(
            creds.access_key,
            creds.secret_key,
            region,
            "neptune-db",
            session_token=creds.token,
        )
    else:
        auth = None

    session = requests.Session()
    repo_name = repo_uri.rstrip("/").split("/")[-1].removesuffix(".git")

    def run(query, params):
        resp = session.post(
            url, auth=auth,
            data={"query": query, "parameters": json.dumps(params)},
            timeout=30,
        )
        if not resp.ok:
            print(f"Neptune {resp.status_code} error: {resp.text[:500]}", file=sys.stderr)
        resp.raise_for_status()
        return resp.json()

    # Upsert repo node
    run(
        "MERGE (r:Repository {uri: $uri}) SET r.name = $name",
        {"uri": repo_uri, "name": repo_name},
    )

    # Upsert resource nodes
    for n in nodes:
        run(
            "MERGE (r:Resource {id: $id, repo: $repo}) SET r.type = $type, r.name = $name",
            {"id": n["id"], "repo": repo_uri, "type": n["type"], "name": n["name"]},
        )

    # Link resources to repo
    run(
        "MATCH (repo:Repository {uri: $repo}), (r:Resource {repo: $repo}) MERGE (repo)-[:CONTAINS]->(r)",
        {"repo": repo_uri},
    )

    # Upsert dependency edges
    for e in edges:
        run(
            """MATCH (a:Resource {id: $from, repo: $repo}), (b:Resource {id: $to, repo: $repo})
               MERGE (a)-[:DEPENDS_ON]->(b)""",
            {"from": e["from"], "to": e["to"], "repo": repo_uri},
        )

    print(f"Merged {len(nodes)} nodes, {len(edges)} edges for {repo_uri}")


def upload_snapshot(dot_text, bucket, snapshot_key, region):
    s3 = boto3.client("s3", region_name=region)
    s3.put_object(
        Bucket=bucket,
        Key=snapshot_key,
        Body=dot_text.encode(),
        ContentType="text/plain",
    )
    print(f"Snapshot uploaded to s3://{bucket}/{snapshot_key}")


def main():
    parser = argparse.ArgumentParser(description="Ingest terraform graph into Neptune")
    parser.add_argument("--dot-path", required=True, help="Path to terraform graph DOT output")
    parser.add_argument("--repo-uri", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--port", default="8182")
    parser.add_argument("--region", required=True)
    parser.add_argument("--iam-auth", default="true")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--snapshot-key", required=True)
    args = parser.parse_args()

    with open(args.dot_path) as f:
        dot_text = f.read()

    nodes, edges = parse_dot(dot_text)
    print(f"Extracted {len(nodes)} resource nodes, {len(edges)} dependency edges")

    if not nodes:
        print("No resource nodes found — nothing to ingest", file=sys.stderr)
        sys.exit(1)

    merge_into_neptune(
        nodes, edges,
        endpoint=args.endpoint,
        port=args.port,
        region=args.region,
        iam_auth=args.iam_auth.lower() == "true",
        repo_uri=args.repo_uri,
    )

    upload_snapshot(dot_text, args.bucket, args.snapshot_key, args.region)


if __name__ == "__main__":
    main()
