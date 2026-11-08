# OneJourney · Airtel SYNC 2026

OneJourney is a local hackathon prototype for small-business telecom onboarding. An owner describes their needs to a seven-language advisor, compares eligible bundles, reviews a quote, submits documents, and tracks the expected activation date. The demo uses fictional business records and simulates registry checks, payments, installation updates, and messaging.

[Watch the 54-second demo](submission/OneJourney_Demo.mp4) · [Architecture diagram](submission/architecture.svg) · [Seven-slide business case](submission/OneJourney_Business_Case.pptx) · [Privacy note](PRIVACY.md)

**Try the reviewer workflow.** Applicant K10's signatory ID and application name disagree. OneJourney pauses checkout, lets the owner authorize sharing, and gives a partner the evidence needed to record a decision. Approval applies to the reviewed identity and quote. [Run this case locally](#try-the-review-case).

The FastAPI backend controls eligibility, pricing and order state. Ollama handles conversation, and guided discovery uses the same catalogue recommender when the model is unavailable or slow. The partner desk adds consented handoffs, synthetic lead prioritization and cohort analytics.

## Run locally

Requirements: Python 3.11+, the Tesseract OCR executable, and Ollama with the local `qwen3:8b` model.

### Docker Compose

The Compose stack starts the app and a local Ollama service. The pull command downloads the model into a persistent local volume:

```powershell
docker compose up --build -d
docker compose exec ollama ollama pull qwen3:8b
```

Open <http://127.0.0.1:8000>. Both services bind to localhost on the host. The first model pull needs internet access; inference stays inside the local Docker network. If the model is missing, offline, or slow, the business questionnaire and deterministic recommender remain available.

### Python on Windows

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
ollama pull qwen3:8b
if (!(Test-Path .env)) { Copy-Item .env.example .env }
# Ollama defaults to http://127.0.0.1:11434; keep the Ollama app running.
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. The API health check is `/api/health`, and interactive API documentation is at `/docs`. The advisor status reports whether Ollama is running and the selected model is installed. When Ollama is unavailable or takes about 20 seconds to answer, the advisor switches to guided discovery. The business questionnaire is also available directly. The WhatsApp-style view is a labelled simulation that calls the same advisor endpoint.

With demo mode enabled, **Explore clinic demo** opens K02. Choose any K01–K12 applicant before quoting. After accepting the quote, give KYC consent, import the selected demo specimens, review each extraction, then verify. Tesseract must be installed to process documents.

## Access and reviewer demo

Copy `.env.example` to the ignored `.env`. Generate two distinct keys locally with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and set `ONEJOURNEY_PARTNER_KEY` and `ONEJOURNEY_OPS_KEY`. Set `ONEJOURNEY_DEMO_MODE=1` only to enable fictional specimen import. Restart the server. Staff enter their own key in Partner desk or Operations. The customer token stays in session storage for that browser tab. Older sessions without issued credentials expire through retention.

Four reviewed document types, a current accepted quote and active consent are required to verify. The service compares certificate and signatory details with the registry and synthetic applicant record. The office-admin designation is informational. Missing documents or stale address proof require customer action. Conflicts go to the partner review queue after the customer allows partner sharing. An approve/reject decision records a rationale and revision. A second journey cannot take over a claimed GSTIN. Verified identity edits require withdrawal, and changing workforce/site/tier or creating a new quote invalidates verification. Customers without GSTIN can compare eligible products and request assisted onboarding. Digital checkout still requires all four documents.

A mock coverage survey, contract length and GST appear on each quote. Simulated payment failures can be retried on the same quote. Quote validity and scheduling show dates on the fixture calendar (15 July 2026 by default). Quotes additionally expire after seven elapsed days. The tracker uses the maximum product installation SLA and the selected install slot against the fixture calendar. Simulated WhatsApp/SMS notices stay in the tracker. Partner desk shows consented handoffs, rescue opportunities scored by the same fitted logistic model, and measured start-to-activation time from retained journeys. Browser dictation requires opt-in and may send audio to the browser provider. The Ollama path remains local.

### Try the review case

Start a journey, open **Demo applicant setup**, select K10, and load the profile. Generate and accept a quote. On Smart KYC, grant consent, import the mock DigiLocker specimen, confirm all four extractions, and run verification. Authorize partner review and open the staff desk. The reviewer can compare the ID signatory with the applicant, record a decision and rationale, and inspect the audit receipt. A successful approval permits payment and order creation for that verified quote.

For API calls, creation returns a token once. Send `Authorization: Bearer <token>` to customer routes, `X-Partner-Key` to staff routes and metrics, and `X-Ops-Key` to mock operations. Keys must be distinct. Shared staff keys identify roles rather than individual employees. The default single-worker limits are 30 journey starts per client IP per hour, 240 API requests per IP per minute, 12 retained uploads per journey, and two concurrent OCR operations. Starts and request rates are process-local prototype limits. Keep this demo on localhost.

## Local GenAI architecture

