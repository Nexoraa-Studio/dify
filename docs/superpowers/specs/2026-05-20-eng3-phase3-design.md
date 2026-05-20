# ENG-3 Phase 3 Architecture Design
## Customer Experience & Billing Integration (Weeks 10–13)

**Date:** 2026-05-20
**Author:** Narayana Chavva
**Linear:** [ENG-3](https://linear.app/nexoraa-ai/issue/ENG-3/epic-phase-3-customer-experience-billing-integration-weeks-10-13)
**Status:** Approved — ready for implementation planning
**Prerequisite:** Phase 2 gate (ENG-29 enforce_block live ≥5 days, ENG-27/28/30/31 all verified)

---

## Purpose

This document is the shared architectural contract for all Phase 3 owners. Each engineer implements their assigned ticket against this spec. Changes to interfaces in `_types.py`, shared table schemas, or API contracts require updating this doc and notifying all owners.

**Owners:**

| Engineer | Tickets |
|---|---|
| Dinesh | ENG-32 (customer dashboard — backend + frontend) |
| Nithilesh | ENG-33 (Stripe Billing Adapter), ENG-34 (auto-overage + top-up + notifications), ENG-36 (mTLS — cross-cutting) |
| Narayana | ENG-35 (SOC 2 Type I + DR), ENG-41 (ASC 606 — cross-cutting), ENG-42 (Aurora read replica — new ticket) |
| Saahithi | extends ENG-20 monthly close to trigger ENG-33 billing adapter |

---

## Stable Contracts from Phases 1–2

All Phase 3 code builds on these locked ADRs:

| ADR | Invariant |
|---|---|
| ADR-0001 | All PKs are UUID v7; idempotency keys are SHA-256-derived `TEXT` |
| ADR-0002 | `NUMERIC(18,6)` for all monetary fields; `BIGINT` for all credit fields |
| ADR-0003 | `credit_ledger` is append-only; corrections via `adjustment` entries only |
| ADR-0004 | `rate()` is a pure function: zero I/O, zero clock reads, zero randomness |

**Locked open decisions affecting Phase 3:**

- **Decision #1:** Credits are internal only; customers see outcome counts and dollar amounts per contract — never raw credit amounts, vendor costs, or margin data
- **Decision #11:** Stripe is the billing provider (Finance confirmed)
- **Decision #7:** On `invoice.payment_failed` for Net 30+ tenants, trigger suspension workflow

---

## Section 1: Module Layout

All Phase 3 code extends `api/services/credits/`. New files are marked **NEW**; existing files are untouched unless noted.

```
api/
  services/credits/
    __init__.py
    _types.py                      ← EXTEND: add Phase 3 types (see Section 2)
    rating.py                      ← Phase 1, untouched
    anomaly.py                     ← Phase 2, untouched
    enforcement.py                 ← Phase 2, untouched
    margin_query.py                ← Phase 2, untouched
    retry.py                       ← Phase 2, untouched
    reconciliation.py              ← Phase 2, untouched
    billing_portal.py              ← NEW: ENG-32 read service (Dinesh)
    stripe_adapter.py              ← NEW: ENG-33 invoice push + webhook (Nithilesh)
    topup.py                       ← NEW: ENG-34 Stripe Checkout + overage (Nithilesh)
    notifications.py               ← NEW: ENG-34 threshold events → SES (Nithilesh)
    revenue_recognition.py         ← NEW: ENG-41 ASC 606 classification (Narayana)

  controllers/credits/
    billing_portal_controller.py   ← NEW: /v1/portal/* routes (Dinesh)
    stripe_webhook_controller.py   ← NEW: /v1/billing/stripe-webhook (Nithilesh)
    topup_controller.py            ← NEW: /v1/billing/topup (Nithilesh)

  tasks/credits/
    __init__.py
    anomaly_scan.py                ← Phase 2, untouched
    reconciliation.py              ← Phase 2, untouched
    margin_digest.py               ← Phase 2, untouched
    monthly_close_billing.py       ← NEW: ENG-33 month-close trigger (Saahithi extends ENG-20)
    cap_monitor.py                 ← NEW: ENG-34 threshold watcher (Nithilesh)

web/app/
  (billingLayout)/                 ← NEW route group (Dinesh — ENG-32)
    layout.tsx                     ← own nav: logo, tenant name, logout only
    billing/
      page.tsx                     ← usage overview: cap bar, projected spend
      usage/page.tsx               ← breakdown by workflow / day / user
      invoices/page.tsx            ← invoice list + PDF download
      topup/page.tsx               ← Stripe Checkout redirect handler
      components/
        CapProgressBar.tsx
        UsageBreakdownTable.tsx
        InvoiceRow.tsx

docs/
  soc2/                            ← NEW: ENG-35 automated evidence artefacts
  DR_RUNBOOK.md                    ← NEW: ENG-35 / ENG-42
  superpowers/specs/
    2026-05-20-eng3-phase3-design.md   ← this file
```

**Boundary rule (same as Phase 2):** `billing_portal.py`, `stripe_adapter.py`, `topup.py`, `notifications.py`, and `revenue_recognition.py` must not import from each other. All shared types flow through `_types.py`.

---

## Section 2: Shared Types (`_types.py` additions)

```python
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID


# ── ENG-32: Portal read models ──────────────────────────────────────────────

@dataclass(frozen=True)
class PortalUsageSummary:
    tenant_id: UUID
    billing_period: str                  # "2026-05"
    included_credits_total: int
    credits_used: int
    credits_reserved: int
    hard_cap_credits: int
    overage_credits_used: int
    projected_period_end_credits: int    # linear extrapolation from days elapsed
    cap_pct: Decimal                     # 0.0–1.0; drives colour thresholds in UI


@dataclass(frozen=True)
class PortalWorkflowBreakdown:
    workflow_id: str
    workflow_name: str
    amount_usd: Decimal                  # dollar amount per contract rate — not raw credits
    run_count: int
    period: str


@dataclass(frozen=True)
class PortalInvoiceLine:
    invoice_id: str
    period: str
    line_type: str                       # "platform_fee" | "overage" | "topup"
    description: str
    amount_usd: Decimal
    pdf_url: str | None


# ── ENG-33: Stripe Adapter ───────────────────────────────────────────────────

@dataclass(frozen=True)
class StripeInvoiceResult:
    stripe_invoice_id: str
    tenant_id: UUID
    period: str
    amount_due_cents: int
    status: str                          # "draft" | "open" | "paid" | "void"
    idempotency_key: str                 # SHA-256(tenant_id + period)


# ── ENG-34: Notifications ────────────────────────────────────────────────────

@dataclass(frozen=True)
class NotificationThresholdEvent:
    tenant_id: UUID
    threshold_pct: int                   # 70 | 85 | 95 | 100
    credits_used: int
    hard_cap_credits: int
    billing_contact_email: str
    triggered_at: datetime


# ── ENG-41: ASC 606 Revenue Recognition ─────────────────────────────────────

class RevenueRecognitionStatus(str, Enum):
    DEFERRED = "deferred"
    RECOGNIZED = "recognized"
    RECOGNIZED_IMMEDIATE = "recognized_immediate"
    BREAKAGE = "breakage"
    NONE = "none"


@dataclass(frozen=True)
class RevenueEntry:
    ledger_entry_id: UUID
    entry_type: str
    credit_class: str                    # "included" | "topup" | "promo" | "overage"
    revenue_recognition_status: RevenueRecognitionStatus
    amount_credits: int
    period: str
```

**Design decisions:**
- `cap_pct` is pre-computed in `billing_portal.py` — no arithmetic in React components
- `StripeInvoiceResult.idempotency_key` is `SHA-256(tenant_id + period)` — prevents double-invoice on Celery retry, consistent with Phase 1 ledger idempotency pattern
- `PortalInvoiceLine.amount_usd` is the only monetary field exposed to customers — raw credits never appear in portal types

---

## Section 3: API Contracts

### 3a — Customer Portal (`billing_portal.py`) — ENG-32

All `/v1/portal/` routes require a valid session with `tenant_id` in context. Return dollar amounts and outcome counts only — never raw credits or vendor costs.

```
GET /v1/portal/usage/summary
  → PortalUsageSummary

GET /v1/portal/usage/breakdown
  Query: period=2026-05, group_by=workflow|day|user, page=1, page_size=50
  → list[PortalWorkflowBreakdown]    (paginated, ordered by credits_used desc)

GET /v1/portal/invoices
  Query: page=1, page_size=12
  → list[PortalInvoiceLine]          (ordered by period desc)

GET /v1/portal/invoices/{invoice_id}/download
  → 302 redirect to signed S3 URL (15-min TTL)
  → 404 if invoice_id not owned by requesting tenant
```

Rate limit: 60 req/min per tenant, separate bucket from Studio API limits.

### 3b — Stripe Billing Adapter (`stripe_adapter.py`) — ENG-33 + ENG-37

```
POST /v1/billing/stripe-webhook
  Headers: Stripe-Signature (verified via stripe.webhooks.constructEvent)
  Body: raw bytes — do NOT parse before signature verification

  Handles:
    invoice.paid                  → update stripe_invoices.status = paid
    invoice.payment_failed        → trigger suspension workflow (open decision #7)
    customer.subscription.deleted → set tenant status = suspended,
                                    emit EventBridge tenant.suspended event
    checkout.session.completed    → handled by topup.py (grant topup credits)

  Returns 200 immediately; all processing is async via Celery
  Returns 400 on failed signature verification — log, discard, no retry
  Replay protection: Stripe's 5-minute timestamp tolerance enforced
```

**Monthly close Celery task** (`monthly_close_billing.py`, 1st of month 02:05 UTC):
```
for each tenant with a closed-period wallet:
  1. Read invoice_{tenant_id}_{period}.csv from S3
  2. Retrieve or create Stripe customer (keyed by tenant_id stored in tenants table)
  3. stripe.invoiceItems.create for each line: platform_fee, overage, topup
  4. stripe.invoices.create with idempotency_key = SHA-256(tenant_id + period)
  5. stripe.invoices.finalizeInvoice + stripe.invoices.sendInvoice
  6. Persist StripeInvoiceResult to stripe_invoices table
  7. Emit billing.invoice.pushed on EventBridge
```

### 3c — Top-up, Auto-overage & Notifications (`topup.py`, `notifications.py`) — ENG-34

**Top-up:**
```
POST /v1/billing/topup
  Auth: billing portal session
  Body: { "tenant_id": uuid, "credits": int, "currency": "usd" }
  → Creates Stripe Checkout session (mode=payment, one-time)
  → Returns { "checkout_url": "https://checkout.stripe.com/..." }

On checkout.session.completed webhook:
  INSERT credit_ledger (entry_type=grant, credit_class=topup)
  UPDATE wallet (topup_credits += amount)
  Send confirmation email via SES
```

**Auto-overage** (extends `POST /v1/credits/finalize` from ENG-12):
```
if final_credits > available_credits AND subscription.overage_enabled:
    overage_amount = final_credits - available_credits
    INSERT credit_ledger (entry_type=overage)
    UPDATE wallet (overage_credits += overage_amount)
    # billed at next monthly close as overage line item in invoice CSV
```

**Notifications** (`cap_monitor.py`, Celery beat every 5 min):
```
for each active tenant:
    cap_pct = (credits_used + credits_reserved) / hard_cap_credits
    for threshold in [0.70, 0.85, 0.95, 1.00]:
        if cap_pct >= threshold AND not already_notified(tenant_id, threshold, period):
            emit NotificationThresholdEvent → EventBridge → SNS → SES
            INSERT cap_notifications (tenant_id, threshold_pct, period, notified_at)

At 100%: enforce_block kicks in regardless of tenant's base enforcement_mode
```

**Email infrastructure:** AWS SES in `ap-south-1`. Sending identity: `notifications@nexoraa.ai`. Email templates stored in SES (not hardcoded in Python). Webhook secret fetched from AWS Secrets Manager at startup — never from environment variables.

---

## Section 4: Compliance, DR & Sequencing

### 4a — ENG-41: ASC 606 Revenue Recognition (`revenue_recognition.py`) — Narayana

Hooks into `POST /v1/ledger/entries`. Every ledger write calls `classify_revenue(entry_type, credit_class) → RevenueRecognitionStatus` and stamps the status on the row. Pure function, no I/O (ADR-0004).

```
Classification table:

entry_type=grant,  credit_class=included|topup  → DEFERRED
entry_type=debit,  credit_class=included|topup  → RECOGNIZED
entry_type=grant,  credit_class=promo           → NONE
entry_type=overage                               → RECOGNIZED_IMMEDIATE
entry_type=expiry                                → BREAKAGE
```

Monthly close report (extends ENG-20) gains four new columns:
`recognized_usd`, `deferred_usd`, `breakage_usd`, `promo_grants_usd`

This feeds ENG-33 Stripe invoice line item descriptions and ENG-35 SOC 2 Finance evidence. **Must complete before ENG-33 can go to production.**

### 4b — ENG-36: mTLS Service Mesh (Nithilesh, parallel with ENG-32)

AWS Private CA issues 1-hour certs at service startup. `stripe_adapter.py` and `billing_portal.py` verify client certs on every inbound request. `X-Actor-Permissions` header populated from mTLS-verified service identity only — never from request body or query params. Must complete by end of Week 10.

### 4c — ENG-42: Aurora Multi-Region Read Replica (owner TBD — new ticket)

Scope:
- Terraform: Aurora read replica in `ap-southeast-1` (Singapore), automated failover
- CloudWatch alarm on replication lag > 30 seconds
- Failover runbook in `docs/DR_RUNBOOK.md`: promote replica → update connection strings via SSM Parameter Store → restart stack → validate with reconciliation job
- Estimated 3–4 days. Runs in parallel with ENG-33/34 in Week 11.
- Blocks ENG-35 DR runbook sign-off.

### 4d — ENG-35: SOC 2 Type I Evidence — Narayana

Seven automated artefacts under `tests/soc2/`, tagged `@pytest.mark.soc2`. Run in weekly CI job, excluded from normal test run.

| Script | Evidence | Control |
|---|---|---|
| `audit_log_s3_lock.py` | S3 Object Lock COMPLIANCE + 7yr retention | Immutability |
| `ledger_immutability.py` | UPDATE/DELETE on `credit_ledger` raises exception | Append-only ledger |
| `rls_isolation.py` | Query without `app.current_tenant_id` returns 0 rows | Tenant isolation |
| `kms_encryption.py` | boto3 describe-key per env, result logged to file | Encryption at rest |
| `backup_restore.py` | Monthly: restore Aurora snapshot, run reconciliation, assert 0 discrepancies | Backup integrity |
| `access_review.py` | Export all `nexoraa_admin` grants + financial-permission assignments to CSV | Access control |
| `financial_audit_query.py` | Count `audit_log` entries per permission/actor/quarter | Financial activity audit |

DR runbook (`docs/DR_RUNBOOK.md`) requires sign-off from two team members before ENG-35 is closed.

### 4e — Week-by-Week Sequencing

```
Week 10:  ENG-32  — customer dashboard (Dinesh, no blockers)
          ENG-36  — mTLS (Nithilesh, MUST finish this week — blocks ENG-33)
          ENG-41  — ASC 606 (Narayana, MUST finish this week — blocks ENG-33)
          ENG-42  — Aurora replica start (infra, parallel)

Week 11:  ENG-33  — Stripe adapter (Nithilesh, needs ENG-36 + ENG-41 done)
          ENG-34  — overage/topup/notifications (Nithilesh, needs ENG-32 + SES setup)
          ENG-42  — Aurora replica complete

Week 12:  Integration hardening:
          — E2E staging test: full monthly close cycle (estimate → reserve → finalize → close → Stripe invoice → paid)
          — Stripe webhook failure scenarios: payment_failed → suspension flow
          — SES delivery verification + notification deduplication test
          — DR failover drill against replica

Week 13:  ENG-35  — SOC 2 evidence collection (Narayana, needs ENG-33/34/41/42 all done)
          DR runbook signed off by 2 reviewers
          Phase 3 go/no-go:
            ✅ ≥1 tenant live on customer dashboard
            ✅ ≥1 Stripe invoice sent and paid end-to-end
            ✅ All 7 SOC 2 evidence scripts pass in CI
            ✅ DR failover runbook validated
```

---

## Open Decisions Inherited

| # | Decision | Status |
|---|---|---|
| #7 | Suspension workflow on `invoice.payment_failed` — what exactly gets suspended (all runs, or just new reservations)? | Needs Finance input before ENG-33 impl |
| #11 | Stripe confirmed | Locked |
| New-1 | ENG-42 owner | Needs assignment |
| New-2 | Customer dashboard credit units: ENG-32 checklist says "credits used vs included" but Decision #1 says "credits are internal only." Should `PortalUsageSummary` expose credit counts (as the customer's purchased unit) or translate to dollar equivalents? `cap_pct` works either way; the raw count fields need a decision. | Needs product/Finance input before ENG-32 impl |
