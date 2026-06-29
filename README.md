# BridgeBrief
BridgeBrief is a guided outbound sales workflow for small B2B teams. It calls the rep first with a short lead briefing, bridges the prospect from a trusted local number, records the conversation, and preserves follow-up state for later review.

## Why it exists
Small teams still rely on live outbound sales, but most lead lists are low-context and hard to work through efficiently. BridgeBrief turns a raw lead batch into a more structured workflow:

- vet a small lead list
- call the rep first
- brief the rep before the prospect answers
- bridge the lead from a trusted outbound number
- save the recording and call result afterward

## What's in this repo
- `bridgebrief/` — public-facing Vercel demo page used for the hackathon submission
- `scripts/outbound_call_app.py` — one-off rep-first bridge caller
- `scripts/batch_outbound_call_runner.py` — sequential batch runner for controlled live tests
- `scripts/bridgebrief-demo-leads.csv` — sanitized single-lead demo batch
- `proof/` — DynamoDB proof artifacts for the submission

## Live demo page
`https://optin.binary-cs.cv/bridgebrief`

## Public-safe demo flow
Dry run:

```powershell
python scripts\batch_outbound_call_runner.py --agent-number +13053024013 --dry-run
```

Single live test:

```powershell
python scripts\batch_outbound_call_runner.py --agent-number +13053024013 --limit 1
```

## Built with
- Vercel
- DynamoDB
- Twilio
- Python
- HTML
- CSS

## DynamoDB proof
The hackathon submission includes a real DynamoDB table named `BridgeBriefDemo` with sanitized lead and call-session records. Proof artifacts are included in:

- `proof/describe-table-clean.json`
- `proof/query-demo-items-clean.json`
- `proof/proof-summary.txt`

## Notes
- This repo is sanitized for public sharing.
- It does not include private `.env` files or account secrets.
- The included lead CSV is a public-safe demo fixture, not the original outreach list.