- `app/genai.py` connects the OpenAI Python client to Ollama's local OpenAI-compatible Chat Completions endpoint. Inference uses `qwen3:8b` by default, configurable with `OLLAMA_MODEL` and `OLLAMA_REASONING_EFFORT`.
- Qwen3 defaults to `OLLAMA_REASONING_EFFORT=none` to avoid unnecessary reasoning work. CPU response time varies by machine; the UI stops waiting after about 20 seconds and the deterministic questionnaire keeps the flow available without faking a model answer. Set `low`, `medium`, or `high` only when slower reasoning is acceptable.
- The bounded agent loop supports function calling with scoped JSON schemas and backend validation, with a maximum of four model rounds per turn. It returns tool evidence alongside the natural-language answer.
- The customer advisor has four tools: `search_catalogue`, `preview_quote`, `assess_journey_recovery`, and `propose_profile_update`.
- The advisor can search the product catalogue. Cohort analytics and lead rescue queues require a partner key. Individual lead rows stay outside the model prompt.
- Quote previews and eligibility checks call the same deterministic backend used by quote creation. A preview is not saved or accepted until the customer continues through the regular workflow.
- Profile facts extracted from chat remain unconfirmed and are shown in the profile form. GSTIN, Udyam IDs, legal identity, and verification status are not inferred from conversation.
- A guided business questionnaire always stays available; it works with no Ollama model and calls the deterministic catalogue recommender directly. The handoff view also shows an experimental logistic-regression activation score trained and evaluated on a fixed held-out split of synthetic challenge leads. It is a prioritisation signal only, not an eligibility or contact decision.
- Numeric facts and business priorities are captured only when grounded in the latest message. A must-keep priority requires explicit wording from the customer. A changed quote is rechecked and any recovery is a new version that requires fresh acceptance.
- `OLLAMA_BASE_URL` is restricted to localhost or the local Docker host so chat cannot be redirected to a hosted endpoint. Common email, Indian phone, GSTIN, and Udyam patterns are redacted before chat text is sent to the local model or stored in advisor history. Chat history is kept in the local SQLite database for this demo.

Keep the Ollama service on this device. Do not use real identity documents or customer details in this prototype.

## Included challenge data

The supplied data pack is included under `app/data/` as requested. Its accompanying `app/data/README.md` describes the catalogue, leads, registries, and KYC specimen documents as fictional synthetic data. The repository is private because the supplied competition materials distinguish a Judges Only pack that must not be shared and do not grant public redistribution rights for the student data. No Judges Only pack is included. The pack includes:

- 25 product catalogue records, including price ranges and eligibility rules
- 5,000 fictional lead records
- Mock GSTIN and Udyam registries
- Supplied KYC specimen images

The document pipeline checks image clarity, deskews scans, classifies document types, extracts GST legal names and identifiers, validates GSTIN checksums, and only repairs a known OCR confusion when exactly one corrected GSTIN matches the mock registry. A fuzzy GSTIN suggestion cannot verify an altered certificate, even if accepted; that case remains `REVIEW_REQUIRED`. GST legal names are compared with the selected mock registry record. Address proof uses the fixture date specified by the supplied data pack. OCR output is shown for review; it does not establish identity by itself.

Staff analytics use aggregate fields from this supplied synthetic dataset only. Review the competition terms before redistributing the data outside this project.

## Demo flow

1. Start a journey or load the clinic scenario, then describe the business in English, Hindi, Marathi, Tamil, Telugu, Kannada, Gujarati, or Hinglish—or select **Open business questionnaire** for a model-free route.
2. Ask the advisor for catalogue comparisons, an illustrative quote, or help recovering a changed journey. The interface shows which tools supplied evidence.
3. Review provisional profile facts and confirm them before recommendations.
4. Edit the bundle, create a versioned quote, and review the itemized prices and validation receipt.
5. Grant KYC consent and review the OCR extraction for each of the four required documents. Run verification and, if evidence conflicts, authorize partner review.
6. Complete the simulated checkout and follow persisted order-state events. Open the partner desk to see referral attribution, consent-based handoffs and the held-out propensity score.

## Project map

- `app/main.py`: application setup, customer discovery, catalogue eligibility, quotes and pricing.
- `app/access.py`, `app/storage.py`: journey and staff credentials, request limits, SQLite migrations, and evidence bindings.
- `app/onboarding.py`, `app/evidence.py`: consent, document intake, registry comparison, and audited reviewer decisions.
- `app/fulfilment.py`: checkout, scheduling, order events, simulated notifications, and the partner desk.
- `app/kyc_pipeline.py`: image quality checks, deskewing, local OCR, document classification, checksum validation, and review flags.
- `app/genai.py`: local Ollama tool definitions and bounded agent orchestration.
- `app/propensity.py`: fixed-seed logistic regression and held-out AUC on synthetic leads.
- `app/static/`: responsive HTML, CSS, and JavaScript; no frontend build step.
- `app/data/`: supplied synthetic challenge data and specimen documents.
- `tests/`: API, commercial policy, KYC/OCR fixtures, quote recovery, order, handoff, privacy, and agent-loop checks.
- `PRIVACY.md`: what the demo processes, where it stores records, and how to delete them.
- `.github/workflows/ci.yml`: Python compilation, JavaScript syntax validation, and automated tests.
- `Dockerfile`: local container setup with Tesseract.
- `docker-compose.yml`: app + local Ollama services and persistent data volumes.
- `submission/`: 7-slide business case, architecture diagram, captioned 54-second demo video and scene script.

## Checks

```powershell
pytest -q
python -m compileall -q app
node --check app/static/app.js
```

## Limits

- This is a local hackathon demonstration, not a production telecom platform.
- Registry, checkout, serviceability, and provisioning flows are simulated.
- Customer APIs require a journey bearer token. Partner and operations APIs require separate staff keys. Use only synthetic data on localhost.
- Uploaded document text and advisor chat history are stored in the local SQLite database, which is not encrypted at rest. Use fictional records only.
- Journey-linked profile, extracted OCR results, consent, chat, quote, handoff and order records are automatically deleted 7 days after creation. Use **Delete my data** to erase the active journey immediately. The supplied challenge dataset is unchanged. See [PRIVACY.md](PRIVACY.md) for the one-page note.
- Illustrative 12-month quotes use the catalogue midpoint. The 24-month term uses the catalogue minimum. The receipt shows simulated GST of 18% and expires after seven days.
