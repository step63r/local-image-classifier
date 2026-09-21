from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct


class WafStack(Stack):
    """us-east-1 WAF WebACL for the CloudFront distribution (CLOUDFRONT-scope
    WebACLs must live in us-east-1 regardless of where the distribution's
    origin is -- same constraint as the ACM certificate, see certificate_stack.py).

    Only a coarse volumetric rate limit against Basic Auth brute-forcing.
    AWS WAF rate-based rules have a hard floor of 100 requests per evaluation
    window (the shortest window is 60s) -- there is no way to configure a
    lower threshold like "5 requests/minute" through this mechanism. Blocking
    slow, low-rate manual guessing would require application-level throttling
    instead.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.web_acl = wafv2.CfnWebACL(
            self,
            "WebAcl",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(
                allow=wafv2.CfnWebACL.AllowActionProperty()
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                sampled_requests_enabled=True,
                cloud_watch_metrics_enabled=True,
                metric_name="ImageClassifierWebAcl",
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="RateLimitPerIp",
                    priority=0,
                    action=wafv2.CfnWebACL.RuleActionProperty(
                        block=wafv2.CfnWebACL.BlockActionProperty()
                    ),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=100,  # AWS WAF's minimum allowed value
                            evaluation_window_sec=60,  # shortest available window
                            aggregate_key_type="IP",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        sampled_requests_enabled=True,
                        cloud_watch_metrics_enabled=True,
                        metric_name="ImageClassifierRateLimitPerIp",
                    ),
                )
            ],
        )

        CfnOutput(self, "WebAclArn", value=self.web_acl.attr_arn)
