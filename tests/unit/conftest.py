import os

import boto3
import pytest
from moto import mock_aws

# Dummy credentials so unit tests can never reach real AWS.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-1")
for _k in ("AWS_PROFILE", "AWS_ENDPOINT_URL"):
    os.environ.pop(_k, None)


@pytest.fixture
def trail_table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="eu-west-1")
        table = ddb.create_table(
            TableName="document-ingest-trail",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table


@pytest.fixture
def registry_table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="eu-west-1")
        table = ddb.create_table(
            TableName="cleandocs-registry",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table
