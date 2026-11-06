# OneJourney privacy note

OneJourney is a local hackathon demonstration. Use only the fictional challenge records and specimen documents included with the project. Never upload a real person's identity document or real customer information.

## What the prototype processes

- The local Ollama model receives the conversation after common email, Indian phone, GSTIN, and Udyam patterns are redacted. Profile facts stay provisional until the owner confirms them.
- Document upload is available only after the owner accepts the stated verification purpose. OCR runs on the same computer through OpenCV and Tesseract. The app compares extracted or typed values with the included mock registries; it does not contact a government service.
- The included lead, catalogue, registry, and specimen-document files are described by the supplied data pack as fictional challenge data. Lead analysis returns aggregates rather than individual records.

## Storage and retention

Journey profiles, entered registry identifiers, consent records, OCR results, and order simulations are saved in the local SQLite database configured by `ONEJOURNEY_DB` (by default, `onejourney.sqlite3`). Records remain there until that database is deleted; the prototype has no automatic expiry. The database is not encrypted at rest. OCR text previews mask common direct identifiers in the interface, and identifier inputs are masked unless the reviewer chooses to reveal them. The local model does not receive KYC fields.

To remove the prototype's saved journeys and KYC records, stop the app and delete the configured SQLite database file. This does not delete the challenge dataset files in `app/data/`.

## Limits

This is not a production onboarding, identity-verification, payment, or telecom service. Registry checks and checkout are simulated. The project has no production authentication or access control. Keep the server on a trusted local device and do not expose it to the public internet. Review the competition terms before redistributing the supplied challenge data outside this repository.
