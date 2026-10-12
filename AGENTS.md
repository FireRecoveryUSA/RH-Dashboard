# RecoveryHub Dashboard — Project Notes

## CI: GitHub Actions Deploy Workflow (`.github/workflows/deploy.yml`)

The deploy workflow authenticates to Azure using **OIDC federated identity**
(not a long-lived `AZURE_CREDENTIALS` client-secret JSON). This requires:

1. **`permissions: id-token: write`** at the top of the workflow (already set)
   — lets the job request an OIDC token from GitHub.
2. **GitHub repo secrets** consumed by the `azure/login@v1` step:
   - `AZURE_CLIENT_ID` — App Registration (clientId)
   - `AZURE_TENANT_ID` — Entra tenant
   - `AZURE_SUBSCRIPTION_ID` — target subscription
   (`AZURE_CLIENT_ID` / `AZURE_TENANT_ID` are also reused for the frontend
   Vite build args and Terraform vars.)
3. **Federated credential on the App Registration** (Azure portal → App
   registrations → the app → Certificates & secrets → Federated credentials):
   - Issuer: `https://token.actions.githubusercontent.com`
   - Subject: `repo:<ORG>/<REPO>:ref:refs/heads/main`
     (use the actual repo owner/name; for branch-scoped deploys)
   - Audience: `api://AzureADTokenExchange` (default)
   This is what lets the GitHub OIDC token impersonate the app — without it,
   `azure/login` fails even when all three secrets are present.

The legacy `creds: ${{ secrets.AZURE_CREDENTIALS }}` JSON form was removed in
favor of this approach to eliminate client-secret rotation and the
"Not all values are present" parsing failures.

The test job pins **Node 22 LTS** (bumped from the deprecated Node 20, which
GitHub Actions runners no longer support as of the 2025-09-19 deprecation).

### Required GitHub Secrets (Auth Hardening)

The deploy workflow requires these additional secrets beyond the OIDC login
secrets above. **The deploy will fail if these are not created before the next
push to `main`.**

- **`AZURE_SPA_CLIENT_ID`** — The Entra ID SPA App Registration client ID used
  by the frontend for MSAL login (`d7d4d4d0-5460-4655-ab6d-a9aaac38b578` for
  the streamlineas.com tenant). This is the single source of truth: it feeds
  both the frontend Vite build arg (`VITE_AZURE_CLIENT_ID`) and the backend's
  JWT audience (`AZURE_CLIENT_ID` via `TF_VAR_azure_spa_client_id`). Although
  this is a public value (baked into browser-visible JS), storing it as a
  secret ensures the frontend build and backend config can't drift apart.
- `FRONTEND_URL` is no longer a required secret — Terraform now computes it from the Container App Environment default_domain and frontend app name.

## Deferred: Azure Billing Configuration

**Status:** On hold as of 2026-07-17. All Azure billing setup work is deferred
until the Azure subscription and billing account are straightened out.

The billing UI, backend sync service, API routes, tests, and Terraform are
all implemented and passing. What remains is **Azure-side configuration only**
— no code changes are needed to enable billing.

### Deferred items

1. **Billing service principal** — create a dedicated App Registration in
   Azure AD with a client secret and assign it the Cost Management Reader
   role on the target subscription or billing account. Fill in:
   - `AZURE_BILLING_CLIENT_ID`
   - `AZURE_BILLING_CLIENT_SECRET`

2. **Billing account identifiers** — determine the subscription ID, billing
   account ID, and account type from the Azure portal. Fill in:
   - `AZURE_SUBSCRIPTION_ID`
   - `AZURE_BILLING_ACCOUNT_ID`
   - `AZURE_BILLING_ACCOUNT_TYPE` (MOSP, MCA, or EA)

3. **Management group scope (optional)** — `AZURE_MANAGEMENT_GROUP_ID` is
   defined in config but not wired into the sync service. If multi-subscription
   scope is needed later, update `_scope()` in `backend/billing/sync_service.py`
   to return `/providers/Microsoft.Management/managementGroups/{id}` when the
   value is set.

4. **AI provider** — configure either Azure OpenAI (Foundry) or OpenAI.com:
   - Azure OpenAI: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, and set
     `OPENAI_CHAT_MODEL`/`OPENAI_EMBEDDING_MODEL` to deployment names
   - OpenAI.com: `OPENAI_API_KEY`

5. **Enable billing sync** — set `BILLING_SYNC_ENABLED=true` in `.env` once
   all credentials are in place.

### Goal when resumed

The objective is to analyze:
- What is actually costing us money
- What costs are going up
- Why they are going up

The billing dashboard views (Cost Overview, Top Spenders, Budgets & Alerts,
Advisor, Invoices, Reservations, AI Cost Analyst) are all built and ready to
display data once the sync service is enabled and has populated MongoDB.

