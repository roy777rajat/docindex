"""Find this deployment's resource names from the CloudFormation stack `cleandocs-core` (works in any AWS account)."""
from __future__ import annotations

import boto3


def load(region: str = "eu-west-1", stack: str = "cleandocs-core") -> dict[str, str]:
    sess = boto3.Session(region_name=region)
    acct = sess.client("sts").get_caller_identity()["Account"]
    cfg = {"account": acct, "region": region, "bucket": f"cleandocs-{acct}-{region}",
           "vector_bucket": f"cleandocs-vectors-{acct}", "index": "cleandocs-index",
           "registry": "cleandocs-registry", "trail": "document-ingest-trail", "kb_id": "", "ds_id": ""}
    try:
        outs = sess.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"cannot read stack '{stack}' in {region} ({exc}). Is AWS configured for the right account/region?")
    o = {x["OutputKey"]: x["OutputValue"] for x in outs}
    cfg.update(bucket=o.get("BucketName", cfg["bucket"]), registry=o.get("RegistryTableName", cfg["registry"]),
               trail=o.get("TrailTableName", cfg["trail"]), kb_id=o.get("KnowledgeBaseId", ""), ds_id=o.get("DataSourceId", ""))
    if o.get("VectorBucketArn"):
        cfg["vector_bucket"] = o["VectorBucketArn"].rsplit("/", 1)[-1]
    return cfg
