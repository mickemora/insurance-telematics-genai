# Demo script: telematics as a gen AI differentiator

Length: about 10 minutes. Audience: business and technology leaders at an auto insurer.

The point to land: **every carrier has a telematics discount app. The difference is what you do
with the data after pricing.** Gen AI turns trip data into words that members and staff can act on,
and the guardrails make it safe to put in front of them.

All data is synthetic. The example insurer serves military members and their families.

## Before you start (15 minutes before)

```bash
cdk deploy -c site_password=<something>      # if not already deployed
make generate                                 # fresh drivers, trips, and 3 crashes
make claims                                   # confirm 3 claims are waiting for a statement
make site                                     # URL and user for the browser
```

Open three tabs:

1. The web app, signed in (user from `make site`, your password).
2. AWS console, **Step Functions**, the `InsuranceTelematicsGenAI-claims` state machine.
3. A terminal in the repo.

Statements have to arrive within 30 minutes of `make generate`. Regenerate if you're past that.

## 1. The problem (1 minute, no clicks)

> "Most of the industry uses telematics for one thing: a discount. Members see a number, and when
> it drops they call us asking why. When they crash, the app knows before we do, but someone still
> has to type up the claim. We built three things on top of the same data: coaching that explains
> the score, a claim that starts itself, and a copilot for our people."

## 2. Explain the score (3 minutes)

**Drivers** tab, then **Victor F. (DRV-0020)**.

- Point at the sign: score 28, down about 17 from the week before. *"This is all most apps show."*
- Click **Explain this score with AI**.
  > "This is what Victor would read in the app: what changed, when it happens, and two or three
  > things he can do about it. No lecture, no pricing."
- Open **What the model saw**.
  > "The model never calculates anything. Python computes these facts; Claude only puts them into
  > words. Every number in the explanation is checked against this list before it is shown."
- Point at the provenance line: model, guardrail passed, tokens, milliseconds.
  > "If any check fails, or the model is down, the member gets a plain template. They never see
  > unchecked text."
- Optional contrast: **Aisha V. (DRV-0003)**, whose score improved. The tone changes to recognition.

Business value: fewer "why did my score drop" calls, more engagement with safe-driving coaching.

## 3. Crash to claim (4 minutes)

**Claims** tab.

- Two rows say **Closed: false alarm**. Open one.
  > "A phone dropped in the footwell looks like a crash to the sensor. Rules, not AI, see that the
  > car kept going at speed, so we send a short 'we noticed a jolt' note instead of opening a claim."
- Open a claim that is **Waiting for member**.
  - Walk the timeline: speed, braking, impact, stopped for 3 minutes.
  - Read the message sent to the member. *"Are you okay? If anyone is hurt, call 911 first."*
- Switch to the **Step Functions** tab and open the running execution. It is paused on
  **Ask member for statement**.
- Back in the app: **Fill inconsistent example**, then **Send statement**.
  - Watch the Step Functions graph move to **Draft FNOL with Claude**.
  - The draft appears: summary, then a red **Statement contradicts data** with the specific numbers,
    then questions for the adjuster.
  > "What the member says doesn't match what the phone recorded. The AI doesn't decide who's right
  > or who's at fault. It hands the adjuster the specific discrepancy and the questions to ask."
- Point at the footer line: *"The AI drafts; an adjuster decides."*

Business value: shorter claim cycle time, structured first notice of loss in minutes, earlier
detection of inconsistent statements (subrogation and fraud).

## 4. The copilot (2 minutes)

**Copilot** tab.

- Click **Summarize DRV-0020's last 10 trips**. Answers take 5 to 20 seconds.
- Expand **How it answered**.
  > "This is an Amazon Bedrock Agent. It decided which lookups to make, and every one is read-only
  > and logged. Same guardrail: ask it about premiums or fault and it declines."
- Ask a follow-up in the same conversation: *"Which claims have statements that don't match the data?"*

Business value: service agents and adjusters get answers without navigating five screens.

## 5. Close (30 seconds)

> "Same telematics data, three new experiences: members understand their score, claims start
> themselves, and our people get a copilot. Everything you saw is in one CDK app that deploys in
> about ten minutes and tears down completely."

Then, if time allows, run `cdk destroy` and `make verify-clean` live.

## If something goes wrong

| Symptom | Fix |
|---|---|
| Explanation says "Template fallback: AccessDenied" | Model access isn't enabled in Bedrock for this region. Show the fallback anyway: it is the safety net working. |
| No claims waiting | More than 30 minutes since `make generate`. Run it again; claims open within a minute. |
| Copilot error or no answer | Check the `CopilotWorker` Lambda logs. Fall back to `make ask Q="..."` in the terminal. |
| Browser keeps asking for the password | User is in `make site`; the password is what you deployed with (default `telematics-demo`). |
