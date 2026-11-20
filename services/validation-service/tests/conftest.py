import os

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-1")
for _k in ("AWS_PROFILE", "AWS_ENDPOINT_URL"):
    os.environ.pop(_k, None)

BUCKET = "cleandocs-test-bucket"


def _table(ddb, name):
    return ddb.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def aws_env():
    """Mocked S3 bucket + registry + trail tables. Returns a dict of handles."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="eu-west-1")
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        ddb = boto3.resource("dynamodb", region_name="eu-west-1")
        yield {
            "s3": s3, "bucket": BUCKET,
            "registry_table": _table(ddb, "cleandocs-registry"),
            "trail_table": _table(ddb, "document-ingest-trail"),
            "sfn": boto3.client("stepfunctions", region_name="eu-west-1"),
        }