## DRY Rule (Enforced)

**Don't Repeat Yourself.** This is a hard rule, not a suggestion.

When multiple components need the same logic (date-range initialization, auth
token handling, data-fetching patterns, filter state management), extract it
into a single shared hook, utility, or service — and consume that one
implementation everywhere. If a change requires editing the same logic in more
than one file, that's a DRY violation that must be fixed before the change is
considered complete.

Existing examples in this codebase:

- **`useAiDateRange`** (`frontend/src/hooks/useAiDateRange.ts`) — the single
  source of truth for AI dashboard date-range initialization (server-date
  fetch + `computeDateRange` + state). All three AI dashboards (Adoption,
  Outcomes, Diagnostics) consume it. Change the default range type in one
  place and all dashboards pick it up.
- **`useAutoRefresh`** (`frontend/src/hooks/useAutoRefresh.ts`) — the single
  auto-refresh polling hook consumed by all AI dashboards.
- **`normalization_core.py`** (`backend/ai_analytics/`) — the single source of
  truth for normalization logic shared between direct-read services and the
  AI Analytics Worker.
- **`createApiFetch`** (`frontend/src/services/fetchWrapper.ts`) — the single
  fetch wrapper with auth-header injection and 401 retry used by all API
  service clients.

When adding a new dashboard or component that needs date-range initialization,
use `useAiDateRange`. Do not copy the `useEffect` + `api.getServerDate()` +
`computeDateRange()` block into the component.

## Build & Run

Local Docker Desktop development. `dev-start.ps1` is a thin wrapper over
`docker compose` — it is the only script; there is no separate logs script:
```powershell
.\dev-start.ps1              # Build + run (default)
.\dev-start.ps1 -NoCache     # Full clean rebuild + restart
.\dev-start.ps1 -Restart     # Restart without rebuilding images
.\dev-start.ps1 -Stop        # Stop and remove containers (volume preserved)
.\dev-start.ps1 -Logs        # Follow all logs (docker compose logs -f)
.\dev-start.ps1 -Follow      # Build + run, then follow logs
.\dev-start.ps1 -Logs -Service backend   # One service
.\dev-start.ps1 -Logs -Tail all          # Full history
```

Stack runs at:
- Frontend: http://localhost:3000
- Backend:  http://localhost:8001/docs
- MongoDB:  localhost:27017

### The stack is Compose-managed

The script previously started containers with individual `docker run` calls,
which meant they carried none of the `com.docker.compose.*` labels — so
`docker compose logs -f` matched zero containers and exited silently, and
`docker-compose.yml` had drifted into a second, divergent definition of the
same stack (different ports, container names, and Mongo target). That is why
`docker compose logs -f` originally found nothing: it only follows
Compose-managed containers, and the old `docker run` containers had no
`com.docker.compose.*` labels. Compose is now the single source of truth. Keep
it that way: add services to `docker-compose.yml`, not to the script.

Two PowerShell-specific traps when editing `dev-start.ps1`:

- **Do not use `ValueFromRemainingArguments` to forward docker flags.** The
  binder silently *discards* single-dash tokens it cannot match to a parameter
  name, so `Invoke-Compose up -d` dropped the `-d` and started the stack
  attached, hanging the script. `Invoke-Compose` takes an explicit
  `[string[]]` array instead.
- **`Show-Logs` deliberately bypasses `Invoke-Compose`.** Ctrl+C makes
  `logs -f` exit non-zero, which is the normal way to stop following and must
  not be treated as a failure.

`docker compose up` is called with `--wait`, so the endpoints the script prints
are actually serving when it returns.

### MONGODB_URI decides which database you get

`docker-compose.yml` intentionally does **not** override `MONGODB_URI` or
`MONGODB_DB_NAME`; both come from `.env` via `env_file`. The old compose file
hardcoded `mongodb://mongo:27017`, which silently repointed the backend at the
empty local container and away from the Atlas cluster in `.env` — and since
`_seed_default_dashboards()` upserts on every startup, that came up as a fresh
empty database.

The `mongo` service is therefore only used when `.env` points at it. To use the
local container, set `MONGODB_URI=mongodb://mongo:27017`.

### Healthcheck intervals are tuned for log readability

Both healthchecks poll on a long `interval` (60s) with a short
`start_interval`, because every Mongo probe opens a connection that mongod
logs and every backend probe is access-logged by uvicorn. Short steady-state
intervals flood `-Logs`. The fast polling that actually gates `depends_on`
happens during `start_period` via `start_interval`, so startup is not slowed.

### Frontend Build-Time Environment Variables

The frontend image is built as a static Nginx bundle, so Vite environment
variables must be passed as Docker build args. Missing values will produce a
fatal Vite config error at build time.

