# ENG-2 Phase 2 Architecture Design
## Runtime Automation & Pricing Models (Weeks 6–9)

**Date:** 2026-05-19
**Author:** Narayana Chavva
**Linear:** [ENG-2](https://linear.app/nexoraa-ai/issue/ENG-2/epic-phase-2-runtime-automation-pricing-models-weeks-69)
**Status:** Approved — ready for implementation planning
**Prerequisite:** Phase 1 gate (ENG-23 + ENG-24 must pass before Phase 2 starts)

---

## Purpose

This document is the shared architectural contract for all Phase 2 owners. Each engineer implements their assigned ticket against this spec — changes to interfaces in `_types.py`, shared table schemas, or API contracts require updating this doc and notifying all owners.

**Owners:**

| Engineer | Tickets |
|---|---|
| Narayana | ENG-27 (hybrid + value_based pricing models) |
| Nithilesh | ENG-28 (anomaly detection + velocity caps) |
| Dinesh | ENG-29 (enforcement mode ramp + margin dashboard) |
| Saahithi | ENG-30 (free-retry + failure charging + late events), ENG-31 (vendor reconciliation) |

---

## Stable Contracts from Phase 1

All Phase 2 code builds on these locked ADRs (merged in PR #1):

| ADR | Invariant |
|---|---|
| ADR-0001 | All PKs are UUID v7; idempotency keys are SHA-256-derived `TEXT` |
| ADR-0002 | `NUMERIC(18,6)` for all monetary fields; `BIGINT` for all credit fields; never mixed |
| ADR-0003 | `credit_ledger` is append-only; corrections via `adjustment` entries only |
| ADR-0004 | `rate()` is a pure function: zero I/O, zero clock reads, zero randomness |

**Locked open decisions affecting Phase 2:**

- **Decision #1:** Credits are internal only; customers see outcome counts and dollar amounts
- **Decision #6:** Charge steps completed on failure; free retry within 60 seconds; `partial_multiplier` defaults to `0.5`
- **Decision #14:** `margin_warning = true` when `raw_cost_usd / rated_credits > 0.0012`

---

## Section 1: Module Layout

The Credits Platform lives entirely in the `dify` repo. All Phase 2 code adds to this structure:

```
api/
  services/credits/
    __init__.py
    _types.py          ← RatingDecision, AnomalyEvent, VelocityCounter (cross-engineer contract)
    rating.py          ← rate() pure function — Narayana (ENG-27)
    anomaly.py         ← velocity cap check + spike detection — Nithilesh (ENG-28)
    enforcement.py     ← mode transition + audit — Dinesh (ENG-29)
    margin_query.py    ← read-only margin aggregation — Dinesh (ENG-29)
    retry.py           ← free-retry policy + partial charging — Saahithi (ENG-30)
    reconciliation.py  ← vendor drift calculation — Saahithi (ENG-31)
  models/credits.py    ← all SQLAlchemy models (Phase 1 + Phase 2 tables)
  tasks/credits/
    __init__.py
    anomaly_scan.py    ← Celery beat every 5 min (ENG-28)
    reconciliation.py  ← Celery beat 5th of month 06:00 UTC (ENG-31)
    margin_digest.py   ← Celery beat daily 08:00 UTC (ENG-29)

web/app/(commonLayout)/admin/credits/
  page.tsx             ← tenant overview: enforcement mode + margin warning counts
  margin/page.tsx      ← per-workflow per-week margin chart
  components/
    MarginChart.tsx    ← Recharts line chart, one series per workflow
    EnforcementModePanel.tsx  ← current mode badge + transition button
```

**Rule:** No service module imports from another service module. All shared types come from `_types.py`. This is the boundary that allows parallel development without circular imports.

---

## Section 2: Shared Types (`_types.py`)

```python
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

@dataclass(frozen=True)
class RatingDecision:
    workflow_run_id: str
    rating_rule_version: str
    cost_model_version: str
    rated_credits: int           # BIGINT — never float
    raw_cost_usd: Decimal        # NUMERIC(18,6)
    margin_warning: bool         # raw_cost_usd / rated_credits > 0.0012
    breakdown: dict              # per-component credit breakdown (JSONB)
    catalog_snapshot: dict       # frozen at reservation time

@dataclass(frozen=True)
class AnomalyEvent:
    tenant_id: str
    detected_at: datetime
    anomaly_type: Literal["velocity_cap_breach", "consumption_spike"]
    current_value: float
    threshold: float
    action_taken: Literal["alerted", "suspended", "none"]

@dataclass(frozen=True)
class VelocityCounter:
    tenant_id: str
    window_type: Literal["1min", "1hour"]
    count: int
    window_start: datetime

@dataclass(frozen=True)
class ResolvedWorkflowStatus:
    status: Literal["failed_free_retry", "failed_charged", "partial_success", "full_success"]
    retry_run_id: str | None     # set when status = failed_free_retry
    reversal_reason: str | None  # set when vendor outage triggered full reversal
```

---

## Section 3: ENG-27 — Hybrid + Value-Based Pricing Models

**Owner: Narayana**

### `rate()` Dispatch

```python
def rate(
    events: list[UsageEvent],           # settlement service pre-filters to billable=true
                                        # and to occurred_at < failed_at when status=failed_charged
    rating_rule: RatingRule,
    cost_model: CostModel,
    overrides: list[RatingOverride],
    catalog_snapshot: CatalogSnapshot,
    workflow_status: str = "full_success",  # set by retry.resolve_workflow_status() before call
) -> RatingDecision:
    model_type = rating_rule.model_type
    if model_type == "fixed":
        return _rate_fixed(...)
    elif model_type == "per_unit":
        return _rate_per_unit(...)
    elif model_type == "hybrid":
        return _rate_hybrid(events, rating_rule, cost_model, overrides, catalog_snapshot)
    elif model_type == "value_based":
        return _rate_value_based(events, rating_rule, cost_model, overrides, catalog_snapshot, workflow_status)
    raise ValueError(f"Unknown model_type: {model_type}")
```

**Settlement service caller contract (pre-call responsibilities):**
1. Call `retry.resolve_workflow_status()` to get the effective `workflow_status`
2. If `workflow_status = "failed_charged"`: filter `events` to `occurred_at < workflow_run.failed_at` before passing to `rate()`
3. Pass the resolved `workflow_status` string as the final argument
4. `rate()` itself never reads `workflow_run.failed_at` or any runtime state — ADR-0004 invariants preserved

### `hybrid` Model

```python
def _rate_hybrid(...) -> RatingDecision:
    base = rating_rule.base_credits
    agent_credits = sum(a.credit_rate for a in catalog_snapshot.agents)
    token_credits = (total_tokens_in + total_tokens_out) * rating_rule.token_credit_rate
    tool_credits = sum(t.credit_rate for t in catalog_snapshot.tools)
    rated_credits = base + agent_credits + token_credits + tool_credits
    # apply overrides, compute margin_warning, build breakdown
```

Agent and tool credit rates come from `catalog_snapshot` (frozen at reservation — never a live lookup). The `breakdown` field records each component separately for dispute resolution.

### `value_based` Model

```python
MULTIPLIERS = {
    "full_success":       1.0,
    "partial_success":    rating_rule.partial_multiplier,  # default 0.5
    "failed_free_retry":  0.0,
    "failed_charged":     None,  # falls through to steps-completed calculation
}

def _rate_value_based(events, rating_rule, cost_model, overrides, catalog_snapshot, workflow_status) -> RatingDecision:
    # workflow_status is passed explicitly by the settlement service (set by retry.resolve_workflow_status())
    # events list is already pre-filtered to occurred_at < failed_at when status=failed_charged
    multiplier = MULTIPLIERS.get(workflow_status)
    if multiplier is not None:
        rated_credits = int(rating_rule.base_credits * multiplier)
    else:
        # failed_charged: events already filtered by settlement service caller — rate normally
        rated_credits = _rate_per_unit(events, ...).rated_credits
```

### Golden Fixtures

All fixtures in `tests/rating/fixtures/credits/`. CI asserts byte-for-byte equality.

| File | Scenario |
|---|---|
| `hybrid_3agents_2tools.json` | 3 agents + 2 tools, no overrides |
| `hybrid_discount_override.json` | 10% discount applied |
| `hybrid_margin_warning.json` | `cost_per_credit > $0.0012` |
| `value_based_full_success.json` | multiplier = 1.0 |
| `value_based_partial_success.json` | multiplier = 0.5 |
| `value_based_failed_free_retry.json` | within 60s → 0 credits |
| `value_based_failed_charged.json` | outside 60s → steps-completed charge |

All existing Phase 1 fixtures must still pass (no regression).

---

## Section 4: ENG-28 — Anomaly Detection + Velocity Caps

**Owner: Nithilesh**

### Velocity Caps — DB-Backed with 30s In-Process Cache

New table in `models/credits.py`:
```python
class VelocityCounterModel(Base):
    __tablename__ = "velocity_counters"
    tenant_id    = Column(UUID, nullable=False)
    window_type  = Column(String, nullable=False)   # '1min' | '1hour'
    window_start = Column(TIMESTAMP(timezone=True), nullable=False)
    count        = Column(BigInteger, default=0)
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "window_type", "window_start"),)
```

Gateway middleware calls `anomaly.check_velocity()` on every `POST /v1/credits/reserve`:

```python
def check_velocity(tenant_id: str) -> VelocityCheckResult:
    # 1. Read in-process LRU cache (30s TTL)
    # 2. Cache miss → SELECT from velocity_counters for current windows
    # 3. Compare against caps (defaults: 60/min, 1000/hour; overridable via entitlement_overrides)
    # 4. Over cap → return 429 with retry_after computed from window_start + window_duration
    # 5. Under cap → fire-and-forget Celery task: UPSERT counter increment
```

**Known soft bound:** The 30s TTL means a burst can exceed the per-minute cap for up to 30s before the cache refreshes. This is intentional and acceptable for enterprise workflow cadences.

**Cleanup:** A Celery beat task runs hourly and deletes `velocity_counters` rows older than 2 hours. This is safe because windows older than 1 hour can never be the current window. Add to `tasks/credits/anomaly_scan.py` alongside the spike detection task.

### Spike Detection — Celery Beat Every 5 Minutes

`tasks/credits/anomaly_scan.py` processes all active tenants:

```sql
SELECT
    SUM(credits) FILTER (WHERE created_at > NOW() - INTERVAL '1 hour') AS hourly_total,
    AVG(daily_total) AS seven_day_avg
FROM (
    SELECT DATE_TRUNC('day', created_at) AS day, SUM(credits) AS daily_total
    FROM credit_ledger
    WHERE tenant_id = :tid AND created_at > NOW() - INTERVAL '7 days'
    GROUP BY 1
) sub
```

If `hourly_total > seven_day_avg * 10`:
1. Emit CloudWatch metric `credits/anomaly_spike`
2. Trigger SNS alert to on-call
3. If `tenants.auto_suspend = true` (boolean column on `tenants` table, default `false`) AND `tenants.enforcement_mode != 'observe_only'`:
   - Set `enforcement_mode = enforce_block`
   - Write `audit_log` entry: `reason="anomaly_auto_suspend"`, actor=`"system"`
   - Emit `tenant.suspended` internal event

`observe_only` tenants are **never** auto-suspended regardless of `auto_suspend` flag.

Every detection (alert or suspend) writes an `AnomalyEvent` to `audit_log`.

---

## Section 5: ENG-29 — Enforcement Mode Ramp + Margin Dashboard

**Owner: Dinesh**

### Enforcement Mode Ramp

Valid transitions only — no skipping, no implicit reverse:
```
observe_only → warn_only → enforce_block
```

`enforcement.py` exposes one function:
```python
def transition_enforcement_mode(
    tenant_id: str,
    to_mode: EnforcementMode,
    actor_id: str,
    reason: str,
) -> None:
    # Validate transition is legal
    # UPDATE tenants SET enforcement_mode = to_mode
    # INSERT audit_log with before_state, after_state, actor_id, reason, transitioned_at
```

Called by existing `PATCH /console/api/admin/tenants/{id}/enforcement-mode`. No new endpoint. Permission check (`tenant.cap.update`) already enforced in ENG-22.

Ramp checkpoints (what to verify before each transition) are documented in `docs/credits/RAMP_RUNBOOK.md` — not enforced in code.

### Daily Margin Email Digest

`tasks/credits/margin_digest.py` — Celery beat at 08:00 UTC daily:
- Aggregates `rating_decisions` for the trailing 24 hours per tenant
- Sends via existing dify `MailClient` to `tenant.billing_contact_email`
- Content: credits consumed vs included, projected period-end, top 3 workflows by cost, margin warning count

### Margin Dashboard

**New API endpoint:**
```
GET /console/api/credits/margin?from=YYYY-MM-DD&to=YYYY-MM-DD
```
Returns: `{ workflows: [{ workflow_id, name, weekly: [{ week, raw_cost_usd, rated_credits, cost_per_credit, margin_warning_count }] }] }`

Backed by `margin_query.py` helper in `services/credits/` — read-only aggregation, no side effects.

**Frontend (`web/app/(commonLayout)/admin/credits/`):**

- `page.tsx` — tenant table: enforcement mode badge, margin warning count, projected period spend
- `margin/page.tsx` — `MarginChart` (Recharts line chart, one series per workflow, threshold line at `$0.0012`)
- `EnforcementModePanel` — current mode badge + "Promote" button → confirmation modal requiring explicit reason string before calling PATCH

---

## Section 6: ENG-30 — Free-Retry + Failure Charging + Late Event Handling

**Owner: Saahithi**

### Free-Retry Detection

`retry.py` resolves `workflow_status` before the settlement service calls `rate()`:

```python
def resolve_workflow_status(
    failed_run: WorkflowRun,
    retry_run: WorkflowRun | None,
) -> ResolvedWorkflowStatus:
    if retry_run is None:
        return ResolvedWorkflowStatus(status="failed_charged", ...)
    elapsed = (retry_run.started_at - failed_run.started_at).total_seconds()
    if elapsed <= 60:
        return ResolvedWorkflowStatus(status="failed_free_retry", retry_run_id=retry_run.id)
    return ResolvedWorkflowStatus(status="failed_charged", ...)
```

`retry_run` is found by querying `workflow_runs` for matching `tenant_id + workflow_id + workflow_input_hash` started after the failed run. `workflow_input_hash` is SHA-256 of the input payload, stored at reservation time.

**Free retry confirmed:** settlement service inserts `adjustment` ledger entries reversing the failed run's charges (`related_entry_id` → original debit entry), then rates the retry run normally.

### Vendor Outage Reversal

`failure_reason` codes matching the configurable outage list in `cost_model_versions.payload` trigger full reversal regardless of the 60-second window. `rating_decisions.reversal_reason = "vendor_outage"` flags these for SLA credit tracking.

### Partial Failure Charging (`fixed` + `per_unit`)

When `workflow_status = "failed_charged"` and model is `fixed` or `per_unit`: `rate()` filters events to `occurred_at < workflow.failed_at`. Charged events listed individually in `RatingDecision.breakdown`.

### Late Event Handling

A usage event for a `permanently_closed` billing period is accepted at ingest (never rejected). Settlement detects the closed period and:

1. Rates against the `rating_rule` version active at `workflow_run.started_at` (not current)
2. Inserts `adjustment` ledger entry in the current open period:
   - `entry_type = "late_adjustment"`
   - `related_entry_id` → original closed-period debit
   - `reference_period` → closed period identifier (new nullable column on `credit_ledger`)
3. Flagged as `late_adjustments` line in monthly close report

---

## Section 7: ENG-31 — Vendor Cost Reconciliation + Drift Alarms

**Owner: Saahithi**

### Trigger

Celery beat fires on the 5th of each month at 06:00 UTC. Finance uploads vendor CSVs to:
```
s3://nexoraa-billing/vendor-invoices/{vendor}/{YYYY}/{MM}/invoice.csv
```
CSV columns: `date`, `model`, `input_tokens`, `output_tokens`, `amount_usd`.

### Reconciliation Logic (`reconciliation.py`)

```python
def reconcile_vendor_month(vendor: str, year: int, month: int) -> ReconciliationResult:
    # 1. Download CSV from S3 via ext_storage.storage
    # 2. Aggregate by (vendor, model, date) → invoice_totals
    # 3. Query: SELECT vendor, model, DATE(occurred_at), SUM(raw_cost_usd)
    #           FROM usage_events WHERE vendor = :vendor AND period = :period
    # 4. Compute drift_pct = (event_sum - invoice_sum) / invoice_sum per tuple
    # 5. Write reconciliation_runs row (always — clean runs are SOC 2 evidence)
    # 6. Emit CloudWatch metric credits/vendor_drift_pct per vendor
    # 7. SNS alert to Finance if |drift_pct| > 2%
```

**`reconciliation_runs` row includes:** `vendor`, `period`, `event_sum_usd`, `invoice_sum_usd`, `drift_pct`, `breakdown` (JSONB per model/date), `run_at`, `triggered_by`.

Clean runs (drift < 2%) are still recorded — required as SOC 2 Type I control evidence (ENG-35).

### Report Export

```
s3://nexoraa-billing/reconciliation-reports/{YYYY}/{MM}/{vendor}-report.csv
```
Columns: `model`, `date`, `event_usd`, `invoice_usd`, `drift_pct`, `status`.

`status` values: `ok` | `drift` | `missing_invoice` | `missing_events`. Missing data is a separate status — not folded into drift percentage.

---

## Phase 2 Gate Conditions

Before Phase 3 starts, all of the following must be true:

| Condition | Verified by |
|---|---|
| ENG-27: `hybrid` and `value_based` pass all 7 golden fixtures | CI |
| ENG-28: Anomaly detection fires in controlled test, alerts correctly | Nithilesh runbook |
| ENG-29: ≥1 production tenant on `enforce_block` for ≥5 business days with zero incidents | Dinesh ramp runbook |
| ENG-30: Free-retry and partial-failure verified end-to-end in staging | Saahithi E2E test |
| ENG-31: Vendor reconciliation run against ≥1 real invoice with drift < 2% | Saahithi + Finance |

---

## Cross-Cutting Rules

1. **No service imports another service.** All shared types via `_types.py`.
2. **`rate()` invariants from ADR-0004 are absolute.** Any proposed change to the function signature or purity requires a spec amendment and Finance sign-off.
3. **`audit_log` on every money-touching and mode-changing action.** No exceptions.
4. **`observe_only` tenants are never auto-suspended.** The anomaly scanner must check enforcement mode before applying suspension.
5. **Late events never rejected.** Always stored; settlement handles the closed-period case.
6. **`reference_period` on `credit_ledger` is nullable.** Only set for `late_adjustment` entries. Never backfilled.
