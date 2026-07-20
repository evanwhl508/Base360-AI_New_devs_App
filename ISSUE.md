# Revenue Dashboard — Bug Diagnosis & Fixes

Investigation of the three symptoms reported by the CEO/finance team, revised after a
second-pass audit. Each symptom is traced to a concrete root cause, but two of the three
require more than a one-line edit to actually resolve end-to-end, and one prerequisite
(database connectivity) must be fixed before any fix can be demonstrated against the seed data.

## Executable path (what the live app actually runs today)

```
GET /api/v1/dashboard/summary        (api/v1/dashboard.py)
        │  tenant_id = current_user.tenant_id or "default_tenant"   ← fail-open (§B2)
        ▼
get_revenue_summary(property_id, tenant_id)      (services/cache.py)
        │  cache_key = f"revenue:{property_id}"   ← Bug #2: tenant missing
        ▼
calculate_total_revenue(property_id, tenant_id)  (services/reservations.py)
        │  DatabasePool() built from settings that DON'T EXIST → init fails →
        │  falls back to hard-coded mock_data     ← Prerequisite bug (§A)
        ▼
float(revenue_data['total'])         (api/v1/dashboard.py:18)   ← Bug #3 (reframed)
```

`calculate_monthly_revenue()` — where the timezone logic for Bug #1 lives — **is never called
by this path** (`services/cache.py:24` calls `calculate_total_revenue`, not the monthly one).
The endpoint has no month/year parameter and returns an all-time total. This materially changes
the fixes below.

| # | Reported by | Symptom | Status of root cause |
|---|-------------|---------|----------------------|
| A | (prerequisite) | — | **DB never connects** → app serves mock data; must fix to demo anything |
| 2 | Client B (Ocean) | Sees another company's revenue on refresh | ✅ Confirmed, reachable, near-one-line fix |
| 3 | Finance | Totals "off by a few cents" | ⚠️ Reframed — it's a **rounding-contract** bug, not IEEE-754 |
| 1 | Client A (Sunset) | March totals don't match | ⚠️ Real timezone defect, but in **dead code**; needs wiring + DB |

Recommended order: **A → 2 → 3 → 1**.

---

## §A — Prerequisite: the database never connects (app serves mock data)

`DatabasePool.initialize()` builds its URL from settings that **do not exist** in `config.py`:

```python
# backend/app/core/database_pool.py:18
database_url = f"postgresql+asyncpg://{settings.supabase_db_user}:{settings.supabase_db_password}@{settings.supabase_db_host}:{settings.supabase_db_port}/{settings.supabase_db_name}"
```

`config.py` defines only `database_url` (pointing at the docker `db` service). The
`supabase_db_*` attributes are undefined → `AttributeError` → pool init swallows it →
`calculate_total_revenue` hits `except` and returns the hard-coded `mock_data` dict
(`services/reservations.py:93-101`).

**Correction to my first draft:** I claimed the mock totals match the real DB sums. That is
**wrong for `prop-001`** — precisely the property involved in Bugs #1 and #2:

| Tenant / property | Seeded DB (all-time) | Mock fallback | Match? |
|-------------------|----------------------|---------------|--------|
| tenant-a / prop-001 | **2250.000, 4 bookings** | 1000.00, 3 bookings | ❌ |
| tenant-b / prop-001 | **0.000, 0 bookings** | 1000.00, 3 bookings | ❌ |
| tenant-a / prop-002 | 4975.50, 4 | 4975.50, 4 | ✅ |
| tenant-a / prop-003 | 6100.50, 2 | 6100.50, 2 | ✅ |
| tenant-b / prop-004 | 1776.50, 4 | 1776.50, 4 | ✅ |
| tenant-b / prop-005 | 3256.00, 3 | 3256.00, 3 | ✅ |

Note the mock `prop-001` value (1000.00, 3 bookings) is exactly the **timezone-buggy** March
result — the real March total *minus* the boundary booking `res-tz-1`. The mock also **ignores
`tenant_id`**, so Ocean (`tenant-b`) gets `1000.00` for `prop-001` even though it owns no such
reservations. This means the mock fallback **masks Bug #2's real severity and fabricates
financial data for a tenant that has none.**