Ensure these variables are present in `.env` and mapped in `docker-compose.yml`
under `services.frontend.build.args`:

- `VITE_AZURE_CLIENT_ID` → sourced from `VITE_AZURE_CLIENT_ID`
- `VITE_AZURE_TENANT_ID` → sourced from `VITE_AZURE_TENANT_ID`

Both use Compose's `${VAR:?err}` form, so a missing value fails the build with
a named error instead of an opaque Vite config crash. (The tenant arg was
previously fed from `AZURE_TENANT_ID` in compose but from `VITE_AZURE_TENANT_ID`
in the script — the two could disagree.)

`frontend/Dockerfile` declares `ARG VITE_AZURE_CLIENT_ID` and
`ARG VITE_AZURE_TENANT_ID` and exposes them as `ENV` for the Vite build.

**`VITE_DEV_AUTH_BYPASS` is deliberately not a build arg.**
`frontend/Dockerfile` documents a security decision not to declare it as an
`ARG` so it cannot be baked into an image. Both the old script and the old
compose file passed it anyway, where Docker silently ignored it — it never had
any effect. It has been removed from compose rather than wired up; do not
re-add it without the security review the Dockerfile calls for.

## Test Commands

```powershell
# Backend (from backend/)
$env:TESTING="true"; .venv\Scripts\python.exe -m pytest -v

# Frontend unit tests (from frontend/)
npm run test

# Frontend E2E tests (from frontend/)
npm run test:e2e

# Terraform validate (from terraform/)
terraform validate
```

## Claims Dashboard

The target SQL Server `Claims` table is system-versioned (temporal_type = 2)
with a history table at `AuditData.claimsHistory` containing **38.6M+ rows**.

**IMPORTANT:** Dashboard widgets must NOT use `FOR SYSTEM_TIME BETWEEN` —
scanning the 38.6M-row history table causes 504 gateway timeouts (180s) on
cold Azure SQL cache. All temporal widgets were rewritten to use plain
`Claims` queries against the well-indexed current table, plus
`dbo.claims_deleted` for deleted-draft counts. The `FOR SYSTEM_TIME` +
`ROW_NUMBER() OVER (PARTITION BY id)` pattern was replaced with:

- **Drafts Created YTD / Period Comparison**: 3-way `UNION` of current drafts
  (`submitted=0, original_run_id IS NULL`), current runs
  (`submitted=1, original_run_id IS NOT NULL`), and deleted drafts
  (`dbo.claims_deleted`), all filtered by `created BETWEEN`. The `UNION`
  deduplicates by `id`, matching the old `ROW_NUMBER` semantics.
- **Deleted Drafts YTD**: `COUNT(DISTINCT id)` on `dbo.claims_deleted` instead
  of `ROW_NUMBER() OVER (PARTITION BY id ORDER BY [timestamp])`.
- **New Runs / Active Runs / Submitted Period Comparison**: plain `Claims`
  queries with the same filters, no temporal scan.

The `dbo.claims_deleted` table (1M+ rows) is a heap with no useful index
(only `nci_wi_claims_deleted_...` on `(dept_id, run_number)`). Queries against
it do a full scan but complete in <1s on warm cache.

The system-managed `Claims Breakdown` dashboard is built by
`backend/main.py::_build_default_claims_dashboard()` and upserted on every
startup in `backend/main.py::_seed_default_dashboards()`. It defines these
claim buckets:

- **Drafts**: `submitted = 0 AND original_run_id IS NULL`
- **New Runs**: `submitted = 1 AND archived = 0 AND original_run_id IS NOT NULL
  AND ClaimCurrentTypeId = 1`
- **Active Runs**: `submitted = 1 AND archived = 0 AND original_run_id IS NOT NULL
  AND ClaimCurrentTypeId = 4`

`backend/target_db.py` normalizes legacy column names at runtime against live
`Claims` metadata (`_resolve_claims_column_map()`), and resolves the best date
column (`_resolve_claims_date_column()`) by preferring the temporal
`AS_ROW_START` column and falling back to `DateCreated` or similar.

## AI Analytics Worker — Phase 0

The AI Analytics Worker is an event-driven projection service that reads
RecoveryHub_AI MongoDB and writes dashboard-oriented analytics projections.
Phase 0 (source audit + contract freeze) is documented in
`docs/ai-analytics/PHASE_0_IMPLEMENTATION_PLAN.md` — this is the single
authoritative source for the verified data contract, projection schema, and
worker architecture.

### Shared Normalization Module (DRY)

`backend/ai_analytics/normalization_core.py` is the single source of truth for
all normalization logic (business outcome classification, writeback
normalization, retry detection, billability classification, confidence
bucketing, processing duration). Both the existing direct-read analytics
services and the future AI Analytics Worker import from this module.

