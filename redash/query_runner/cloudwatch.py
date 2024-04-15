import datetime
import logging
import os

import yaml

from redash.query_runner import BaseQueryRunner, register
from redash.settings import parse_boolean
from redash.utils import parse_human_time

try:
    import boto3

    enabled = True
except ImportError:
    enabled = False

logger = logging.getLogger(__name__)
ASSUME_ROLE = parse_boolean(os.environ.get('ATHENA_ASSUME_ROLE', 'false'))

def parse_response(results):
    columns = [
        {"name": "id", "type": "string"},
        {"name": "label", "type": "string"},
        {"name": "timestamp", "type": "datetime"},
        {"name": "value", "type": "float"},
    ]

    rows = []

    for metric in results:
        for i, value in enumerate(metric["Values"]):
            rows.append(
                {
                    "id": metric["Id"],
                    "label": metric["Label"],
                    "timestamp": metric["Timestamps"][i],
                    "value": value,
                }
            )

    return rows, columns


def parse_query(query):
    query = yaml.safe_load(query)

    for timeKey in ["StartTime", "EndTime"]:
        if isinstance(query.get(timeKey), str):
            query[timeKey] = int(parse_human_time(query[timeKey]).timestamp())
    if not query.get("EndTime"):
        query["EndTime"] = int(datetime.datetime.now().timestamp())

    return query


class CloudWatch(BaseQueryRunner):
    should_annotate_query = False

    @classmethod
    def name(cls):
        return "Amazon CloudWatch"

    @classmethod
    def configuration_schema(cls):
        schema = {
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "title": "AWS Region"
                },
                "aws_access_key": {
                    "type": "string",
                    "title": "AWS Access Key"
                },
                "aws_secret_key": {
                    "type": "string",
                    "title": "AWS Secret Key"
                },
            },
            "required": ["region", "aws_access_key", "aws_secret_key"],
            "order": ["region", "aws_access_key", "aws_secret_key"],
            "secret": ["aws_secret_key"],
        }

        if ASSUME_ROLE:
            del schema["properties"]["aws_access_key"]
            del schema["properties"]["aws_secret_key"]

            schema["required"].remove("aws_access_key")
            schema["required"].remove("aws_secret_key")

            schema["order"].remove("aws_access_key")
            schema["order"].remove("aws_secret_key")

            schema["properties"].update({
                "assume_role_arn": {
                    "type": "string",
                    "title": "Assume Role ARN"
                },
                "external_id": {
                    "type": "string",
                    "title": "External ID"
                },
            })
            schema["required"].extend(["assume_role_arn", "assume_role_session_name"])
            schema["order"].extend(["region","assume_role_arn", "assume_role_session_name", "external_id"])

        return schema

    @classmethod
    def enabled(cls):
        return enabled

    def __init__(self, configuration):
        super(CloudWatch, self).__init__(configuration)
        self.syntax = "yaml"

    def test_connection(self):
        self.get_schema()

    def _get_iam_credentials(self, user=None):
        if ASSUME_ROLE:
            role_session_name = 'redash' if user is None else user.email
            sts = boto3.client('sts')
            creds = sts.assume_role(
                RoleArn=self.configuration['assume_role_arn'],
                RoleSessionName=role_session_name,
                ExternalId=self.configuration.get('external_id')
            )
            return {
                'aws_access_key_id': creds['Credentials']['AccessKeyId'],
                'aws_secret_access_key': creds['Credentials']['SecretAccessKey'],
                'aws_session_token': creds['Credentials']['SessionToken'],
                'region_name': self.configuration.get('region')
            }
        else:
            return {
                'aws_access_key_id': self.configuration.get('aws_access_key'),
                'aws_secret_access_key': self.configuration.get('aws_secret_key'),
                'region_name': self.configuration.get('region')
            }

    def _get_client(self):
        try:
            cloudwatch = boto3.client("cloudwatch", **self._get_iam_credentials(self.current_user))
            return cloudwatch
        except Exception as e:
            logger.exception(f"Failed to connect to CloudWatch: {e}")
            raise

    def get_schema(self, get_stats=False):
        client = self._get_client()

        paginator = client.get_paginator("list_metrics")

        metrics = {}
        for page in paginator.paginate():
            for metric in page["Metrics"]:
                if metric["Namespace"] not in metrics:
                    metrics[metric["Namespace"]] = {
                        "name": metric["Namespace"],
                        "columns": [],
                    }

                if metric["MetricName"] not in metrics[metric["Namespace"]]["columns"]:
                    metrics[metric["Namespace"]]["columns"].append(metric["MetricName"])

        return list(metrics.values())

    def run_query(self, query, user):
        cloudwatch = self._get_client()

        query = parse_query(query)

        results = []
        paginator = cloudwatch.get_paginator("get_metric_data")
        for page in paginator.paginate(**query):
            results += page["MetricDataResults"]

        rows, columns = parse_response(results)

        return {"rows": rows, "columns": columns}, None


register(CloudWatch)
