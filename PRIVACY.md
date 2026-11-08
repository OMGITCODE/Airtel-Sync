# OneJourney privacy note

**Synthetic demonstration · 8 November 2026**

## DPDP Act, 2023

**Notice, purpose and choice**

This prototype is designed to align with the Digital Personal Data Protection Act, 2023. Sections 5 and 6 cover notice and consent. Use fictional data. KYC consent permits local OCR and mock registry checks. Partner sharing requires separate permission. Share only the information needed for each purpose (§6(1)).

**What is stored**

Images are processed locally. Larger uploads may use temporary files during the request. Original images are not retained by the app. SQLite retains extracted fields, file digests for duplicate checks, verification decisions, quotes, order events and review history. Identifiers are masked in customer responses. The local database is unencrypted. The local advisor receives redacted chat and business facts. Optional dictation uses browser speech recognition, which may send audio to its provider after opt-in. SMS, WhatsApp, DigiLocker import and signature steps are simulated.

**Withdrawal, retention and erasure**

Withdraw KYC consent on the verification screen to erase its documents, identifiers and approval. Withdraw partner sharing in Partner desk to remove shared handoffs and end case access. These controls implement the withdrawal and cessation design in §6(4)–(6). Journey access expires after seven days. Startup and hourly cleanup erase linked records. Delete my data erases the current journey immediately (§8(7), §12). The supplied fictional reference dataset has a separate lifecycle.

**Grievances and contact**

Contact the local demo operator through Privacy request in the workspace footer using your journey token. Keep the returned reference. Operators read requests in the protected operations inbox (§8(9)–(10), §13). This prototype contact serves local demonstrations. Before use with real customers, the operator must publish a monitored business contact and applicable complaint routes.

[Read the Act on India Code](https://www.indiacode.nic.in/bitstream/123456789/22037/2/a2023-22.pdf).
