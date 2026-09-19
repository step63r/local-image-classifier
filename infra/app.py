#!/usr/bin/env python
"""CDK app entrypoint.

Requires two context values on every invocation (see infra/README.md):
    cdk deploy --all -c myIp=203.0.113.10/32 -c domainName=images.example.com
"""
from __future__ import annotations

import os

import boto3
import aws_cdk as cdk

from stacks.app_stack import AppStack
from stacks.certificate_stack import CertificateStack

APP_REGION = "ap-northeast-1"
CERT_REGION = "us-east-1"  # ACM certs for CloudFront must live in us-east-1


def cloudfront_origin_facing_prefix_list_id(region: str) -> str:
    ec2 = boto3.client("ec2", region_name=region)
    resp = ec2.describe_managed_prefix_lists(
        Filters=[
            {"Name": "prefix-list-name", "Values": ["com.amazonaws.global.cloudfront.origin-facing"]}
        ]
    )
    prefix_lists = resp["PrefixLists"]
    if not prefix_lists:
        raise SystemExit(f"Could not find the CloudFront origin-facing managed prefix list in {region}")
    return prefix_lists[0]["PrefixListId"]


app = cdk.App()

my_ip = app.node.try_get_context("myIp")
domain_name = app.node.try_get_context("domainName")
if not my_ip or not domain_name:
    raise SystemExit(
        "Missing required context. Pass -c myIp=<your-ip>/32 -c domainName=<fqdn> to every "
        "`cdk` command, e.g.:\n"
        "  cdk deploy --all -c myIp=203.0.113.10/32 -c domainName=images.example.com"
    )

auth_username = app.node.try_get_context("authUsername") or "admin"
auth_password = app.node.try_get_context("authPassword") or "changeme"

account = app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT")

cert_stack = CertificateStack(
    app,
    "ImageClassifierCertStack",
    domain_name=domain_name,
    env=cdk.Environment(account=account, region=CERT_REGION),
    cross_region_references=True,
)

app_stack = AppStack(
    app,
    "ImageClassifierAppStack",
    my_ip=my_ip,
    domain_name=domain_name,
    auth_username=auth_username,
    auth_password=auth_password,
    certificate=cert_stack.certificate,
    cloudfront_prefix_list_id=cloudfront_origin_facing_prefix_list_id(APP_REGION),
    env=cdk.Environment(account=account, region=APP_REGION),
    cross_region_references=True,
)
app_stack.add_stack_dependency(cert_stack)

app.synth()
