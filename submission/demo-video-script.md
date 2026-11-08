# OneJourney demo walkthrough

The 54-second captioned recording uses an isolated synthetic K10 journey. It runs the real OCR, quote, review and order endpoints. Reviewer confirmation, payment, operations and notifications are simulated.

| Scene | What the viewer sees |
| --- | --- |
| Discovery | A local advisor and guided discovery. The demo operator selects fictional applicant K10. |
| Quote | The customer selects 24 months, checks the mock service area, and accepts the quote with GST and expiry. |
| Consent and evidence | The customer grants KYC consent, authorizes a simulated DigiLocker import, and confirms each of the four OCR extractions. |
| Conflict | K10’s document signatory differs from the applicant on file. The result is REVIEW_REQUIRED. |
| Human review | The owner authorizes sharing. A partner compares the names and records a reasoned simulated approval for the evidence revision and quote. |
| Checkout and tracking | The first simulated payment fails. The customer retries, receives the scheduled and expected dates, and sees a status notice after an authorized operations update. |

No external vault, payment provider, telecom network or messaging service is contacted.
