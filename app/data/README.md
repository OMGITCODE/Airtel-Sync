# Track A: Project Udyam Digital - Synthetic dataset

All businesses, people, identifiers, documents and figures in this pack are **fictional** and generated for the Accel YTL Hackathon. They do not describe any real company or customer.

## What's in the pack

| File | Rows | What it is |
| --- | --- | --- |
| `product_catalogue.csv` | 25 | Accel Business products with price range, setup fee, contract, eligibility rules and dependencies. Use it to power the advisor and the quote. |
| `smb_leads.csv` | 5,000 | One quarter (Apr-Jun 2026) of small-business leads with profile, funnel stage reached, drop reason, dates, and - for activated leads - bundle bought, value, churn and NPS. |
| `mock_gstin_registry.csv` | 3,534 | Mock GST registry. Back your "GSTIN lookup" API with this file. |
| `mock_udyam_registry.csv` | 2,723 | Mock Udyam registry. Back your "Udyam lookup" API with this file. |
| `kyc_documents/` | 47 images | Specimen KYC documents for 12 applicants (K01-K12): business registration, Udyam certificate, signatory ID and address proof. Some have problems your Smart KYC should catch. |

## Funnel at a glance

| Stage | Leads | % of previous stage |
| --- | --- | --- |
| Lead | 5,000 | - |
| Contacted | 3,406 | 68% |
| Needs assessed | 1,974 | 58% |
| Quote accepted | 861 | 44% |
| KYC complete | 588 | 68% |
| Activated | 368 | 63% |

Activated customers took 17.6 days on average from lead to activation and made 3 follow-up calls. Blended acquisition cost is about Rs 15,000 per activated customer.

## smb_leads.csv - column guide

| Column | Meaning |
| --- | --- |
| lead_id | Unique lead ID (L00001...) |
| created_date | Date lead was created |
| lead_source | Field sales, Website form, Channel partner, Inbound call, Referral, WhatsApp campaign |
| business_name, business_type, segment | Fictional business, its type and one of 4 segments |
| city, city_tier, state | Location; tier 1 or 2 |
| preferred_language | Language the owner prefers |
| employee_count, num_sites, annual_turnover_band | Size of the business |
| has_gstin, gstin, has_udyam, udyam_number | Registration status and numbers (match the mock registries) |
| current_internet, current_provider, monthly_telecom_spend_inr | Current setup and spend |
| products_of_interest | Product IDs the lead asked about (`;`-separated) |
| contact_name, contact_role | Fictional contact person |
| stage_reached | Last funnel stage reached |
| outcome, drop_reason | Activated or Dropped, with reason |
| contacted_date ... activated_date | Date each stage was reached (blank if not reached) |
| days_lead_to_activation | Days from lead to activation (activated only) |
| customer_follow_up_calls | Times the customer called to chase status |
| bundle_products | Products bought, as `P01x1;P07x2` (product x quantity) |
| monthly_bundle_value_inr, one_time_setup_inr, contract_months | Value of what was bought |
| churned_within_6_months, nps_score | Outcome after activation (0-10 NPS) |
| acquisition_cost_inr | Sales and partner cost spent on this lead |

## product_catalogue.csv - eligibility rules

- `available_city_tiers`: tiers where the product is sold
- `min_employees`, `min_sites`: product is only offered at or above these sizes
- `requires_gstin = Y`: cannot be sold without a valid, active GSTIN
- `requires_one_of`: product needs at least one of these products in the same bundle (`|`-separated)
- `typical_install_days`: use for the order tracker and for expected activation dates

## Notes on identifiers

- GSTINs follow the real 15-character structure with a valid checksum, so you can build a real format check. The 4th character of the embedded PAN is always `X`, which is not a valid entity type, so none of them can match a real taxpayer. Accept `X` in your validator.
- Udyam numbers use district code `00`, which does not exist.
- Use 2026-07-15 as "today" for any date-based check (e.g. address proof must be no older than 90 days).

## Privacy expectations

Treat this data as if it were real customer data: collect only what each step needs, show a clear consent notice before KYC, mask identifiers in logs and UI, and explain how long documents are retained. Your 1-page privacy note should cover these choices.
