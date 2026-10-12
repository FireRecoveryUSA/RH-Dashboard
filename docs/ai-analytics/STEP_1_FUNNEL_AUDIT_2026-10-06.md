# AI Outcomes Step 1 and worker audit — 2026-10-06

## Scope

Read-only inspection of the Mongo databases selected by this workspace's
`.env`, plus the dashboard and worker code. This is not a verification of
another deployment's environment settings.

Database observations below cover the entire `ai_line_items` collection,
not the SQL intake cohort or a particular dashboard date/department filter.
Reads were sequential against a live system, not a transactional snapshot.
Mongo timestamps below are UTC; the investigation occurred on October 6
locally / October 7 UTC.

## Findings

### 1. The configured dashboard projection is behind the source

At the full comparison:

| Observation | Result |
| --- | ---: |
| Source `ai_line_items` records | 22,815 |
| Dashboard `ai_invoice_analytics` projections | 21,214 |
| Source records with a corresponding projection | 21,214 |
| Source records without a corresponding projection | 1,601 |
| Category differences among corresponding records | 0 |
| Level differences among corresponding records | 0 |

Matching used source `claim_id` and projection `_id`, normalized to strings
for comparison. Only `billing_category` and `billing_level` were compared
for value equality; this does not establish equality of other fields.

All observed projections have schema version 3. The latest
`worker_processed_at` is **2026-09-15 02:37:12.085 UTC**. The persisted
worker checkpoint is **2026-09-15 02:37:12.158 UTC**, and the worker leader
lease expired at **2026-09-15 03:15:45.697 UTC**. The persisted worker control
flag is still enabled. `docker compose ps --format json` returned no local
Compose containers.

The local environment enables `AI_ANALYTICS_USE_PROJECTION`. In that mode,
`outcome_service._load_normalized_cohort` reads the dashboard projections.
`projection_read_repository.get_projection_records_for_claim_ids` leaves
claims without projections absent from its result; normalization treats
them as missing AI records, even when they exist in the operational source.
There is no direct-source fallback on this funnel path.

This establishes stale/incomplete cached data in the configured database.
It does not establish why the worker stopped updating, or that a different
deployed dashboard uses this same destination. The 1,601-record gap must not
be presented as the number missing from a specific filtered funnel.

### 2. “Category identified (no level)” hides valid category information

`outcome_service.get_outcome_funnel` currently:

1. Groups a record under `billing_level` whenever that field is truthy.
2. Otherwise increments one generic “Category identified (no level)” count
   when `billing_category` is truthy.
3. Otherwise groups the record under “Not identified.”

Consequently, Fire Suppression, Rescue Operation, Hazardous Materials, and
other category results disappear into the same generic row. The code does
not check whether the department's category requires a level.

The source uses `billing_level` for fee-item names, including
`Standard Fire Response Fee`, `Vehicle Fire`, and `Fire Suppression Response`.
It is therefore not exclusively a numbered severity/response level.
An absent numbered level does not itself establish an incomplete or
incorrect classification.

### 3. Step 1 does not reconcile its output with the department's fee schedule

The relevant configuration is
`department_fees_resources.fees_resources_final`. Observed tile fields
include:

- `fee_category`: the schedule category.
- `item`: the fee item used in Fees & Resources.
- `fee_label_from_document`: the wording from the source schedule.
- `use_in_ai_process` and `fee_send_option`: AI participation controls.
- `resources`: resources associated with a fee item.

`get_ai_participation_map` reads the fee tiles but retains only department
participation/mode/count information through `classify_fees`. The eligibility
snapshot persists that summary, not the category/item catalog. The funnel
uses it for eligibility, not for matching Step 1 results to fee tiles.

For example, source records for department 576 contain category
`Motor Vehicle Accident` without a billing level, while its current fee
tiles use category `Motor vehicle incidents` with specific response-level
items. A generic historical category is not a verified fee-item selection.
It would be incorrect to assign those older records a current level merely
to make the display match.

The configuration collection also contains duplicate department IDs
(666 documents, with two documents beyond one per department). The current
participation reader overwrites its map entry as it iterates without an
explicit ordering/version selection. Any implementation that joins a full
fee catalog needs an explicit authoritative-document rule rather than
assuming department IDs are unique.

Historical output and current fee configuration can differ legitimately.
Text mismatch alone is not proof that an invoice was calculated incorrectly.
This audit did not compare historical fee schedule documents or invoice
amount calculations.

### 4. New intake evidence is not represented by the dashboard worker

The current source contains fields including:

- `intake_status`
- `intake_evaluated_at`
- `intake_evaluation_count`
- `level_label_matched`
- `level_identification_reasoning`

Observed intake states include `IDENTIFIED`, `CATEGORY_ONLY`,
`NO_LEVELS_CONFIGURED`, and `RECYCLED`. These states have different meanings;
they should not be inferred solely from the presence of a document.

