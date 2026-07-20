# AGENT-TASK.md — Parallel Fix Plan

Implementation plan for the fixes diagnosed in `ISSUE.md`. Work is partitioned **by layer into
disjoint file sets** so agents can edit **in parallel with no write conflicts**. The only
cross-agent coupling is the **interface contract** (frozen below); agents implement to it, and the
orchestrator integrates + verifies afterward.

## Why partition by layer, not by bug

The four issues overlap on shared files, so a naïve "one agent per bug" split would collide:

| File | §A DB | Bug 2 cache | Bug 3 round | Bug 1 tz/month |
|------|:----:|:----:|:----:|:----:|
| `core/database_pool.py` | ✏️ | | | |
| `services/reservations.py` | ✏️ | | | ✏️ |
| `services/cache.py` | | ✏️ | | ✏️ |
| `api/v1/dashboard.py` | | | ✏️ | ✏️ |
| frontend | | | ✏️ | (✏️) |

Re-partitioned by layer → each file has exactly one owner → safe concurrency.

## Dependency analysis

- **File-level:** all 5 tasks own disjoint files ⇒ **no write dependency ⇒ run all in parallel.**
- **Logical (contract) coupling only**, resolved by freezing the contract before spawning:
  `frontend → HTTP → dashboard.py → cache.py → reservations.py → db_pool`.
  Each layer codes against the frozen signatures, so no edit needs another edit to land first.
- **Integration** (contract wiring correctness) is verified by the orchestrator after all agents
  finish — this is the one genuinely serial step and is not parallelizable.

---

## FROZEN INTERFACE CONTRACT (all agents code to this — do not change)

### HTTP — `GET /api/v1/dashboard/summary`
- Query params: `property_id: str` (required), `year: int` (optional → default current UTC year),
  `month: int` (optional → default current UTC month, 1–12).
- Auth: tenant comes from `current_user.tenant_id`. If missing/unresolved → **HTTP 400**
  `{"detail": "Tenant context required"}` (never substitute `default_tenant`).
- **200 response:**
  ```json
  {
    "property_id": "prop-001",
    "tenant_id": "tenant-a",
    "total_revenue": "2250.00",
    "currency": "USD",
    "reservations_count": 4,
    "period": { "year": 2024, "month": 3 }
  }
  ```
  `total_revenue` = **exact decimal string, 2 dp, aggregate-first, ROUND_HALF_UP**. Never a float.
- DB unavailable → **HTTP 503** `{"detail": "Revenue data temporarily unavailable"}`. No mock data.

### Service — `services/cache.py`
```python
async def get_revenue_summary(property_id: str, tenant_id: str, year: int, month: int) -> dict:
    cache_key = f"revenue:{tenant_id}:{property_id}:{year}:{month}"
    # HIT: if payload["tenant_id"] != tenant_id -> treat as miss (defense in depth)
    # MISS: result = await calculate_monthly_revenue(property_id, tenant_id, month, year)
    #       cache 300s; return result
```

### Service — `services/reservations.py`
```python
async def calculate_monthly_revenue(property_id: str, tenant_id: str, month: int, year: int) -> dict:
    # 1. look up properties.timezone WHERE id=property_id AND tenant_id=tenant_id
    # 2. tz-aware month bounds in that timezone (ZoneInfo)
    # 3. SUM(total_amount), COUNT(*) WHERE property_id AND tenant_id
    #    AND check_in_date >= start AND check_in_date < end
    # returns EXACT (not yet 2dp-rounded) aggregate:
    #   {"property_id","tenant_id","total": "<exact decimal string>","currency","count"}
    # raises on DB failure (NO mock_data fallback)
```
Rounding to 2 dp happens **once**, at the API boundary (dashboard.py) — single rounding point.

### `core/database_pool.py`
- Build URL from `settings.database_url`, coercing scheme to `postgresql+asyncpg://`.
- `create_async_engine(...)` with the **default async pool** (remove `poolclass=QueuePool`).
- Keep the module-level singleton `db_pool`; make `initialize()` **idempotent** (safe to call
  repeatedly; no-op if already initialized).

---

## TASKS (all parallel)

### Agent A — Database connectivity  ·  owns `backend/app/core/database_pool.py`
- Coerce `settings.database_url` → `postgresql+asyncpg://` (it currently reads nonexistent
  `settings.supabase_db_*`).