`backend/ai_analytics/normalization.py` is a re-export shim that preserves
backward compatibility with existing imports. New code should import from
`normalization_core.py` directly.

### Verified Production Schema (2026-08-13)

- MongoDB Atlas: replica set `atlas-3oe9kl-shard-0` (3 members, v8.0.29)
- Change Streams: confirmed available and tested
- `ai_line_items`: indexed on `claim_id`, 17,732 docs
- `ai_agent_conversations`: indexed on `claim_id` and `line_item_record_id`,
  18,048 docs
- Conversation coverage: 100% (every ai_line_items has a linked conversation)
- Reconciliation by `updated_at`: viable (0.087s for 1-day query, no dedicated
  index needed at current scale)
- `is_billable`, `thread_id`, `retry_thread_id`: still 0% populated (use
  `billing_category` and `retry_count` instead)
- Recent records (last 30 days): `billing_category` dropped to 21% populated
  (was 99.8% historically) — `classify_billability()` handles this correctly

### Worker Observability Conventions (Phase 8)

**Two state stores must both be written.** Checkpoint and event timestamps
live in *both* the `ai_analytics_worker_state` Mongo document (survives
restart; read by reconciliation) and the in-memory `worker_health` singleton
(read by the `/ready` and `/status` HTTP endpoints). Writing only Mongo makes
the endpoints report `null` forever while the real value sits in the database.
Any new call site that advances a checkpoint must update both — see
`queue.run_queue_consumer` and `change_stream_listener._save_resume_token`.

**`worker_metrics` counters must be incremented at the call site.** Testing
`WorkerMetrics` in isolation does not prove the pipeline uses it. The
regression guard is `tests/test_ai_analytics_worker_instrumentation.py`, which
drives real pipeline paths and asserts counter deltas. Verify a new counter
test actually fails when the increment is removed.

Counter semantics worth preserving:
- `claims_refreshed == projections_created + projections_updated` (invariant)
- `claim_refresh_retries` counts retries *taken*, not failed attempts — the
  terminal attempt is a `claim_refresh_errors`, not a retry
- `events_received` counts total change-stream volume including skipped
  deletes, so a quiet stream is distinguishable from a delete-only one
- `reconciliation_runs` counts only scans that actually executed; a run
  skipped for a missing checkpoint is not counted, which makes a stuck-at-zero
  counter a meaningful signal
- `asyncio.CancelledError` must never increment error counters — otherwise
  every graceful deploy looks like an error burst

**Unauthenticated endpoints must not carry error text.** `/health` and
`/ready` are unauthenticated for container probes, and the backend Container
App sets `external_enabled = true`, so their payloads are world-readable.
`record_error` stores raw exception strings, which for driver failures embed
the Atlas cluster hostname, port, and timeout config. `last_error` is exposed
only on the auth-protected `/status`.

### Testing: avoid wall-clock dependence

`ClaimQueue` takes an injectable `clock` (defaults to `time.monotonic`)
specifically so debounce tests need no `time.sleep`. Sleep-based debounce
tests were flaky on Windows at roughly 2 failures in 6 runs: the default
system timer granularity (~15.6 ms) can exceed a short debounce margin.

When using the `FakeClock` helper in
`tests/test_ai_analytics_worker_queue.py`, start at `0.0` and advance by whole
seconds. Both keep IEEE-754 arithmetic exact. Adding a small delta to a large
origin does not: `1000.0 + 0.05` rounds to `1000.04999999999995`, so an
exact-boundary assertion fails for reasons unrelated to the queue. Since no
real time passes, large window values cost nothing.

### Dashboard Read Path — Projection vs Direct-Read (Phase 9)

The AI analytics endpoints (`/api/ai-analytics/*`) have two read paths for
AI-side data, gated by `AI_ANALYTICS_USE_PROJECTION` (default `false`):

- **Direct-read (default):** `_load_normalized_cohort` issues a per-request
  `$in` query against the operational RecoveryHub_AI Mongo cluster for
  `ai_line_items` documents. This is the pre-Phase-9 path.
- **Projection read (flag on):** reads from the worker's
  `ai_invoice_analytics` projection in the dashboard-owned Mongo instead.
  Eliminates the cross-cluster round-trip; the projection is a cache of the
  AI-Mongo side.

SQL-side data (business_outcome, cancellation_reason, process_logs,
claim_created_at) is **always** joined from SQL regardless of the flag — the
projection continues to cache only the AI-Mongo side. Phase 10 enriches the
AI-side trace and conversation data but does not move SQL truth into the
projection.

The adapter (`ai_analytics/projection_read_repository.py`) maps projection
field names to the raw `ai_line_items` field names that
`build_normalized_record` expects. The field mapping is explicit
(`_FIELD_MAP`, `_PASSTHROUGH_FIELDS`, `_MISSING_FROM_PROJECTION`) so a
schema change is caught there rather than silently producing `None`.