The worker fetches the full `ai_line_items` document, but its projection
builder does not preserve those fields. The funnel's direct summary read
and normalized record also omit them. The source also contains the newer
processing status `STALE_RELEASED`, which is not a member of the funnel's
`AI_COMPLETED_STATUSES = {"COMPLETED"}`.

The current Step 1 count means “AI document exists,” not verified successful
fee identification. Conversely, treating `STALE_RELEASED` as completed would
require confirming its upstream semantics; its name is insufficient evidence
to change the completed-stage rule.

## Recommended correction

1. Restore the worker serving the intended dashboard database and run its
   existing backfill/reconciliation path. Confirm the source/projection gap
   and checkpoint freshness after completion.
2. Represent Step 1 as **category and fee item identification**. Display
   actual categories for category-only results, without implying that every
   category needs a numbered level.
3. Resolve each department's authoritative Fees & Resources document and
   compare category/item selections within that department. Preserve raw AI
   output alongside a verified match; retain unavailable, ambiguous, and
   unmatched results explicitly. Do not guess levels from generic historical
   categories or cross-department names.
4. Preserve the source intake status and matched-label evidence through
   both direct and projection read paths, then backfill existing projections.
   Verify upstream semantics before changing which states mean evaluated,
   completed, skipped, or not applicable.
5. Keep the breakdown exhaustive for the selected cohort and retain the
   existing funnel subset/count contract. Department-filtered results must
   use that department's catalog.

This audit changes no runtime settings, database records, fee schedules, or
funnel calculation code.

## Implementation note (2026-10)

The correction above is implemented as follows:

- `ai_analytics/fee_schedule.py` holds the shared step-1 logic:
  `STEP1_SOURCE_FIELDS` (billing_level, the level-identification
  confidence/reasoning/low-confidence fields, `intake_status`,
  `intake_evaluated_at`, `intake_evaluation_count`, `level_label_matched`),
  `compact_fee_catalog`, and `identify_fee_result`. Matching is
  case/whitespace-insensitive against the department's current catalog —
  no synonym, level, or historical inference.
- `get_ai_participation_map` now reads every document with a finalized
  `fees_resources_final` array (metadata-only duplicates are excluded by a
  `$type` filter and can never overwrite a finalized schedule). Identical
  finalized duplicates select the first; differing finalized schedules set
  `uses_ai=None`, `ai_mode='unknown'`, `fee_catalog=None` — no guessing.
  Each entry carries `fee_catalog`/`fee_catalog_version` for matching, and
  the values are persisted in the eligibility snapshot (snapshot
  `schema_version` 2; v1 snapshots are treated as expired immediately but
  remain the stale fallback).
- The funnel's third stage is now "Step 1: category & fee item results";
  source record existence is still the stage denominator. Breakdown rows
  are the distinct `(label, match_status, description)` results of
  `identify_fee_result`, so actual categories and fee items are shown
  (e.g. `Structure Fires / Standard Fire Response Fee`) with intake
  evidence appended.
  `STALE_RELEASED` remains unmapped — the completed stage is unchanged.
- Projection schema bumped to v4; the worker startup backfill rebuilds
  older projections, and the projection read path performs one batched
  source read for claims with missing or pre-v4 projections (retaining
  old projection data if that read fails).

## Runtime verification — 2026-10-07 07:17 UTC

The local Compose stack was rebuilt and started using `dev-start.ps1`.
The existing leader-controlled startup hook ran the schema-4 backfill.
Its persisted run completed at 07:05:59 UTC with 22,816 claims processed
and zero failed claims. Normal reconciliation ran alongside the backfill.

An independent read-only comparison at 07:17:45 UTC verified:

| Observation | Result |
| --- | ---: |
| Source records / distinct source claim IDs | 22,816 / 22,816 |
| Dashboard projections | 22,816 |
| Source records missing a projection | 0 |
| Extra projection IDs | 0 |
| Projections at schema version 4 | 22,816 |
| Differences in the checked source/projection fields | 0 |

The comparison checked `billing_category` and every field in
`STEP1_SOURCE_FIELDS`, including the intake timestamps and matched-label
evidence. It matched source `claim_id` to projection `_id`. As with the
initial audit, this covers the configured databases as a whole, not a
specific filtered SQL funnel.

The latest projection update was 07:09:45 UTC. The worker leader lease
was active, expiring at 07:18:38 UTC at the time of the check. The local
frontend and backend endpoints returned HTTP 200; Compose services were
left running.

Validation: the eight targeted backend test files passed (306 tests);
after final matching and department-scope refinements, the three affected
files passed again (144 tests). The Outcomes frontend tests passed
(12 tests), and TypeScript checking passed.
