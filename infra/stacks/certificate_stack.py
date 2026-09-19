from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_certificatemanager as acm
from constructs import Construct


class CertificateStack(Stack):
    """us-east-1 ACM certificate for the CloudFront distribution.

    No Route53 hosted zone exists in this account, so validation is DNS-based
    but unmanaged by CDK -- the validation CNAME must be added manually in
    Cloudflare (see infra/README.md).
    """

    def __init__(self, scope: Construct, construct_id: str, *, domain_name: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.certificate = acm.Certificate(
            self,
            "SiteCertificate",
            domain_name=domain_name,
            validation=acm.CertificateValidation.from_dns(),
        )

        CfnOutput(self, "CertificateArn", value=self.certificate.certificate_arn)