Fields not in the projection (`thread_id`, `retry_thread_id`) are 0%
populated in production per the Phase 0 audit, so their absence is
semantically identical to the direct-read path returning `None`.

**Phase 10 endpoint behavior:**
- `/invoices/{claim_id}/trace` — reads AI summary fields from the projection
  when enabled, including canonicalized line items with nested `resources`,
  `review_msg`, timestamps, and retry fields. Full conversation payloads are
  still loaded from RecoveryHub_AI when available; v2 `conversation_summaries`
  are used as a lightweight fallback if that read fails. A projection marked
  `has_ai_line_item_record=false` remains an AI-record-missing trace.
- `/diagnostics/agents` — aggregates v2 `conversation_summaries` from the
  projection by (agent, status, processing_stage, request_type). v1
  projections are excluded because they have no per-conversation summaries.
  The direct RecoveryHub_AI aggregation remains the fallback path when the
  flag is false.

**Rolling out the flag:** set `AI_ANALYTICS_USE_PROJECTION=true` only after
the worker has run a backfill and `ai_invoice_analytics` is populated. When
false, behaviour is identical to pre-Phase-9. The flag is read at import
time (the `settings` singleton in `config.py` evaluates `os.getenv` once
when `Settings()` is constructed). Toggling it requires a container
restart (a new Container App revision) — Azure Container Apps does not
hot-reload env vars into a running process.

### Invoice Trace & Agent Stats — Projection Integration (Phase 10)

Phase 10 enriches the projection schema (v1 → v2) and migrates two
endpoints to read from the projection when `AI_ANALYTICS_USE_PROJECTION`
is true:

**Schema v2 additions (projection_schema_version 1 → 2):**
- `ai_line_items` — now includes `resources` in each entry (was summary-only)
- `conversation_summaries` — new list of per-conversation summary dicts
  (conversation_id, agent, status, created_at as ISO string, processing_stage,
  request_type, execution_time_seconds). Excludes large payload fields
  (input_data, incident_json, results, output_data).
- `conversation_id` — from `ai_line_items.conversation_id` (linking ID)
- `thread_id_is_billable` — from `ai_line_items.thread_id_is_billable`

Per Section 9.12 Schema Evolution Policy: old v1 projections keep their
shape and are upgraded lazily on next refresh. The read adapter returns
`None`/empty for v1-missing fields — callers fall back gracefully.

**`/invoices/{claim_id}/trace` (Phase 10 migration):**
- When flag is on: reads AI summary from the projection (line items with
  resources, review_msg, timestamps, processing status, etc.) via
  `projection_read_repository.get_projection_for_trace`. Eliminates the
  `ai_line_items` cross-cluster read.
- Full conversation documents (input_data, incident_json, results,
  output_data) are still fetched from RecoveryHub_AI Mongo — the
  projection only stores conversation summaries, not the large payload
  fields. This is one remaining per-claim cross-cluster read, which is
  acceptable for an on-demand forensic tool.
- If no projection exists for the claim (v1 not yet refreshed, or claim
  never processed by worker), falls back to the direct-read path
  automatically.

**`/diagnostics/agents` (Phase 10 migration):**
- When flag is on: aggregates from the projection's
  `conversation_summaries` via `$unwind` + `$group` on the
  `ai_invoice_analytics` collection. Eliminates the batch cross-cluster
  read on `ai_agent_conversations`.
- v1 projections (no `conversation_summaries`) contribute nothing —
  `$unwind` on an empty/missing array produces no documents. Stats are
  incomplete until all projections are refreshed to v2, but never
  incorrect.

**Projection schema version bump:**
- `AI_ANALYTICS_WORKER_PROJECTION_SCHEMA_VERSION` default changed from
  1 to 2 in `config.py`. Existing deployments that pin the env var to 1
  continue producing v1 projections (no breaking change). New/restarted
  workers produce v2 projections.

### Sync Integrity & Worker Health Visibility (Phase 11)

Phase 11 reframed the original "staleness = cache age" concept into "sync
integrity = does the cache match MongoDB?" This mirrors FireSquirrel's
local-first sync pattern: verify the cache is in sync, surface the status,
and auto-heal on divergence.

