"""CDK stack wiring the full AWS-native bird-conservation pipeline."""
from __future__ import annotations

import os

from aws_cdk import (
    Aws,
    BundlingOptions,
    CfnParameter,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Size,
    Stack,
)
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

# Project root = parent of the cdk/ directory; used for asset paths.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAMBDAS_DIR = os.path.join(PROJECT_ROOT, "lambdas")

# Default Claude model on Bedrock (override via context/env if desired).
DEFAULT_MODEL_ID = "anthropic.claude-3-5-sonnet-20240620-v1:0"


class BirdConservationStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        alert_email = CfnParameter(
            self, "alertEmail",
            type="String",
            description="Email address that receives bird alerts (SNS subscription).",
        )
        model_id = self.node.try_get_context("bedrockModelId") or DEFAULT_MODEL_ID

        # ----------------------------------------------------------------- #
        # Storage: raw-audio bucket + detections table                       #
        # ----------------------------------------------------------------- #
        audio_bucket = s3.Bucket(
            self, "RawAudioBucket",
            removal_policy=RemovalPolicy.RETAIN,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            lifecycle_rules=[
                s3.LifecycleRule(
                    transitions=[s3.Transition(
                        storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                        transition_after=Duration.days(30),
                    )],
                )
            ],
        )

        table = dynamodb.Table(
            self, "DetectionsTable",
            partition_key=dynamodb.Attribute(
                name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(
                name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery=True,
        )
        table.add_global_secondary_index(
            index_name="species-index",
            partition_key=dynamodb.Attribute(
                name="GSI1PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(
                name="GSI1SK", type=dynamodb.AttributeType.STRING),
        )

        # ----------------------------------------------------------------- #
        # Alerts: SNS topic + email subscription                             #
        # ----------------------------------------------------------------- #
        alert_topic = sns.Topic(self, "AlertTopic", display_name="Bird Conservation Alerts")
        alert_topic.add_subscription(
            subs.EmailSubscription(alert_email.value_as_string))

        # ----------------------------------------------------------------- #
        # Shared layer: shared/*.py + 'requests'                             #
        # ----------------------------------------------------------------- #
        shared_layer = lambda_.LayerVersion(
            self, "SharedLayer",
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_11],
            code=lambda_.Code.from_asset(
                LAMBDAS_DIR,
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_11.bundling_image,
                    command=[
                        "bash", "-c",
                        "pip install requests -t /asset-output/python && "
                        "cp shared/*.py /asset-output/python/",
                    ],
                ),
            ),
            description="Shared bird-agent modules + requests",
        )

        common_env = {
            "DETECTIONS_TABLE": table.table_name,
            "ALERT_TOPIC_ARN": alert_topic.topic_arn,
            "BEDROCK_MODEL_ID": model_id,
        }

        bedrock_policy = iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[f"arn:aws:bedrock:{Aws.REGION}::foundation-model/*"],
        )

        # ----------------------------------------------------------------- #
        # Alert Lambda (Bedrock agent -> SNS)                                #
        # ----------------------------------------------------------------- #
        alert_fn = lambda_.Function(
            self, "AlertFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.handler",
            code=lambda_.Code.from_asset(os.path.join(LAMBDAS_DIR, "alert")),
            layers=[shared_layer],
            timeout=Duration.seconds(120),
            memory_size=512,
            environment=common_env,
        )
        table.grant_read_data(alert_fn)
        alert_topic.grant_publish(alert_fn)
        alert_fn.add_to_role_policy(bedrock_policy)

        # ----------------------------------------------------------------- #
        # Migration Lambda (scheduled daily)                                 #
        # ----------------------------------------------------------------- #
        migration_fn = lambda_.Function(
            self, "MigrationFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.handler",
            code=lambda_.Code.from_asset(os.path.join(LAMBDAS_DIR, "migration")),
            layers=[shared_layer],
            timeout=Duration.seconds(300),
            memory_size=512,
            environment=common_env,
        )
        table.grant_read_data(migration_fn)
        alert_topic.grant_publish(migration_fn)
        migration_fn.add_to_role_policy(bedrock_policy)

        events.Rule(
            self, "DailyMigrationSchedule",
            schedule=events.Schedule.cron(minute="0", hour="6"),  # 06:00 UTC daily
            targets=[targets.LambdaFunction(migration_fn)],
        )

        # ----------------------------------------------------------------- #
        # Inference Lambda (BirdNET container, S3-triggered)                 #
        # ----------------------------------------------------------------- #
        inference_image = ecr_assets.DockerImageAsset(
            self, "BirdNetImage",
            directory=PROJECT_ROOT,
            file="lambdas/inference/Dockerfile",
            platform=ecr_assets.Platform.LINUX_AMD64,
        )
        inference_fn = lambda_.DockerImageFunction(
            self, "InferenceFunction",
            code=lambda_.DockerImageCode.from_ecr(
                inference_image.repository,
                tag_or_digest=inference_image.image_tag,
            ),
            timeout=Duration.seconds(300),
            memory_size=3008,
            ephemeral_storage_size=Size.mebibytes(2048),
            environment={
                **common_env,
                "ALERT_FUNCTION_NAME": alert_fn.function_name,
                "MIN_CONFIDENCE": "0.5",
                "ALERT_THRESHOLD": "0.7",
            },
        )
        audio_bucket.grant_read(inference_fn)
        table.grant_write_data(inference_fn)
        alert_fn.grant_invoke(inference_fn)

        inference_fn.add_event_source(
            event_sources.S3EventSource(
                audio_bucket,
                events=[s3.EventType.OBJECT_CREATED],
                filters=[s3.NotificationKeyFilter(suffix=".wav")],
            )
        )

        # ----------------------------------------------------------------- #
        # Conversational API (Phase 4): chat Lambda + API Gateway            #
        # ----------------------------------------------------------------- #
        chat_fn = lambda_.Function(
            self, "ChatFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.handler",
            code=lambda_.Code.from_asset(os.path.join(LAMBDAS_DIR, "chat")),
            layers=[shared_layer],
            timeout=Duration.seconds(120),
            memory_size=512,
            environment=common_env,
        )
        table.grant_read_data(chat_fn)
        chat_fn.add_to_role_policy(bedrock_policy)

        api = apigw.RestApi(
            self, "OfficialsApi",
            rest_api_name="bird-conservation-officials-api",
            description="Conversational Q&A over bird detections.",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["POST", "OPTIONS"],
                allow_headers=["Content-Type", "x-api-key"],
            ),
        )
        ask = api.root.add_resource("ask")
        ask.add_method(
            "POST",
            apigw.LambdaIntegration(chat_fn),
            api_key_required=True,
        )

        # API key + usage plan so the endpoint isn't open to the world.
        api_key = api.add_api_key("OfficialsApiKey")
        usage_plan = api.add_usage_plan(
            "OfficialsUsagePlan",
            throttle=apigw.ThrottleSettings(rate_limit=5, burst_limit=10),
        )
        usage_plan.add_api_key(api_key)
        usage_plan.add_api_stage(stage=api.deployment_stage)

        # ----------------------------------------------------------------- #
        # Outputs                                                            #
        # ----------------------------------------------------------------- #
        CfnOutput(self, "AudioBucketName", value=audio_bucket.bucket_name)
        CfnOutput(self, "DetectionsTableName", value=table.table_name)
        CfnOutput(self, "AlertTopicArn", value=alert_topic.topic_arn)
        CfnOutput(self, "BedrockModelId", value=model_id)
        CfnOutput(self, "OfficialsApiUrl", value=f"{api.url}ask")
        CfnOutput(self, "OfficialsApiKeyId", value=api_key.key_id,
                  description="Retrieve the key value: "
                              "aws apigateway get-api-key --api-key <id> --include-value")
