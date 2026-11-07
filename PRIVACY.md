# OneJourney privacy note

**Local hackathon demonstration · synthetic challenge records only · updated 7 November 2026**

Do not upload real identity documents or customer information. OneJourney is not an Airtel production service and has no production authentication.

## What is processed

KYC upload requires purpose-specific consent. The image is processed in memory by local OpenCV/Tesseract OCR and is not saved. The app stores the extracted document fields, masked preview, confirmation state, entered registry identifiers, consent, profile, quote, chat history, handoff and simulated order events in its local SQLite database. The database is not encrypted at rest.

Only advisor chat goes to the configured local Ollama model; common email, phone, GSTIN and Udyam patterns are redacted first. KYC fields never go to the model. The deterministic questionnaire and catalogue recommender work without Ollama. WhatsApp preview is simulated and uses the same local advisor route. No government, payment, Airtel or other hosted service is contacted.

## Retention and deletion

Journey-linked records expire **7 days after journey creation**. The app removes expired journeys at startup and checks hourly while running. Use **Delete my data** in the app to immediately erase the active journey’s profile, extracted KYC results, consent, quotes, chat, handoffs and simulated order history. This action does not delete the supplied synthetic challenge dataset in `app/data/`.

## Limits

Registry, pricing, eligibility and activation are simulations. Keep the app bound to localhost and do not expose it to the public internet. The supplied data pack describes its sample businesses and identifiers as fictional; the GitHub repository remains private because the competition materials do not grant permission for public redistribution. Review applicable competition terms before sharing the supplied data elsewhere.