**Sync integrity check** (`sync_integrity.py`) runs on its own cadence
(default 5 min, separate from reconciliation's 30 min). Two checks:
1. Count comparison: `ai_line_items.count()` vs `ai_invoice_analytics.count()`
2. Sample verification: N most recent source docs compared against
   projection `source_latest_updated_at`
Divergent claims are auto-enqueued into the ClaimQueue for refresh.

**Why sync integrity is separate from reconciliation:**
- Reconciliation (Phase 7) catches *missed change events* — looks for
  `updated_at > checkpoint`. Does NOT verify existing projections.
- Sync integrity (Phase 11) catches *divergence* — verifies existing
  projections match their source. Catches direct Mongo edits that bypass
  the change stream.
- Both are needed.

**Sync status states** (stable, frontend matches on these):
- `synced` / `syncing` / `catching-up` / `divergence-detected` / `error` /
  `stopped`

**New endpoints:**
- `GET /api/ai-analytics/worker/sync-health` — sync health summary
  (auth-protected, consumed by SyncHealthIndicator)
- `GET /api/ai-analytics/worker/dead-letters` — unresolved dead-lettered
  claims (auth-protected)
- `POST /api/ai-analytics/worker/dead-letters/{claim_id}/resolve` — mark
  a dead-lettered claim as resolved for retry

**Frontend SyncHealthIndicator** appears on all AI Analytics dashboard pages
(Outcomes, Diagnostics). Compact badge with expandable detail panel showing
sync status, source vs cache counts, divergent/missing counts, throughput
metrics, errors, and dead-letter list with resolve buttons. Auto-refreshes
every 30s.

**New config settings:**
- `WORKER_SYNC_INTEGRITY_INTERVAL_MINUTES` (default 5)
- `WORKER_SYNC_INTEGRITY_SAMPLE_SIZE` (default 50)

**New metrics counters:**
- `sync_integrity_checks` — incremented on each check cycle
- `sync_integrity_divergent_found` — incremented by divergent claim count

**v2 projection sizing** (Section 9.13.1): measured via
`backend/scripts/measure_v2_projection_size.py`. Median v2 projection is
~2.9 KB — within the v1 estimate. Annual growth ~68 MB/year, 10-year ~684 MB.
The 16 MB document limit is not a concern.

## AI Outcomes Funnel — Verified Data Flow (2026-09-12, corrected)

The funnel (`GET /outcomes/funnel`, `AiOutcomesDashboard.FunnelView`) has 6
stages; each stage is a subset of the previous one so counts never grow
top to bottom (`test_funnel_cancelled_outcome` asserts monotonicity):

1. **Reached Ready to Invoice Insurance** — rows in `AIInvoiceProcessRHTemp`
   joined to Claims. Breakdown: "Eligible for AI processing" /
   "Did not qualify — no qualifying AI tile" / "Eligibility unknown —
   configuration unavailable" (see *AI Tile Eligibility* below).
2. **Eligible for AI processing** — the qualified cohort (eligible +
   unknown). This replaced the former "Marked for AI billing"
   (`dept_send_auto_invoice_status = 2`) stage: the legacy flag no longer
   gates anything.
3. **Step 1: category & fee item results** — an `ai_line_items` doc exists
   (record presence, not verified successful evaluation). `breakdown` lists
   one row per distinct step-1 result — `(label, match_status,
   description)` — from `ai_analytics.fee_schedule.identify_fee_result`,
   which matches `billing_category`/`billing_level`/`level_label_matched`
   against the department's current fee catalog (case/whitespace only; a
   `billing_level` value may be a fee *item* name, not a numbered level).
   The catalog rides on the participation map
   (`get_ai_participation_map`); unavailable or ambiguous catalogs keep the
   raw source label. `match_status` is one of `matched`,
   `category_matched`, `ambiguous`, `unmatched`,
   `configuration_unavailable`, `configuration_ambiguous`,
   `not_identified`.
4. **AI processing completed** — `COMPLETED` within the evaluated cohort.
5. **Line items saved to RH** — `line_items_save_to_rh_status = true`;
   this is what lands the claim on the review grid. `breakdown` partitions
   the saved cohort by grid outcome — "Released" / "Cancelled / Rejected"
   (cancellation detail row, cancelled log, or `ai_line_items.is_cancelled`)
   / "In review grid (pending)" — so the three sum to the stage count, plus
   "Cancelled — <reason>" rows per normalized rejection category (via
   `reason_normalization.CATEGORY_LABELS`).
6. **Released** — process log "Invoice to Insurance - Released" or status 7.

Released and cancelled are sibling terminal outcomes, not successive
stages, so cancelled/pending are never emitted as stages of their own — a
stage after Released with a higher count would break the subset contract.

### Legacy "is AI" flags (verified 2026-09-12; superseded)

The dashboard's eligibility rule is the fee-tile configuration in
`department_fees_resources` — see *AI Tile Eligibility (authoritative
rule)* below. None of the flags in this list gate a funnel stage or
metric any more; they are documented so nobody reintroduces them:

- **`Departments.IsSendInvoiceAI`** (tinyint, values 0/2) — the legacy
  invoicing flag, snapshotted onto `ai_line_items` as
  `dept_send_auto_invoice_status`. Historically `2` correlated with line
  items being produced, and it is still passed through for display, but
  eligibility is derived from tile config, not from it.
  **It is NOT a routing mode** — there is no straight-through/auto-send
  path; every writeback goes through human grid review (send=2 claims
  show Released logs from reviewers).
- **`Departments.ai_fee_calc_status_id`** (1/3/NULL) — the MVI fee-calc
  feature flag, snapshotted as `dept_ai_fee_mvi_status` ("Active"/
  "Not Active": 1→Active, 3/NULL→Not Active). A different capability —
  does not gate invoicing either; send=2 claims write back regardless of it.
- **`dept_ai_identify_billable_status`** — null on all 21,047 docs,
  all-time. Dead field; never written.
- **`AIInvoiceProcessRHTemp.AI_inv_process_status`** — queue lifecycle
  state (0 queued, 2 processed, 4 terminal, 7 post-release, 9 active
  review), not an AI yes/no flag.
- **`Claims.is_truck_claim_AI`** — unrelated truck-claim feature.

### New ai_line_items fields (schema v3 → v4)

`billing_level`, `level_identification_confidence`,
`level_identification_low_confidence`, `level_identification_reasoning`
appeared ~2026-09 (the step-1 identification output; ~200 docs at review
time, only for departments whose fee schedules define levels).
`is_cancelled`, `cancellation_reason`, `cancelled_on`,
`is_invoice_sent_manually`, `sent_invoice_manually_on`,
`lineitems_edited*` also exist on newer source docs. Schema v3 preserves
`is_cancelled` and `cancellation_reason`; the other source fields are not
all included in the dashboard projection. Schema v4 adds the intake
evidence fields
`intake_status`, `intake_evaluated_at`, `intake_evaluation_count`, and
`level_label_matched`. Only the step-1 identification + intake fields
(`billing_level`, `level_identification_confidence` /
`level_identification_low_confidence` / `level_identification_reasoning`,
`intake_status`, `intake_evaluated_at`, `intake_evaluation_count`,
`level_label_matched`) are wired through `SUMMARY_PROJECTION`,
`build_normalized_record`, the worker projection, and
`_PASSTHROUGH_FIELDS`/`_TRACE_PASSTHROUGH_FIELDS` via the single
`STEP1_SOURCE_FIELDS` tuple in `ai_analytics/fee_schedule.py`.

**Pre-v4 projections lack the intake fields** — until the automatic
startup backfill (`main._run_worker_backfill_if_needed`, triggered by the
schema-version bump) rewrites them, the projection read path refreshes
missing/pre-v4 claims with one batched source read per request and marks
`source_status.recoveryhub_ai_projection` `"refreshing"` (or `"partial"`
if that read fails, keeping the old projection data).

### Bugs fixed (2026-09-12)

- **STRING_SPLIT ntext failure**: `get_process_logs_for_claims` and
  `get_cancellation_details_for_claims` pass claim IDs as one CSV param.
  Over ~4000 chars, pyodbc sends it as `ntext`, which `STRING_SPLIT`
  rejects — both queries failed on every realistic cohort, so Released/
  Cancelled were always 0 and `recoveryhub_sql` reported "partial".
  Fixed with `STRING_SPLIT(CAST(%(claim_ids_csv)s AS nvarchar(max)), ',')`.
  Never pass a large CSV param uncast into `STRING_SPLIT`.
- **`to_list(length=1000)` cap**: `get_ai_line_items_for_claim_ids`
  silently truncated AI-side reads at 1,000 docs. Now `length=None`.

### Gotchas

- ~13% of `AIInvoiceProcessRHTemp` claims have no `ai_line_items` doc —
  step 1 never ran for them (department not AI-enabled at the time, still
  queued, or skipped). "Step 1 evaluated" < intake is real, not a bug.
- `is_invoice_sent_manually` is true on ~19k docs — most invoices are sent
  outside the grid release path, so Released reflects grid releases only.
- `AI_inv_process_status = 0` exists (~113 rows) and classifies "unknown".
- Rejection categories distinguish `unknown` (a reason WAS recorded but the
  reason_id/text didn't map) from `no_reason_recorded` (claim was cancelled
  but no reason exists on the SQL detail row or the AI doc). As of
  2026-09-12, ~188 cancelled claims have a "Cancelled" process log but no
  `AIClaimInvoiceCancellationDetails` row — reviewers cancelled without
  picking a reason. All 679 detail rows map to reason_ids 1-17.

## Multi-Process Deployment & Leader Election (2026-09-12)

