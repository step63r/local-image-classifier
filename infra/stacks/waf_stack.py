from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

# ISO 3166-1 alpha-2. 個人利用で国内からしか使わないため、それ以外は一律遮断する。
ALLOWED_COUNTRIES = ("JP",)


def _geo_block_rule(priority: int) -> wafv2.CfnWebACL.RuleProperty:
    """Blocks every request whose source country is not in ALLOWED_COUNTRIES."""
    return wafv2.CfnWebACL.RuleProperty(
        name="BlockNonJapan",
        priority=priority,
        action=wafv2.CfnWebACL.RuleActionProperty(
            block=wafv2.CfnWebACL.BlockActionProperty()
        ),
        statement=wafv2.CfnWebACL.StatementProperty(
            not_statement=wafv2.CfnWebACL.NotStatementProperty(
                statement=wafv2.CfnWebACL.StatementProperty(
                    geo_match_statement=wafv2.CfnWebACL.GeoMatchStatementProperty(
                        country_codes=list(ALLOWED_COUNTRIES)
                    )
                )
            )
        ),
        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
            sampled_requests_enabled=True,
            cloud_watch_metrics_enabled=True,
            metric_name="ImageClassifierBlockNonJapan",
        ),
    )


class WafStack(Stack):
    """us-east-1 WAF WebACL for the CloudFront distribution (CLOUDFRONT-scope
    WebACLs must live in us-east-1 regardless of where the distribution's
    origin is -- same constraint as the ACM certificate, see certificate_stack.py).

    国別ブロックのみ。レート制限は固定費(ルール1本あたり月$1)に見合わないため置かない。
    ブルートフォース対策はアプリ側(app.py のログイン時の遅延)に任せる。
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
            rules=[_geo_block_rule(0)],
        )

        CfnOutput(self, "WebAclArn", value=self.web_acl.attr_arn)
