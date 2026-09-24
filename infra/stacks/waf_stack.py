from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

# Paths whose requests are cheap thumbnail/original fetches. A single grid page
# pulls 40 thumbnails (and infinite scroll pulls 40 more per page), so counting
# them against the 100/min page limit blocked ordinary browsing with 403s.
MEDIA_PATH_PREFIXES = ("/thumb/", "/image/")

# Media requests still pass through Flask-HTTPAuth, so they are NOT exempt --
# excluding them entirely would let an attacker brute-force credentials via
# /thumb/1 with no limit. They get their own, much higher, per-IP ceiling.
PAGE_RATE_LIMIT = 100  # AWS WAF's minimum allowed value
MEDIA_RATE_LIMIT = 2000


def _media_path_statement() -> wafv2.CfnWebACL.StatementProperty:
    """Matches requests whose (URL-decoded) path starts with a media prefix."""
    return wafv2.CfnWebACL.StatementProperty(
        or_statement=wafv2.CfnWebACL.OrStatementProperty(
            statements=[
                wafv2.CfnWebACL.StatementProperty(
                    byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
                        field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(uri_path={}),
                        positional_constraint="STARTS_WITH",
                        search_string=prefix,
                        # Flask decodes %xx before routing, so match on the
                        # decoded form too (e.g. "/%74humb/1" -> "/thumb/1").
                        text_transformations=[
                            wafv2.CfnWebACL.TextTransformationProperty(
                                priority=0, type="URL_DECODE"
                            )
                        ],
                    )
                )
                for prefix in MEDIA_PATH_PREFIXES
            ]
        )
    )


def _rate_limit_rule(
    name: str,
    priority: int,
    limit: int,
    scope_down: wafv2.CfnWebACL.StatementProperty,
) -> wafv2.CfnWebACL.RuleProperty:
    return wafv2.CfnWebACL.RuleProperty(
        name=name,
        priority=priority,
        action=wafv2.CfnWebACL.RuleActionProperty(
            block=wafv2.CfnWebACL.BlockActionProperty()
        ),
        statement=wafv2.CfnWebACL.StatementProperty(
            rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                limit=limit,
                evaluation_window_sec=60,  # shortest available window
                aggregate_key_type="IP",
                scope_down_statement=scope_down,
            )
        ),
        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
            sampled_requests_enabled=True,
            cloud_watch_metrics_enabled=True,
            metric_name=f"ImageClassifier{name}",
        ),
    )


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

    WAF counts every request, including CloudFront cache hits, so page/htmx
    requests and media requests are limited separately (see MEDIA_PATH_PREFIXES).
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        media = _media_path_statement()
        not_media = wafv2.CfnWebACL.StatementProperty(
            not_statement=wafv2.CfnWebACL.NotStatementProperty(statement=media)
        )

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
                # Keeps the original rule name/metric so existing CloudWatch
                # history carries over.
                _rate_limit_rule("RateLimitPerIp", 0, PAGE_RATE_LIMIT, not_media),
                _rate_limit_rule("RateLimitMediaPerIp", 1, MEDIA_RATE_LIMIT, media),
            ],
        )

        CfnOutput(self, "WebAclArn", value=self.web_acl.attr_arn)