The backend runs `uvicorn main:app --workers 4` (see `backend/Dockerfile`) —
four separate OS processes, each running the FastAPI lifespan. Without
coordination every process booted its own copy of the worker (4x duplicate
change streams, reconciliation, and sync-integrity loops) and the worker
control endpoints were per-process — `GET /status` could report "not
running" while a backfill churned in a sibling process.

**Leader election** (`ai_analytics_worker/leader_election.py`) fixes this
with a lease doc `{"_id": "worker_leader"}` in `ai_analytics_worker_state`.
`runtime._run_leader_campaign` acquires via atomic `find_one_and_update`,
runs `run_worker` only while the lease is held, renews every
`lease/3` seconds, and releases on shutdown. Non-leaders campaign every
`WORKER_LEADER_CAMPAIGN_SECONDS`; failover after a leader dies takes at most
lease + campaign seconds. `GET /worker/status` reports `is_leader` and
`holder_id` so a non-leader read is diagnosable.

Two more docs in the same collection use the generic lease helpers
(`try_acquire_lease` / `renew_lease` / `release_lease`):

- `{"_id": "backfill_leader"}` — held by whichever process is running a
  backfill (manual `/worker/backfill` or the leader's startup hook), so
  two Uvicorn processes can never run full scans concurrently.
  `start_backfill()` returns `already_running` if the lease is held
  elsewhere; `is_backfill_running_anywhere(db)` is the cross-process
  check. A leader's hook/backfill is also cancelled when its term ends.
- `{"_id": "worker_control", "enabled": bool}` — deployment-wide switch.
  `POST /worker/stop` sets it false, stops the local campaign, and waits
  for the lease to be released; peers' campaigns never acquire (or renew)
  while it is false. `start_worker()` (lifespan or `/worker/start`) sets
  it true again. Lifespan shutdown does not touch it.

Config: `WORKER_LEADER_LEASE_SECONDS` (60), `WORKER_LEADER_CAMPAIGN_SECONDS`
(15), `WORKER_BACKFILL_CONCURRENCY` (8; `.env` runs 25) — backfill refreshes
claims concurrently under a semaphore; it is I/O-bound on Atlas round-trips,
not CPU.

**Stale change-stream resume tokens**: when a saved token points at an
oplog position that has rolled off, MongoDB returns
`ChangeStreamHistoryLost` (code 286) and the token can never work again.
`run_change_stream_listener` detects this, discards the persisted token,
and reopens a fresh stream; reconciliation covers the missed window.

## AI Tile Eligibility (authoritative rule)

A claim qualifies for AI invoicing iff its department's
`department_fees_resources` doc has at least one `fees_resources_final` tile
with `use_in_ai_process` truthy and `fee_send_option` in
`{"auto", "queued", "limited_auto"}` (`AI_SEND_OPTIONS`). The legacy
`Departments.IsSendInvoiceAI` flag / `dept_send_auto_invoice_status`
snapshot no longer gates any funnel stage or metric.

- **Source of truth**: `ai_analytics/normalization_core.py` —
  `classify_fees` (tile-level detail) and `classify_ai_eligibility`
  (per-claim: eligible / not_configured / unknown).
  `ai_adoption_service._classify_fees` is an alias, not a reimplementation.
- **Unknown fallback**: if the fee-config Mongo can't be read, every claim
  is `unknown` (never `not_configured`), `data_complete=false`, and
  `source_status["recoveryhub_ai_fee_config"]="unavailable"`. Unknown
  claims stay in the qualified cohort so the dashboards degrade rather
  than collapse.
- **Snapshot (projection mode)**: `ai_analytics/eligibility_snapshot.py`
  persists the last full department map in the dashboard-owned
  `ai_department_eligibility` collection (TTL
  `AI_ELIGIBILITY_SNAPSHOT_TTL_SECONDS`). With
  `AI_ANALYTICS_USE_PROJECTION=true` a fresh snapshot is served without
  touching RecoveryHub_AI Mongo; an expired one is refreshed from the
  source, and if that fails the stale map is served with
  `recoveryhub_ai_fee_config="stale"`. Direct mode always reads the
  source and rewrites the snapshot. Snapshot docs carry
  `schema_version` (currently 2); v1 snapshots predate the fee catalog
  entries (`fee_catalog`/`fee_catalog_version`, used for step-1 result
  matching) so they are treated as expired immediately while remaining
  the stale fallback.
- **Historical rescue**: a normalized writeback `success` or a
  `released`/`cancelled_rejected` outcome proves the claim was eligible
  when it ran and overrides the current (possibly disabled) tile config.
- **Metric scoping**: `qualified_records` / `qualified_ai_runs` in
  `outcome_service.py` are the shared population helpers. Outcome
  metrics use qualified records (eligible + unknown); diagnostics use
  qualified AI runs (`ai_record_state == "present"`);
  `did_not_qualify` counts only the definitive `not_configured` intake.
