# insurance-telematics-genai

A deploy-and-destroy demo showing how an auto insurer can turn telematics data
into a differentiator with gen AI. Everything runs in your own AWS account,
is defined in **AWS CDK (Python)**, and uses **synthetic data only**.

| Day | Feature | Status |
|-----|---------|--------|
| 1 | Foundation: synthetic trip generator, rule-based scoring, weekly aggregates | ✅ done |
| 2 | Score explainer and coach (Claude on Bedrock + Guardrail) behind an API | ✅ done |
| 3 | Possible crash → triage → member statement → FNOL draft (EventBridge + Step Functions + Bedrock) | ✅ done |
| 4 | Web app (CloudFront + passcode), Amazon Bedrock Agent copilot, demo script | ✅ done |

## The whole thing

```mermaid
flowchart LR
    U[Browser] -->|passcode| CF[CloudFront]
    CF -->|/| S3W[(S3 web app)]
    CF -->|/api/* + API key| AG[API Gateway]
    AG --> API[API Lambda]
    API --> DDB[(DynamoDB)]
    API --> BR[Claude on Bedrock<br/>+ Guardrail]
    API -->|async job| W[Copilot worker]
    W --> AGENT[Bedrock Agent]
    AGENT -->|read-only tools| T[Agent tools Lambda] --> DDB
    GEN[Generator] --> S3R[(S3 raw trips)] --> Q[SQS] --> SC[Scoring] --> DDB
    SC -->|crash event| EB{{EventBridge}} --> SF[Step Functions<br/>claims workflow]
    SF --> BR
    SF --> DDB
    API -.->|statement resumes| SF
```

The demo talk track is in [docs/demo-script.md](docs/demo-script.md).

## Architecture (Day 1)

```mermaid
flowchart LR
    G[Generator Lambda] -->|raw/trips/*.json| S3[(S3 raw bucket)]
    G -->|PROFILE items| DDB[(DynamoDB)]
    G -->|ground-truth/ manifest| S3
    S3 -->|ObjectCreated| Q[SQS queue]
    Q -->|batches of 10| SC[Scoring Lambda]
    Q -.->|3 failures| DLQ[Dead-letter queue]
    SC -->|TRIP# + WEEK# items| DDB
    SC -->|PossibleCrashDetected| EB{{EventBridge}}
    EB --> LG[Log group<br/>Day 3: claims workflow]
```

**Generator** builds drivers in six metros with hidden personas (steady,
rusher, distracted, night owl, new driver) and week-over-week trends (some
improve, some decline). Each trip looks like a phone SDK upload: GPS/speed
samples every 10 s plus on-device events (hard brake, rapid acceleration,
harsh cornering, phone handling). It also plants a few **real crashes** and a
few **false alarms** (phone dropped in the footwell) as `possible_crash`
events with a 1 Hz window around the impact. The answer key (personas, which
crashes are real) goes to `ground-truth/` and is never scored.

**Scoring** is deliberately simple and explainable: start at 100 and deduct
capped points per factor. The per-factor `penalties` breakdown is what the
Day 2 explainer turns into plain-English coaching.

| Factor | Points | Cap |
|---|---|---|
| Hard braking | 2.0 per event per 100 mi | 25 |
| Rapid acceleration | 1.5 per event per 100 mi | 15 |
| Harsh cornering | 1.5 per event per 100 mi | 10 |
| Speeding (>10 mph over) | 1.0 per % of moving time | 20 |
| Phone handling | 4.0 per minute per driving hour | 25 |
| Late night (00:00–04:00 local) | 0.25 per % of driving time | 10 |

Rates use a 10-mile exposure floor so one event on a short trip doesn't wreck
the score. Weekly scores are computed from summed exposure and counts, not by
averaging trip scores. Possible crashes are not penalized; they go to claims.

### DynamoDB layout (single table)

| `driver_id` | `sk` | Holds |
|---|---|---|
| `DRV-0001` | `PROFILE` | name, policy, vehicle, metro, UTC offset |
| `DRV-0001` | `TRIP#<start_utc>#<trip_id>` | score, band, rates, penalties, totals, possible crashes, raw S3 key |
| `DRV-0001` | `WEEK#2026-W41` | summed counters (score derived on read) |

