from aws_cdk import CfnOutput, Duration, Fn, RemovalPolicy, Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_assets as s3_assets
from constructs import Construct

APP_PORT = 8000
DEFAULT_VPC_ID = "vpc-02446d835e9eb0076"


class AppStack(Stack):
    """EC2 (self-managed PostgreSQL + pgvector, gunicorn+Flask) + S3 (media) + CloudFront."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        domain_name: str,
        auth_username: str,
        auth_password: str,
        certificate: acm.ICertificate,
        cloudfront_prefix_list_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", vpc_id=DEFAULT_VPC_ID)

        # --- S3: originals + thumbnails ------------------------------------
        # RETAIN: this bucket holds ~24GB of irreplaceable originals. A
        # `cdk destroy` must never be able to take it out.
        bucket = s3.Bucket(
            self,
            "MediaBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- IAM: read-only S3 access + SSM (no open ports needed to debug) -
        instance_role = iam.Role(
            self,
            "AppInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
        )
        bucket.grant_read(instance_role)
        instance_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")
        )

        # --- Security group ---------------------------------------------------
        # No inbound SSH at all: instance access is via SSM Session Manager only
        # (the instance role's AmazonSSMManagedInstanceCore policy is enough --
        # SSM works over an outbound connection the instance itself initiates,
        # so no inbound port or key pair is needed).
        app_sg = ec2.SecurityGroup(
            self,
            "AppSecurityGroup",
            vpc=vpc,
            description="local-image-classifier app instance",
            allow_all_outbound=True,
        )
        app_sg.add_ingress_rule(
            ec2.Peer.prefix_list(cloudfront_prefix_list_id),
            ec2.Port.tcp(APP_PORT),
            "gunicorn, CloudFront origin-facing IPs only",
        )
        # Deliberately no rule for 5432: PostgreSQL binds to localhost only,
        # so there is nothing to expose regardless of SG rules (belt+suspenders).

        # --- system_setup.sh delivered as an asset, fetched by UserData --------
        setup_script_asset = s3_assets.Asset(
            self, "SystemSetupScript", path="scripts/system_setup.sh"
        )
        setup_script_asset.grant_read(instance_role)

        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "set -euo pipefail",
            f"aws s3 cp {setup_script_asset.s3_object_url} /opt/system_setup.sh --region {self.region}",
            "chmod +x /opt/system_setup.sh",
            f"export S3_BUCKET={bucket.bucket_name}",
            f"export AUTH_USERNAME={auth_username}",
            f"export AUTH_PASSWORD={auth_password}",
            f"export AWS_DEFAULT_REGION={self.region}",
            "/opt/system_setup.sh",
        )

        instance = ec2.Instance(
            self,
            "AppInstance",
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.MICRO),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(
                cpu_type=ec2.AmazonLinuxCpuType.ARM_64
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=app_sg,
            role=instance_role,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(20, volume_type=ec2.EbsDeviceVolumeType.GP3),
                )
            ],
            user_data=user_data,
        )

        # --- Elastic IP: keeps the CloudFront origin + SSM tunnel target stable
        eip = ec2.CfnEIP(self, "AppEip", instance_id=instance.instance_id, domain="vpc")

        # --- CloudFront ---------------------------------------------------------
        # Single origin (the EC2 instance) so Flask-HTTPAuth stays the one and
        # only auth gate -- there is deliberately no separate S3-direct origin.
        # CloudFront rejects a bare IP address as a custom origin domain name,
        # so derive AWS's own DNS hostname for the EIP (ec2-1-2-3-4.<region>.
        # compute.amazonaws.com) instead of using eip.ref directly.
        eip_dashed = Fn.join("-", Fn.split(".", eip.ref))
        eip_hostname = f"ec2-{eip_dashed}.{self.region}.compute.amazonaws.com"
        ec2_origin = origins.HttpOrigin(
            eip_hostname,
            http_port=APP_PORT,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
        )

        # Images/thumbnails: cache at the edge, but the cache key MUST include
        # Authorization -- otherwise a cache HIT would skip Flask's auth check
        # entirely and serve images to unauthenticated requests. This is the
        # one place caching and Basic Auth are in tension; including the
        # header in the cache key resolves it (same credentials -> cache hit,
        # no/different credentials -> forwarded to origin -> 401).
        media_cache_policy = cloudfront.CachePolicy(
            self,
            "MediaCachePolicy",
            cache_policy_name=f"{construct_id}-media",
            header_behavior=cloudfront.CacheHeaderBehavior.allow_list("Authorization"),
            query_string_behavior=cloudfront.CacheQueryStringBehavior.none(),
            cookie_behavior=cloudfront.CacheCookieBehavior.none(),
            default_ttl=Duration.days(30),
            max_ttl=Duration.days(365),
            min_ttl=Duration.seconds(0),
            enable_accept_encoding_gzip=True,
            enable_accept_encoding_brotli=True,
        )

        # Search pages / htmx partials: always dynamic, never cached, but the
        # app needs to see the real query string + auth + htmx headers.
        # `Authorization` (and `Accept-Encoding`) can only be forwarded via a
        # CachePolicy's header allow-list, not an OriginRequestPolicy's -- so
        # this uses a near-zero-TTL *custom* CachePolicy (CloudFront's own
        # CACHING_DISABLED managed policy forwards no extra headers/query
        # strings at all, which would break search params and auth here).
        # max_ttl must be > 0: CloudFront rejects a non-none HeaderBehavior on
        # a policy where min/default/max TTL are all exactly 0 ("caching
        # disabled"), so max_ttl=1s keeps it just barely "enabled" while
        # default_ttl=0 still means practically no caching for these routes.
        search_cache_policy = cloudfront.CachePolicy(
            self,
            "SearchCachePolicy",
            cache_policy_name=f"{construct_id}-search",
            header_behavior=cloudfront.CacheHeaderBehavior.allow_list("Authorization"),
            query_string_behavior=cloudfront.CacheQueryStringBehavior.all(),
            cookie_behavior=cloudfront.CacheCookieBehavior.none(),
            default_ttl=Duration.seconds(0),
            max_ttl=Duration.seconds(1),
            min_ttl=Duration.seconds(0),
        )
        search_origin_request_policy = cloudfront.OriginRequestPolicy(
            self,
            "SearchOriginRequestPolicy",
            origin_request_policy_name=f"{construct_id}-search",
            header_behavior=cloudfront.OriginRequestHeaderBehavior.allow_list(
                "HX-Request", "HX-Target", "HX-Current-URL"
            ),
            query_string_behavior=cloudfront.OriginRequestQueryStringBehavior.all(),
            cookie_behavior=cloudfront.OriginRequestCookieBehavior.none(),
        )

        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=ec2_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=search_cache_policy,
                origin_request_policy=search_origin_request_policy,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
            ),
            additional_behaviors={
                "/image/*": cloudfront.BehaviorOptions(
                    origin=ec2_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=media_cache_policy,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                ),
                "/thumb/*": cloudfront.BehaviorOptions(
                    origin=ec2_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=media_cache_policy,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                ),
            },
            domain_names=[domain_name],
            certificate=certificate,
            price_class=cloudfront.PriceClass.PRICE_CLASS_200,
        )

        CfnOutput(self, "InstanceId", value=instance.instance_id)
        CfnOutput(self, "InstancePublicIp", value=eip.ref)
        CfnOutput(self, "MediaBucketName", value=bucket.bucket_name)
        CfnOutput(self, "DistributionDomainName", value=distribution.distribution_domain_name)
