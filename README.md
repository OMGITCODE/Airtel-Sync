# OneJourney · Airtel SYNC 2026

OneJourney is a full-stack small-business onboarding prototype built around a multilingual local GenAI advisor powered by Ollama. The advisor can hold context across turns and call tightly scoped tools to search the supplied product catalogue, calculate rule-checked quote previews, analyze the supplied lead cohort, and recover a journey after an eligibility change. A deterministic FastAPI policy engine remains authoritative for prices, eligibility, saved quotes, consent, KYC simulation, and order state.

The experience also includes local OCR, consent-gated document handling, synthetic GSTIN/Udyam checks, customer-chosen quote recovery, simulated checkout and fulfilment events, and a responsive browser interface. This is a hackathon demonstration; it is not an Airtel production service and does not contact a live operator, registry, payment system, or network.

## Run locally

Requirements: Python 3.11+, the Tesseract OCR executable, and Ollama with the local `qwen3:8b` model.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
ollama pull qwen3:8b
Copy-Item .env.example .env
# Ollama defaults to http://127.0.0.1:11434; keep the Ollama app running.
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. The API health check is `/api/health`, and interactive API documentation is at `/docs`. The advisor status reports whether Ollama is running and the selected model is installed. All chat inference uses the local Ollama service on this device; no API key or hosted model is used. The app does not fake AI responses when Ollama is unavailable.

The **Load clinic demo** button creates a fictional scenario. Its sample registration document is generated in memory by `/api/demo/document`; no generated image is stored in the repository. OCR runs locally through Tesseract.

## Local GenAI architecture

- `app/genai.py` connects the OpenAI Python client to Ollama's local OpenAI-compatible Chat Completions endpoint. Inference uses `qwen3:8b` by default, configurable with `OLLAMA_MODEL` and `OLLAMA_REASONING_EFFORT`.
- Qwen3 defaults to `OLLAMA_REASONING_EFFORT=none` so the CPU-friendly local model responds within the app's request window. Set `low`, `medium`, or `high` when the machine can handle slower reasoning.
- The bounded agent loop supports function calling with scoped JSON schemas and backend validation, with a maximum of four model rounds per turn. It returns tool evidence alongside the natural-language answer.
- The advisor has five tools: `search_catalogue`, `preview_quote`, `analyze_funnel`, `assess_journey_recovery`, and `propose_profile_update`.
- Search and analytics operate on the supplied local challenge data. Funnel results are aggregates; individual lead rows are never sent to the model.
- Quote previews and eligibility checks call the same deterministic backend used by quote creation. A preview is not saved or accepted until the customer continues through the regular workflow.
- Profile facts extracted from chat remain unconfirmed and are shown in the profile form. GSTIN, Udyam IDs, legal identity, and verification status are not inferred from conversation.
- Numeric facts and business priorities are captured only when grounded in the latest message. A must-keep priority requires explicit wording from the customer. A changed quote is rechecked and any recovery is a new version that requires fresh acceptance.
- `OLLAMA_BASE_URL` is restricted to localhost or the local Docker host so chat cannot be redirected to a hosted endpoint. Common email, Indian phone, GSTIN, and Udyam patterns are redacted before chat text is sent to the local model or stored in advisor history. Chat history is kept in the local SQLite database for this demo.

Keep the Ollama service on this device. Do not use real identity documents or customer details in this prototype.

## Included challenge data

The supplied data pack is included under `app/data/` as requested. Its accompanying `app/data/README.md` describes the catalogue, leads, registries, and KYC specimen documents as fictional synthetic data. The pack includes:

- 25 product catalogue records, including price ranges and eligibility rules
- 5,000 fictional lead records
- Mock GSTIN and Udyam registries
- Supplied KYC specimen images

The document pipeline checks image clarity, deskews scans, classifies document types, extracts identifiers, validates GSTIN checksums, and only repairs a read error when exactly one corrected GSTIN matches the mock registry. A looser match is shown as a suggestion requiring the customer's confirmation. Address proof uses the fixture date specified by the supplied data pack. OCR output is shown for review; it does not establish identity by itself.

The lead analytics endpoint and GenAI tool use aggregate fields from this supplied synthetic dataset only. Review the competition terms before redistributing the data outside this project.

## Demo flow

1. Start a journey or load the clinic scenario, then describe the business in English, Hindi, Marathi, or Hinglish.
2. Ask the advisor for catalogue comparisons, an illustrative quote, cohort insights, or help recovering a changed journey. The interface shows which tools supplied evidence.
3. Review provisional profile facts and confirm them before recommendations.
4. Edit the bundle, create a versioned quote, and review the itemized prices and validation receipt.
5. Grant consent, upload a document, review actual local OCR output, and run the mock registry checks.
6. Complete the simulated checkout and follow persisted order-state events.

## Project map

- `app/main.py`: FastAPI routes, catalogue eligibility and pricing, SQLite persistence, KYC checks, order lifecycle, and partner handoff.
- `app/kyc_pipeline.py`: image quality checks, deskewing, local OCR, document classification, checksum validation, and review flags.
- `app/genai.py`: local Ollama tool definitions and bounded agent orchestration.
- `app/static/`: responsive HTML, CSS, and JavaScript; no frontend build step.
- `app/data/`: supplied synthetic challenge data and specimen documents.
- `tests/`: API, commercial policy, KYC/OCR fixtures, quote recovery, order, handoff, privacy, and agent-loop checks.
- `PRIVACY.md`: what the demo processes, where it stores records, and how to delete them.
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
- The SQLite database remains until deleted; the prototype does not expire records automatically. See [PRIVACY.md](PRIVACY.md) for the complete local data-handling note.
- Illustrative midpoint catalogue pricing is not an operator tariff or commercial offer.
