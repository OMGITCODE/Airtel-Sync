# OneJourney · Airtel SYNC 2026

OneJourney is a full-stack small-business onboarding prototype built around a multilingual GenAI advisor. The advisor can hold context across turns and call tightly scoped tools to search the supplied product catalogue, calculate rule-checked quote previews, analyze the supplied lead cohort, and recover a journey after an eligibility change. A deterministic FastAPI policy engine remains authoritative for prices, eligibility, saved quotes, consent, KYC simulation, and order state.

The experience also includes local OCR, consent-gated document handling, synthetic GSTIN/Udyam checks, simulated checkout and fulfilment events, and a responsive browser interface. This is a hackathon demonstration; it is not an Airtel production service and does not contact a live operator, registry, payment system, or network.

## Run locally

Requirements: Python 3.11+, the Tesseract OCR executable, and an OpenAI API key to use live GenAI.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env and replace OPENAI_API_KEY with your server-side key.
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. The API health check is `/api/health`, and interactive API documentation is at `/docs`. The advisor status in the app reports whether a live key is configured. Without a key, the rest of the prototype runs and the advisor clearly reports that live GenAI is unavailable; it does not pretend to answer with an AI fallback.

The **Load clinic demo** button creates a fictional scenario. Its sample registration document is generated in memory by `/api/demo/document`; no generated image is stored in the repository. OCR runs locally through Tesseract.

## GenAI architecture

- `app/genai.py` uses the OpenAI Responses API with `gpt-6.1-sol` by default, configurable through `OPENAI_MODEL` and `OPENAI_REASONING_EFFORT`.
- The bounded agent loop supports function calling with strict schemas and a maximum of four model rounds per turn. It returns tool evidence alongside the natural-language answer.
- The advisor has five tools: `search_catalogue`, `preview_quote`, `analyze_funnel`, `assess_journey_recovery`, and `propose_profile_update`.
- Search and analytics operate on the supplied local challenge data. Funnel results are aggregates; individual lead rows are never sent to the model.
- Quote previews and eligibility checks call the same deterministic backend used by quote creation. A preview is not saved or accepted until the customer continues through the regular workflow.
- Profile facts extracted from chat remain unconfirmed and are shown in the profile form. GSTIN, Udyam IDs, legal identity, and verification status are not inferred from conversation.
- Common email, Indian phone, GSTIN, and Udyam patterns are redacted before chat text is sent to the model or stored in advisor history. The chat history is kept in the local SQLite database for this demo.

Keep `OPENAI_API_KEY` on the server. Do not commit `.env`, expose the key in browser code, or use real identity documents or customer details in this prototype.

## Included challenge data

The supplied data pack is included under `app/data/` as requested. Its accompanying `app/data/README.md` describes the catalogue, leads, registries, and KYC specimen documents as fictional synthetic data. The pack includes:

- 25 product catalogue records, including price ranges and eligibility rules
- 5,000 fictional lead records
- Mock GSTIN and Udyam registries
- Supplied KYC specimen images

The lead analytics endpoint and GenAI tool use aggregate fields from this supplied synthetic dataset only. Review the competition terms before redistributing the data outside this project.

## Demo flow

1. Start a journey or load the clinic scenario, then describe the business in English, Hindi, Marathi, or Hinglish.
2. Ask the advisor for catalogue comparisons, an illustrative quote, cohort insights, or help recovering a changed journey. The interface shows which tools supplied evidence.
3. Review provisional profile facts and confirm them before recommendations.
4. Edit the bundle, create a versioned quote, and review the itemized prices and validation receipt.
5. Grant consent, upload a document, review actual local OCR output, and run the mock registry checks.
6. Complete the simulated checkout and follow persisted order-state events.

## Project map

- `app/main.py`: FastAPI routes, catalogue eligibility and pricing, SQLite persistence, OCR, mock verification, order lifecycle, and partner handoff.
- `app/genai.py`: Responses API tool definitions and bounded agent orchestration.
- `app/static/`: responsive HTML, CSS, and JavaScript; no frontend build step.
- `app/data/`: supplied synthetic challenge data and specimen documents.
- `tests/`: API, commercial policy, KYC, OCR, order, handoff, privacy, and agent-loop checks.
- `.github/workflows/ci.yml`: Python compilation, JavaScript syntax validation, and automated tests.
- `Dockerfile`: local container setup with Tesseract.

## Checks

```powershell
pytest -q
python -m compileall -q app
node --check app/static/app.js
```

## Limits

- This is a local hackathon demonstration, not a production telecom platform.
- Registry, checkout, serviceability, and provisioning flows are simulated.
- No authentication or production security controls are included. Do not deploy the open demo endpoints publicly with real customer data.
- Uploaded document text and advisor chat history are stored in the local SQLite database, which is not encrypted at rest. Use fictional records only.
- Illustrative midpoint catalogue pricing is not an operator tariff or commercial offer.