- Remove `poolclass=QueuePool`; use default async pool. Keep pool sizing via settings if present.
- Make `initialize()` idempotent; expose the existing module-level `db_pool` for reuse.
- **Accept:** `python -c "import ast; ast.parse(open('backend/app/core/database_pool.py').read())"`
  passes; engine URL derives from `settings.database_url`; no `QueuePool` import used for the engine.

### Agent B — Revenue service  ·  owns `backend/app/services/reservations.py`
- Implement `calculate_monthly_revenue(property_id, tenant_id, month, year)` per contract
  (timezone-aware bounds via `properties.timezone`, tenant+property+period filter, aggregate-first
  exact Decimal string). Use `from zoneinfo import ZoneInfo`.
- Use the shared `db_pool` (`from app.core.database_pool import db_pool`), init idempotently — do
  **not** create a new `DatabasePool()` per call.
- **Delete the `mock_data` fallback**; on DB failure, raise (let the API map to 503).
- Leave `calculate_total_revenue` intact only if trivially reused; otherwise it may delegate to the
  monthly function. Do not break imports.
- **Accept:** module parses; `calculate_monthly_revenue` signature matches contract; no `mock_data`;
  no per-call `DatabasePool()`.

### Agent C — Cache layer  ·  owns `backend/app/services/cache.py`
- Change `get_revenue_summary` to `(property_id, tenant_id, year, month)`; key
  `f"revenue:{tenant_id}:{property_id}:{year}:{month}"`.
- Call `calculate_monthly_revenue(property_id, tenant_id, month, year)`.
- On cache HIT, verify `payload["tenant_id"] == tenant_id`; if not, treat as miss and recompute.
- **Accept:** module parses; key includes tenant+period; tenant-match check present.

### Agent D — API endpoint  ·  owns `backend/app/api/v1/dashboard.py`
- Add `year: int | None = None`, `month: int | None = None` query params; default to current
  UTC year/month; validate `1 <= month <= 12` (else 400).
- Reject missing tenant with **HTTP 400** (remove the `"default_tenant"` substitution).
- Call `get_revenue_summary(property_id, tenant_id, year, month)`.
- Quantize the returned exact total **once**: `Decimal(total).quantize(Decimal("0.01"), ROUND_HALF_UP)`
  and return it as a **string** in `total_revenue`. Add `tenant_id` and `period` to the response.
- Wrap DB errors → **HTTP 503** (no fabricated data).
- **Accept:** module parses; response matches contract; returns string total; 400 on no tenant;
  503 on DB error.

### Agent E — Frontend  ·  owns `frontend/src/components/RevenueSummary.tsx`,
`frontend/src/components/Dashboard.tsx`, `frontend/src/lib/secureApi.ts`
- `secureApi.getDashboardSummary(propertyId, { year, month, simulatedTenant?, timestamp? })` →
  append `year` & `month` query params.
- `RevenueSummary`: type `total_revenue: string`; format the string for display (no
  `Number * 100 / 100` float math); accept `year`/`month` props; render `period`. The
  "Precision Mismatch Detected" block can be removed (no longer meaningful with string totals).
- `Dashboard`: add a month/year selector (**default `year=2024, month=3`** so the seeded March demo
  shows the corrected number); pass selection to `RevenueSummary`.
- **Accept:** `tsc`/build not required from the agent; ensure valid TSX and that `total_revenue` is
  consumed as a string; year/month are sent.

---

## Constraints for every agent
- **Edit ONLY your owned file(s).** Do not touch another agent's files.
- Do **not** run servers, Docker, `npm install`, or `git commit`. Make edits + local static checks
  (syntax/parse) only.
- Follow the frozen contract exactly — signatures, key format, response shape, status codes.
- Match existing code style; keep changes minimal and localized.

## Orchestrator (serial, after all agents finish)
1. Integration review: verify signatures/keys/response line up across layers.
2. `docker-compose up --build`; confirm DB connects (no mock fallback path taken).
3. Run the `ISSUE.md` validation matrix (March=2250.00/4, Ocean prop-001=0.00/0, cross-tenant
   cache isolation both orders, aggregate rounding, 400 on no tenant, 503 on DB down).
4. Report diff summary + proposed commit message; await approval (no auto-commit).
