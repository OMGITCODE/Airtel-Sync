# OneJourney · Airtel SYNC 2026

OneJourney is a runnable hackathon prototype for small-business discovery, catalogue-based recommendations, transparent quotes, consent-led document OCR, mock identity checks, simulated checkout, and order tracking.

The backend keeps commercial decisions deterministic. The advisor uses a clearly disclosed local rules-based fallback unless an optional hosted model is configured. Payment, serviceability, provisioning, activation, and government registry checks are simulations; the prototype does not connect to Airtel production systems.

## Run locally

Requirements: Python 3.11+ and the Tesseract OCR executable.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. The API health check is at `/api/health`, and interactive API documentation is at `/docs`.

The **Load clinic demo** button creates a fictional scenario. Its sample registration document is generated in memory by `/api/demo/document`; no generated image needs to be stored in the repository. The OCR step runs locally through Tesseract.

## Included challenge data

The supplied challenge pack is included under `app/data/` as requested. Its accompanying `app/data/README.md` describes the catalogue, leads, registries, and KYC specimen documents as fictional synthetic data. The pack includes:

- 25 catalogue records with pricing ranges and eligibility rules
- 5,000 fictional lead records
- Mock GSTIN and Udyam registries
- The supplied KYC specimen images

The datasets are used only by this prototype and its tests. They are not real Airtel performance data or production identity services. Check the competition's terms before reusing or redistributing the challenge pack elsewhere.

## Demo flow

1. Load the clinic scenario or create a new journey and describe the business needs in English, Hindi, Marathi, or Hinglish.
2. Confirm the extracted business profile and compare eligible catalogue options.
3. Review itemized recurring and setup prices, eligibility checks, assumptions, and quote versions.
4. Grant consent, upload a document, review local OCR output, and run the mock registry checks.
5. Complete the simulated checkout and follow order-state events.
6. Change a material profile fact before checkout to see JourneyProof invalidate affected decisions. Submitted orders require an assisted amendment flow.

## Architecture

- `app/main.py`: FastAPI endpoints, deterministic eligibility and pricing, SQLite persistence, OCR, mock verification, order lifecycle, and partner handoff.
- `app/static/`: responsive HTML, CSS, and JavaScript frontend; no frontend build step.
- `app/data/`: supplied synthetic challenge data and specimen documents.
- `tests/`: API, commercial policy, KYC, OCR, order, and handoff checks.
- `.github/workflows/ci.yml`: Python and JavaScript checks plus the test suite.
- `Dockerfile`: local container setup with Tesseract.

## Optional hosted advisor

Without credentials, the advisor uses the deterministic local fallback. To enable the optional structured extraction adapter, install the `openai` package and set `OPENAI_API_KEY` in the process environment. Never commit a real key. Model output is treated as provisional customer facts; pricing, eligibility, identity checks, and order state remain controlled by the backend.

## Run checks

```bash
pytest -q
```

CI also compiles the Python package and checks the frontend JavaScript syntax.

## Limits

- This is a local hackathon demonstration, not a production telecom platform.
- The registry, checkout, serviceability, and provisioning flows are simulated.
- No authentication or production security controls are included. Do not deploy the open demo endpoints publicly with real customer data.
- Uploaded document text is stored in the local SQLite database for the demo and is not encrypted at rest. Use fictional records only.
- Illustrative midpoint catalogue pricing is not an operator tariff or commercial offer.
