# Deployment guide

## Prerequisites

- An AWS account with credentials configured (`aws configure` / SSO).
- **Docker** running locally (CDK builds the BirdNET container + bundles the layer).
- **Node.js** + AWS CDK CLI: `npm install -g aws-cdk`
- **Python 3.11**.

## 1. Enable Claude on Amazon Bedrock (one-time, per region)

In the AWS console → **Bedrock → Model access**, request/enable access to the
Anthropic Claude model you want (default: `anthropic.claude-3-5-sonnet-20240620-v1:0`).
Make sure the region you deploy to supports the chosen model.

To use a different model, deploy with:

```bash
cdk deploy -c bedrockModelId=anthropic.claude-3-haiku-20240307-v1:0 ...
```

## 2. Install dependencies

```bash
cd bird-conservation-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Bootstrap + deploy

```bash
cd cdk
export CDK_DEFAULT_REGION=us-east-1        # a region with Bedrock + your model
cdk bootstrap                              # first time per account/region
cdk deploy --parameters alertEmail=officials@example.org
```

Deployment builds:
- the BirdNET Docker image (first build is slow — it downloads the model),
- the shared Lambda layer,
- all AWS resources.

After deploy, **confirm the SNS subscription** from the email AWS sends you,
otherwise alerts won't be delivered.

## 4. Test the pipeline

```bash
# from repo root, with the venv active
python scripts/upload_sample.py path/to/birdcall.wav \
    --site Site-1 --lat 12.97 --lon 77.59
```

Then watch it work:
- **CloudWatch Logs** → `InferenceFunction` shows BirdNET detections.
- **DynamoDB** → `DetectionsTable` shows stored detections.
- For a confident detection of a notable species, the **AlertFunction** runs and you
  get an email.

## 5. Migration reports

The `MigrationFunction` runs daily at 06:00 UTC via EventBridge. To test it now:

```bash
aws lambda invoke --function-name <MigrationFunction-name> /dev/stdout
```

(Get the function name from the AWS console or `aws lambda list-functions`.)

## 6. Conversational API (Phase 4)

The stack creates an API-key-protected REST endpoint (`OfficialsApiUrl` output)
backed by the chat Lambda. Get the key value:

```bash
aws apigateway get-api-key \
  --api-key <OfficialsApiKeyId-from-outputs> \
  --include-value --query value --output text
```

Then ask questions:

```bash
curl -X POST "<OfficialsApiUrl>" \
  -H "x-api-key: <key-value>" \
  -H "Content-Type: application/json" \
  -d '{"question": "What was detected at Site-1 in the last 14 days?"}'
```

The chat agent has read-only tools (`query_detections`, `get_bird_info`,
`get_bird_image`) — it cannot send alerts, so the endpoint can't be abused to
spam officials.

## Local testing (no deploy required)

Run BirdNET + the alert agent on your laptop first:

```bash
pip install -r requirements-dev.txt
python scripts/local_test.py sample.wav --lat 12.97 --lon 77.59 --alert
```

The agent runs in DRY-RUN mode (`DRY_RUN=1` is set automatically) so it prints
the alert instead of sending SNS. The `--alert` / `--ask` modes still call
Amazon Bedrock, so you need AWS credentials and Claude model access; plain
classification (no flags) is fully offline.

## Tuning knobs (Lambda environment variables)

| Variable                     | Where        | Meaning                                   |
|------------------------------|--------------|-------------------------------------------|
| `MIN_CONFIDENCE`             | inference    | BirdNET min confidence to record          |
| `ALERT_THRESHOLD`            | inference    | confidence above which a real-time alert fires |
| `ARRIVAL_RECENT_DAYS`        | migration    | "recent" window for arrivals              |
| `ARRIVAL_LOOKBACK_DAYS`      | migration    | prior window that must be empty for arrival |
| `DEPARTURE_SILENCE_DAYS`     | migration    | silence length to suspect departure       |
| `DEPARTURE_MIN_REGULAR_DAYS` | migration    | how long a species must have been regular |

## Cost notes

All services are serverless / pay-per-use: S3, DynamoDB on-demand, Lambda, SNS,
Bedrock per-token. With light traffic this runs at a few dollars/month plus
Bedrock usage. The BirdNET container Lambda is the main compute cost; tune its
memory (`InferenceFunction`) for the cost/latency trade-off.

## Cleanup

```bash
cd cdk
cdk destroy
```

Note: the S3 bucket and DynamoDB table use `RETAIN`, so they survive `destroy`.
Delete them manually if you want them gone.