### What "connect the DB" requires (not external infra — the container already exists)
- Docker network address: `postgresql+asyncpg://postgres:postgres@db:5432/propertyflow`
  (host-mapped at `localhost:5433`). Use `settings.database_url` and coerce the scheme to
  `postgresql+asyncpg://`.
- **Remove `poolclass=QueuePool`** from `create_async_engine` — `QueuePool` is the *sync* pool;
  async engines need `AsyncAdaptedQueuePool` (the default). Passing `QueuePool` raises at connect.
- **Reuse one application-level pool.** `calculate_total_revenue` currently does
  `db_pool = DatabasePool()` + `initialize()` **per request** and never closes it
  (`services/reservations.py:43`) — a connection leak. Use the module-level `db_pool` and
  initialize once at startup.
- **Do not fabricate financial data on DB failure.** Replace the `mock_data` fallback with an
  error response (HTTP 503) so a real outage can never silently return invented revenue.

> Scope note: the assignment says "don't rebuild." Connecting to the *provided* Postgres
> container is a bug fix, not a rebuild — but if you intend to validate purely via unit tests
> with a mocked session, §A can be documented rather than fixed. **A live UI/login demo cannot
> show the real fixes for #1 or #3 until §A is done**, because the seed data is never queried.

---

## §Bug 2 — Client B sees another company's numbers (cross-tenant cache leak)

**Report:** *"Sometimes when we refresh, we see revenue numbers that belong to another company."*

### Root cause (confirmed — the strongest finding)
```python
# backend/app/services/cache.py:13
cache_key = f"revenue:{property_id}"     # tenant dropped
```
Property IDs are unique **per tenant** (composite PK `(id, tenant_id)`, `schema.sql:18`), and the
seed gives both tenants a `prop-001` (`seed.sql:8-9`). Whichever client warms `revenue:prop-001`
first (5-min TTL) is served to the other → cross-tenant data leak. Intermittent by nature
(depends on cache warmth/TTL), matching "sometimes when we refresh."

### Fix
```python
cache_key = f"revenue:{tenant_id}:{property_id}"
```
**Defense in depth (recommended):** after a cache hit, verify the payload's `tenant_id` matches
the requested tenant before returning it, so a future key-construction slip can't silently leak.

### Validate
- Cold cache → `get_revenue_summary('prop-001','tenant-a')` then `('prop-001','tenant-b')`;
  assert the second response's `tenant_id == 'tenant-b'` (before fix it returns tenant-a's object).