The trip write and the weekly `ADD` run in one transaction guarded by
`attribute_not_exists`, so SQS redeliveries never double count.

## Day 2: score explainer and coach

```mermaid
flowchart LR
    C[Client / demo.py] -->|x-api-key| AG[API Gateway<br/>usage plan + throttling]
    AG --> API[Driver API Lambda]
    API -->|query driver| DDB[(DynamoDB)]
    API -->|1. facts in Python| F[Fact sheet]
    F -->|2. Converse, forced tool output| BR[Claude on Bedrock]
    BR -->|3. validate numbers + factors| V{OK?}
    V -->|4. ApplyGuardrail| GR[Bedrock Guardrail]
    V -.->|no| FB[Template fallback]
    GR -.->|intervened| FB
    API -->|cache per driver/week/model| DDB
```

| Route | Gen AI | Returns |
|---|---|---|
| `GET /drivers` | no | every driver with latest weekly score and change |
| `GET /drivers/{id}/score` | no | weekly scores, penalty breakdown, last 10 trips |
| `GET /drivers/{id}/explanation[?refresh=true]` | yes | headline, summary, 2-3 tips, the facts used, and provenance |

**Facts first.** `lambdas/shared/python/facts.py` computes everything the model
may say: this week vs last week, points lost per factor, which factor moved
most, *when* the top factor tends to happen (for example "weekday evening
commute, 4-7 PM: 58% of hard braking on 30% of miles"), the two worst trips,
and strengths. The model only puts these into words; it never calculates.

**Three checks before anything reaches the driver:**

1. *Structured output:* the Converse API forces a `submit_explanation` tool call
   (headline, summary, tips with a factor enum).
2. *Grounding in code:* every number in the headline and summary must match a
   fact (rounding allowed), every tip must name a factor that actually cost
   points, and pricing or fault language is rejected.
3. *Bedrock Guardrail:* `ApplyGuardrail` on the output denies insurance pricing
   promises and fault or legal advice, and filters harmful content.

If any check fails, or Bedrock is unavailable, the API returns a deterministic
template explanation with `source: "fallback"` and the reason, so the endpoint
never returns unchecked text. Model explanations are cached in DynamoDB until the
week's facts or the model change.

**Model.** Defaults to `us.anthropic.claude-haiku-5-5` (set in `cdk.json`). Try
another model without editing code:

```bash
cdk deploy -c bedrock_model_id=us.anthropic.claude-sonnet-5-5
```

IAM is scoped to that inference profile and its foundation model only.

## Day 3: crash to claim draft

```mermaid
flowchart LR
    SC[Scoring Lambda] -->|PossibleCrashDetected| EB{{EventBridge}}
    EB --> E[Enrich: crash facts,<br/>street address, triage]
    E --> T{Triage}
    T -->|likely false alarm| FA[Close + 'we noticed a jolt' message]
    T -->|likely crash / needs review| W[Ask member for statement<br/>waitForTaskToken]
    M[Member: POST /claims/id/statement] -.->|SendTaskSuccess| W
    W -->|statement or timeout| D[Draft FNOL with Claude<br/>checks + Guardrail]
    D --> DB[(Claim: draft ready for adjuster)]
```

The workflow is a Step Functions **Standard** state machine, so every claim is
a visual execution in the console.

1. **Enrich.** Loads the raw trip from S3 and computes crash facts in Python
   from the 1 Hz window around the impact: speed 5 seconds before and at impact,
   whether and when braking started, impact force and direction, how long the
   vehicle stayed stopped, local time, and a street address from Amazon
   Location (Places API, no resource to manage). The driving score is
   deliberately left out of the claim.
2. **Triage (rules, not AI).** *Likely crash*: high-force impact, then the
   vehicle stopped and stayed stopped. *Likely false alarm*: a spike while
   the vehicle kept moving at speed (phone dropped in the footwell). False
   alarms are closed with a short member message.
3. **Member check-in.** The workflow parks a task token and waits for
   `POST /claims/{id}/statement`. With no answer within 30 minutes
   (`-c statement_timeout_minutes=N`), it drafts anyway and the adjuster sees
   "no statement".
4. **FNOL draft.** Deterministic `statement_checks` compare what the member
   says with the data (speed, impact direction, braking). Claude writes the
   incident summary, a statement-vs-data verdict with the specific data
   points, questions for the adjuster, and next steps. Code rejects numbers
   that are not in the facts or the statement, any "consistent" verdict that
   contradicts a failed check, and fault, liability, or payout language. The
   Bedrock Guardrail runs on the output, and a template draft is the fallback.

The model drafts and an adjuster decides. Nothing determines fault, coverage,
or payment.

| Route | Purpose |
|---|---|
| `GET /claims` | claims with status, triage, and verdict |
| `GET /claims/{id}` | crash facts, timeline, member message, statement, checks, FNOL draft |
| `POST /claims/{id}/statement` | `{"statement": "..."}` resumes the waiting workflow |

Claims live in the same table under partition key `CLAIMS`, with the claim id as
the sort key. Claim ids derive from the trip id, so duplicate events never open
two claims.

## Day 4: web app and Bedrock Agent copilot

**Web app.** One CloudFront URL serves the static site (S3, private, origin access
control) and proxies `/api/*` to API Gateway. A CloudFront Function checks a
username and password (HTTP basic auth) on every request and strips the `/api`
prefix. CloudFront adds the API key as an origin header from Secrets Manager, so
the key never reaches the browser. The app is plain HTML, CSS, and JavaScript
with Chart.js from a CDN, so there is no build step.

- **Drivers:** score as a road-sign plate, weekly chart, points lost per factor,
  recent trips, and *Explain this score with AI* with the facts the model saw.
- **Claims:** triage, a lane-strip timeline of what the phone recorded, the
  member message, a statement form (with truthful and inconsistent examples),
  the FNOL draft, and a link to the run in the Step Functions console.
- **Copilot:** chat with the Bedrock Agent; *How it answered* lists every tool
  call and the agent's reason for it.

```bash
cdk deploy -c site_password='pick-something'   # user defaults to "demo" (-c site_user=...)
make site                                       # prints the URL
```

Without `site_password` the stack deploys with the default `telematics-demo` and
CDK prints a warning. The credentials end up in the CloudFront Function code,
which is fine for a demo gate but is not real authentication; use Cognito for
anything beyond a demo.

**Copilot (Amazon Bedrock Agent).** `CfnAgent` with the same model and guardrail
as Days 2 and 3, and one action group, `TelematicsData`, defined with function
schemas and backed by a Lambda that reuses the API's data access:

| Tool | Returns |
|---|---|
| `list_drivers` | every driver with latest score, band, and change |
| `get_driver_score(driver_id)` | weekly scores and points lost per factor |
| `get_driver_trips(driver_id, limit)` | most recent trips with points lost and event counts |
| `get_score_explanation(driver_id)` | the Day 2 explanation (cached) |
| `list_claims(status)` | claims with triage and verdict |
| `get_claim(claim_id)` | crash facts, statement checks, and the FNOL draft |

All tools are read-only. Agents can take longer than API Gateway's 29-second
limit, so `POST /copilot` stores a job and invokes a worker Lambda asynchronously,
and the client polls `GET /copilot/{job_id}`. The worker calls `InvokeAgent` with
tracing on and stores the answer, every tool call with the agent's rationale,
token usage, and whether the guardrail intervened. The worker uses the built-in
`TSTALIASID` alias (the agent's working draft), which `auto_prepare` refreshes on
every deploy; a versioned alias would need a new version for every change.

```bash
make ask Q="Which drivers dropped the most this week, and why?"
python scripts/demo.py ask "And what should I tell them?" --session cli-1234567890
```

## Prerequisites

- AWS account and credentials (`aws sts get-caller-identity` works)
- Node.js 18+ and the CDK CLI: `npm install -g aws-cdk`
- Python 3.11+
- CDK bootstrapped once per account/region: `cdk bootstrap`
- Day 2: access to the Claude model in Amazon Bedrock for your region (check with
  `aws bedrock list-inference-profiles`)
- Day 4: Amazon Bedrock Agents in your region, with a model that Agents supports
  (switch with `-c bedrock_model_id=...` if the agent fails to prepare)

## Deploy, run, destroy

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

make test        # unit + CDK template tests
cdk deploy       # ~3 minutes

make generate    # 20 drivers x 28 days (~1,000 trips), waits for scoring
make scores      # weekly score per driver
python scripts/demo.py trips DRV-0020   # one driver's trips + penalty breakdown
python scripts/demo.py crashes          # possible crashes (real and false alarms)

make explain DRIVER=DRV-0020            # Day 2: AI explanation and coaching
python scripts/demo.py explain DRV-0003 --facts   # ...plus the facts sent to the model
make eval                               # explain every driver, check against hidden personas

make claims                             # Day 3: claims opened by the crash workflow
python scripts/demo.py claim CLM-XXXXXXXX
python scripts/demo.py statement CLM-XXXXXXXX                 # truthful statement -> draft
python scripts/demo.py statement CLM-XXXXXXXX --inconsistent  # contradicts the data -> flagged
make eval-claims                        # triage + cross-check vs ground truth (right after generate)

make site                               # Day 4: web app URL (sign in with your site_password)
make ask Q="Summarize DRV-0020's last 10 trips"   # Day 4: Bedrock Agent copilot from the terminal

cdk destroy      # removes everything, including bucket contents and logs
make verify-clean   # confirm nothing tagged with the project is left
```

`make generate` resets previous data by default. Options:
`python scripts/demo.py generate --drivers 50 --days 42 --seed 7 --crashes 5`.

No AWS handy? `make local` runs the same generator and scoring offline, writes
JSON to `./local_data`, and prints weekly scores next to the hidden personas.

## Cost

Pay-per-use only: Lambda, S3, SQS, DynamoDB on-demand, EventBridge, API
Gateway, Bedrock, Guardrails, Step Functions, and Amazon Location. A full generate run costs well under $0.10.
A full `make eval` (20 explanations of roughly 2,000 tokens each, plus 20
guardrail checks) costs a few cents with Haiku. A Day 3 run is a handful of
state transitions, 3 reverse geocodes, and 3 claim drafts: well under $0.05.

Day 4 adds CloudFront (requests and transfer, pennies for a demo), one Secrets
Manager secret (about $0.40 a month, prorated while deployed), and Bedrock Agent
calls: a copilot question typically takes 2 to 4 model calls of a few thousand
tokens each, so a few cents per 10 questions with Haiku.

| Left running for a month, idle | Roughly |
|---|---|
| Secrets Manager secret | $0.40 |
| Everything else (on demand, no traffic) | $0 to $1 |

`make verify-clean` after `cdk destroy` lists anything still tagged with the
project. The secret can show as pending deletion during Secrets Manager's recovery
window, and CDK's own helper Lambdas can leave empty log groups behind. Nothing runs between invocations, and
`cdk destroy` leaves nothing behind (the CDK bootstrap stack is shared and
stays).

## Repo layout

```
app.py                       CDK app
telematics/telematics_stack.py
lambdas/generator/           synth.py (pure generator) + handler.py
lambdas/scoring/             handler.py (S3 -> SQS -> score -> DynamoDB + EventBridge)
lambdas/shared/python/       Lambda layer: score.py, facts.py (explainer), crash.py (claims), llm.py (Bedrock)
lambdas/api/                 handler.py (routes) + explain.py (Bedrock, validation, fallback)
                             agent_tools.py (Bedrock Agent action group) + copilot_worker.py
lambdas/claims/              handlers.py (4 workflow tasks) + draft.py (FNOL draft, validation, fallback)
web/                         index.html, styles.css, app.js (no build step)
docs/demo-script.md          10-minute talk track
scripts/demo.py              generate / scores / trips / crashes / explain / eval / claims / claim /
                             statement / eval-claims / site / ask / verify-clean / local
tests/                       pytest: scoring, generator, facts + explainer, handlers (fakes), CDK template
```

All data is synthetic. Names, policies, and trips are generated and do not
describe real people.
