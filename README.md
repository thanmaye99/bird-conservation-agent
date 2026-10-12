# Bird Call Conservation Agent (AWS-native)

An AI agent for wildlife conservation that listens to bird-call audio, identifies the
species, notifies forest officials with the bird's name + picture + conservation status,
and detects **migration events** (a species newly arriving, or a once-regular species
going silent / departing).

The system uses a **two-brain design**:

1. **The "ear" — BirdNET** (Cornell Lab's bioacoustic model) does the actual audio →
   species classification. LLMs cannot classify raw audio, so we use the right tool.
2. **The "brain" — Claude on Amazon Bedrock** orchestrates everything else: composing
   rich notifications, reasoning about migration trends, hedging on uncertainty, and
   answering officials' questions, all via tool/function calling.

Everything runs inside AWS.

## Architecture

```
Recorder/phone ──upload .wav──► S3 (raw-audio)
                                   │ ObjectCreated event
                                   ▼
                         Lambda (BirdNET container)  ── species + confidence
                                   │
                                   ▼
                         DynamoDB "Detections" (PK=site, SK=ts, GSI=species)
                                   │
            ┌──────────────────────┼───────────────────────┐
            ▼                       ▼                        ▼
   Alert Lambda            EventBridge daily cron      (future) Officials
   (rare/new species)      → Migration Lambda          chat API
            │                       │
            └───────────┬───────────┘
                        ▼
            Amazon Bedrock — Claude (agent brain)
            tools: query_detections, get_bird_info,
                   get_bird_image, analyze_migration, send_alert
                        │
                        ▼
                  Amazon SNS ──► email / SMS to officials
```

## AWS services used

| Concern              | Service                                   |
|----------------------|-------------------------------------------|
| Audio ingestion      | Amazon S3 (extensible to AWS IoT Core)    |
| Species inference    | AWS Lambda (container image, BirdNET)     |
| Detection store      | Amazon DynamoDB                           |
| Agent brain (LLM)    | Amazon Bedrock (Anthropic Claude)         |
| Scheduling           | Amazon EventBridge (cron)                 |
| Alerts               | Amazon SNS (email + SMS)                  |
| Infra as code        | AWS CDK (Python)                          |

## Repository layout

```
bird-conservation-agent/
├── cdk/                      # AWS CDK app (infrastructure as code)
│   ├── app.py
│   ├── cdk.json
│   └── stacks/
│       └── bird_conservation_stack.py
├── lambdas/
│   ├── inference/            # BirdNET container Lambda (S3-triggered)
│   │   ├── Dockerfile
│   │   ├── handler.py
│   │   └── requirements.txt
│   ├── alert/                # real-time alert (Bedrock + SNS)
│   │   └── handler.py
│   ├── migration/            # scheduled arrival/absence detection
│   │   └── handler.py
│   ├── chat/                 # conversational Q&A API (Phase 4)
│   │   └── handler.py
│   └── shared/               # shared library packaged into the Lambdas
│       ├── detections.py     # DynamoDB access layer
│       ├── bedrock_agent.py  # Claude (Bedrock) wrapper + tool loop
│       ├── bird_info.py      # GBIF / Wikimedia / IUCN lookups
│       └── notify.py         # SNS publishing
├── scripts/
│   ├── upload_sample.py      # upload a local .wav to the ingest bucket
│   └── local_test.py        # run BirdNET + the agent locally (no deploy needed)
├── requirements.txt
├── requirements-dev.txt     # local test-harness deps
└── README.md
```

## Try it locally before deploying

You can run BirdNET and the Claude alert agent on a sample clip without any AWS
infrastructure (the agent runs in DRY-RUN mode and prints alerts instead of
sending them):

```bash
pip install -r requirements-dev.txt

# Just classify a clip:
python scripts/local_test.py sample.wav --lat 12.97 --lon 77.59

# Also run the alert agent on the top detection (needs AWS creds + Bedrock access):
python scripts/local_test.py sample.wav --lat 12.97 --lon 77.59 --alert
```

## Conversational API for officials (Phase 4)

After deploy, officials can ask natural-language questions over the detection
data via an API-key-protected endpoint:

```bash
curl -X POST "$OFFICIALS_API_URL" \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"question": "What was detected at Site-2 this week? Any threatened species?"}'
```

`OfficialsApiUrl` and `OfficialsApiKeyId` are printed as stack outputs. Retrieve
the key value with:

```bash
aws apigateway get-api-key --api-key <OfficialsApiKeyId> --include-value \
  --query value --output text
```

## Quick start

See [`DEPLOY.md`](./DEPLOY.md) for full deployment steps. In short:

```bash
# 1. Install deps
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Enable Claude model access in the Bedrock console (one-time, per region)

# 3. Deploy infra
cd cdk
cdk bootstrap        # first time only
cdk deploy --parameters alertEmail=you@example.org

# 4. Upload a test clip
python ../scripts/upload_sample.py path/to/birdcall.wav --site "Site-1"
```

## How migration detection works

- **Arrival (migrated IN):** the daily job flags a species detected this season that
  has not been heard in the trailing `ARRIVAL_LOOKBACK_DAYS` window — a likely seasonal
  arrival.
- **Departure (migrated AWAY):** the job flags a species that was regularly detected for
  weeks and then went silent for `DEPARTURE_SILENCE_DAYS`. Because *absence is not proof*
  (a recorder could be dead, or it's just breeding-season quiet), the job runs a
  **global-silence health check** — if *all* species went quiet at once, it's flagged as a
  likely hardware fault, not migration. Claude then expresses appropriate confidence in
  the alert.
```