- Reverse order (B then A) → same guarantee.
- Concurrent requests for both tenants → no bleed.
- Test at the **API** layer (with each client's login), not only the service function.
- Inspect keys without disrupting shared state: `SCAN MATCH revenue:*` should show two distinct
  keys `revenue:tenant-a:prop-001`, `revenue:tenant-b:prop-001`. **Avoid `FLUSHALL`/`KEYS`** in
  any shared Redis; use an isolated test DB or a unique key prefix.
- ⚠️ This fix is necessary but **not sufficient while §A's tenant-blind mock fallback is live** —
  the mock returns `prop-001 = 1000.00` for *both* tenants regardless of the cache key.

---

## §Bug 3 — "Off by a few cents" (rounding contract, not IEEE-754)

**Report:** *"Totals seem slightly off by a few cents… couldn't pin down when or why."*

### Reframed root cause
`api/v1/dashboard.py:18` casts the exact `Decimal` to `float`:
```python
total_revenue_float = float(revenue_data['total'])
```
This is a genuine anti-pattern for a financial API. **However — correcting my first draft — I
could not reproduce a cent-level error from this cast on the seed data.** The code converts an
*already-aggregated* Decimal once (it does not sum floats), and every real seed total
(2250.000, 4975.50, 6100.50, 1776.50, 3256.00) is exactly representable as a double and
round-trips through JSON with **zero drift**. The ~2e-12 drift I showed earlier came from float
*aggregation* of random values — not this code path. That evidence was overstated; withdrawn.

The **real, seed-backed** "few cents" mechanism is an undefined **rounding contract**:
- DB stores `NUMERIC(10,3)` — three decimals, explicitly "sub-cent precision" (`schema.sql:28`).
- UI displays 2 decimals.
- Nowhere is it defined whether rounding happens **per reservation** or **after aggregation**.

The seed makes this concrete (`prop-001`/`tenant-a`, the `res-dec-*` bookings):
```
333.333 + 333.333 + 333.334  (NUMERIC(10,3))
  aggregate-first, then round → 1000.00
  round each reservation first → 333.33 × 3 = 999.99   ← 1 cent off
```
Any place that rounds per-reservation while another sums-then-rounds will disagree by cents —
exactly "here and there… couldn't pin down when or why."

### Fix
Define the contract explicitly: **aggregate first, quantize once, transport exactly.**
```python
from decimal import Decimal, ROUND_HALF_UP
total = Decimal(revenue_data['total']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
return {..., "total_revenue": str(total), ...}   # exact string, no float
```
- JSON has no decimal type — for exact transport use a **decimal string** (or integer minor
  units / cents). "Decimal-aware JSON number" (my first draft's wording) is misleading; dropped.
- Confirm rounding **point** (aggregate-first) and **mode** (HALF_UP vs banker's) as a business
  decision, and per-currency precision (see §B4 — not all currencies are 2 dp).
- **Frontend contract change is required, not just the TS type.** `RevenueSummary.tsx` currently
  does `Math.round(total_revenue * 100)/100`, subtracts, and formats it as a `number`
  (and JS has its own edge cases, e.g. `1.005`). Moving to a string means updating that
  arithmetic/formatting, or standardizing on integer cents end-to-end.

### Validate
- `333.333 + 333.333 + 333.334` returns `1000.00` (aggregate-first), never `999.99`.
- Response for a clean total is the exact string `"X.XX"` with no float tail.
- The latent "Precision Mismatch Detected" banner (`RevenueSummary.tsx:107`) never fires.

---

## §Bug 1 — March total is wrong (timezone) — real defect, but in dead code

**Report:** *"Different totals for March."*

### Root cause
`calculate_monthly_revenue()` builds **naive** month bounds and compares them against tz-aware
`check_in_date`, ignoring each property's timezone:
```python
# backend/app/services/reservations.py:10-14
start_date = datetime(year, month, 1)            # naive → treated as UTC
end_date   = datetime(year, month + 1, 1)
```
Every property has a `timezone` column (`schema.sql:16`) that is never used. The seed plants the
failure: `res-tz-1` at `2024-02-29 23:30 UTC` = **`2024-03-01 00:30` Paris** (`prop-001`/`tenant-a`
is `Europe/Paris`). In Paris-local terms it's a March booking worth **$1,250**, but naive-UTC
bounds exclude it → March under-reports by $1,250.

### Why the fix is more than a timezone edit
`calculate_monthly_revenue` is **not wired into the dashboard** and is itself incomplete:
returns `Decimal('0')`, never runs its SQL, references an **undefined `tenant_id`**, takes no
`tenant_id`, never fetches the property timezone; and the endpoint/cache carry **no month/year**.
A real fix must thread the reporting period and tenant through the whole path:

```
endpoint(property_id, month, year)  →  cache key f"revenue:{tenant_id}:{property_id}:{year}:{month}"
  →  fetch properties.timezone WHERE id=:property_id AND tenant_id=:tenant_id
  →  month bounds localized to that tz:
        tz = ZoneInfo(property_timezone)
        start = datetime(year, month, 1, tzinfo=tz)
        end   = (datetime(year+1,1,1,tzinfo=tz) if month==12
                 else datetime(year, month+1, 1, tzinfo=tz))
  →  WHERE check_in_date >= :start AND check_in_date < :end
        AND property_id=:property_id AND tenant_id=:tenant_id
```
(Equivalent SQL-side: compare `check_in_date AT TIME ZONE properties.timezone` against
naive local bounds.)

> This is the point of most tension with "don't rebuild": the endpoint genuinely has no month
> concept today. If a month selector is out of scope, at minimum fix the timezone logic and note
> that month-scoped reporting is required to satisfy Client A's request.

### Validate
- `prop-001`/`tenant-a`, **March 2024** → `2250.00`, **4** reservations (includes `res-tz-1`).
  Before fix: `1000.00`, 3 (boundary booking dropped) — and note this is exactly today's mock value.
- Same booking is **excluded** from February.
- A `America/New_York` (UTC−5) property shifts the opposite way: a `00:30 UTC Mar 1` booking
  counts as **February** locally.
- Mid-month booking (`res-004`, Mar 5) lands in March regardless of tz (regression guard).

---

## §B — Documented follow-ups (fail-open / isolation; not required to fix for this exercise)

These are real weaknesses surfaced during the audit. Recommended to **document**, not necessarily
fix, within a debugging exercise — but the backend must remain the authorization boundary.

- **B1 — Frontend hard-codes both tenants' properties.** `Dashboard.tsx:4-10` lists all five
  properties (incl. other tenants' names) for every user. This leaks *names*, but is **not** the
  cause of the revenue leak (that's §Bug 2, server-side). Fix = tenant-scoped property endpoint;
  follow-up. UI filtering must never be the security boundary.
- **B2 — Fail-open tenant resolution.** `tenant_resolver.py:92` defaults **any** unknown user to
  `tenant-a`; `dashboard.py:14` substitutes `"default_tenant"` when tenant context is missing.
  Safer default is to **reject** unresolved-tenant requests.
- **B3 — RLS enabled, no policies.** `schema.sql:35-36` enables RLS but defines no policies, and
  the app connects as the Postgres owner (which bypasses RLS in Docker). Isolation currently rests
  entirely on application `WHERE tenant_id` clauses.
- **B4 — Multi-currency ignored.** `calculate_total_revenue` sums all `total_amount` and hard-codes
  `"USD"`. Seed is USD-only so it's latent, but summing mixed currencies is incorrect; a real fix
  groups by currency or converts with an explicit rate/date. Currency also affects rounding
  precision (§Bug 3). Out of scope unless mixed-currency data is added.
- **B5 — Backend must reject cross-tenant property IDs.** A request for a `property_id` the
  authenticated tenant doesn't own should return 403/404, independent of the cache fix.

---

## Validation matrix

| Scenario | Expected result |
|----------|-----------------|
| Sunset, prop-001, March 2024 | `2250.00`, 4 reservations (boundary booking included) |
| Sunset, prop-001, February 2024 | boundary booking `res-tz-1` **excluded** |
| Ocean, prop-001 | `0.00`, 0 reservations (owns no such bookings) |
| Sunset request → then Ocean request (shared prop-001) | Ocean never receives Sunset's cached data |
| Ocean request → then Sunset request | Sunset never receives Ocean's cached data |
| `333.333 + 333.333 + 333.334` | aggregate-first `1000.00` (never `999.99`) |
| Missing / unresolved tenant context | request **rejected** (not `default_tenant`) |
| Property owned only by another tenant | **403/404** |
| Database unavailable | error (HTTP 503), **not** fabricated financial data |

---

## Scope summary

**Fix for a credible end-to-end demo**
1. §A connect the provided Docker Postgres (async URL, drop `QueuePool`, single reused pool,
   remove the fabricated mock fallback).
2. §Bug 2 add `tenant_id` to the cache key (+ tenant-match check on read).
3. §Bug 3 define aggregate-first rounding; return an exact decimal string (update the frontend
   contract accordingly).
4. §Bug 1 connect + timezone-scope the monthly calculation (thread tenant/month/year, use
   `properties.timezone`).
5. Focused automated tests per the matrix above.

**Document as follow-up** — B1 hard-coded frontend list · B2 fail-open tenant default ·
B3 missing RLS policies · B4 multi-currency aggregation · B5 cross-tenant property authorization.

**Honesty corrections vs. first draft:** mock ≠ DB for `prop-001` (§A table); the IEEE-754
"few cents" claim was overstated and is withdrawn in favour of the rounding-contract cause
(§Bug 3); Bug #1's fix is not a one-line timezone edit because the monthly function is dead code
(§Bug 1); DB connectivity promoted from "secondary note" to prerequisite (§A).
