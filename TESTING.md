# Verifying the Revenue Dashboard Fixes

Prereqs: stack running (`docker compose up --build -d`) → frontend `http://localhost:3000`,
backend `http://localhost:8000`. Seed data is all in **March 2024**, so test with **year=2024,
month=3**.

Clients: **Sunset (tenant-a)** `sunset@propertyflow.com` / `client_a_2024` ·
**Ocean (tenant-b)** `ocean@propertyflow.com` / `client_b_2024`.

---

## A. Fastest check — API (copy/paste)

```bash
API=http://localhost:8000/api/v1
TA=$(curl -s -X POST $API/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"sunset@propertyflow.com","password":"client_a_2024"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
TB=$(curl -s -X POST $API/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"ocean@propertyflow.com","password":"client_b_2024"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# Bug #1 (timezone) + #3 (rounding): Sunset prop-001, March
curl -s -H "Authorization: Bearer $TA" "$API/dashboard/summary?property_id=prop-001&year=2024&month=3"; echo
# Bug #2 (isolation): Ocean asks the SAME property id right after
curl -s -H "Authorization: Bearer $TB" "$API/dashboard/summary?property_id=prop-001&year=2024&month=3"; echo
```

| Call | Expected | Proves |
|------|----------|--------|
| Sunset `prop-001` March | `total_revenue:"2250.00"`, `reservations_count:4`, `tenant_id:"tenant-a"` | #1 boundary booking counted in March; #3 exact string |
| Ocean `prop-001` March | `total_revenue:"0.00"`, `reservations_count:0`, `tenant_id:"tenant-b"` | #2 no cross-tenant leak (not 2250) |

### Bug #1 boundary proof (same booking, different month)
```bash
curl -s -H "Authorization: Bearer $TA" "$API/dashboard/summary?property_id=prop-001&year=2024&month=2"; echo
```
Expect `"0.00"`, `0` reservations. The booking `res-tz-1` is `2024-02-29 23:30 UTC` =
`2024-03-01 00:30` in Paris, so it belongs to **March, not February**.
*Before the fix it was the reverse: March showed 1000.00/3 (booking dropped), Feb absorbed it.*

### Bug #2 both cache orders + key namespacing
```bash
docker exec the-flex-code-test-redis-1 redis-cli flushall >/dev/null   # local test cache only
curl -s -H "Authorization: Bearer $TB" "$API/dashboard/summary?property_id=prop-001&year=2024&month=3"; echo  # B first
curl -s -H "Authorization: Bearer $TA" "$API/dashboard/summary?property_id=prop-001&year=2024&month=3"; echo  # A second
docker exec the-flex-code-test-redis-1 redis-cli KEYS 'revenue:*'
```
Ocean always gets `0.00`, Sunset always `2250.00` regardless of order. Keys are distinct:
`revenue:tenant-a:prop-001:2024:3` and `revenue:tenant-b:prop-001:2024:3`.

### Bug #3 aggregate-first rounding
Sunset `prop-001` March = `2250.00`, **not** `2249.99`. The three sub-cent bookings
(`333.333 + 333.333 + 333.334`) are summed exactly to `1000.000` then rounded once — not
rounded per booking (which would lose a cent). Response is a string, never a float.

### Edge cases
```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TA" "$API/dashboard/summary?property_id=prop-001&year=2024&month=13"  # 400 bad month
curl -s -o /dev/null -w "%{http_code}\n" "$API/dashboard/summary?property_id=prop-001&year=2024&month=3"                                   # 401 no auth
```

### Cross-check against the database (ground truth)
```bash
docker exec the-flex-code-test-db-1 psql -U postgres -d propertyflow -tA \
  -c "SELECT tenant_id, property_id, COUNT(*), SUM(total_amount) FROM reservations GROUP BY 1,2 ORDER BY 1,2;"
```
Dashboard totals must match these sums (tenant-a/prop-001 = 2250.000/4; tenant-b has no prop-001).

---

## B. UI check (for the Loom)

1. Open `http://localhost:3000`, log in as **Sunset**.
2. Property = *Beach House Alpha* (prop-001), period = **March 2024** → **USD 2,250.00**, 4 bookings.
3. Switch month to **February 2024** → **USD 0.00** (the boundary booking is correctly in March).
4. Log out, log in as **Ocean**, select prop-001, March 2024 → **USD 0.00 / 0** — Ocean never
   sees Sunset's 2,250 even right after Sunset loaded it. (This is the privacy fix.)
5. Totals always render with exactly two decimals and no `…9999` artifacts.

---

## C. Regression sanity (other properties, March 2024)
| Login | Property | Expected total / count |
|-------|----------|------------------------|
| Sunset | prop-002 | 4975.50 / 4 |
| Sunset | prop-003 | 6100.50 / 2 |
| Ocean  | prop-004 | 1776.50 / 4 |
| Ocean  | prop-005 | 3256.00 / 3 |

---

## What each result maps to
- **2250.00 (not 1000.00) for March** → Bug #1 timezone fix (`services/reservations.py`).
- **Ocean 0.00, not Sunset's 2250** → Bug #2 tenant-scoped cache key (`services/cache.py`).
- **Exact `"2250.00"` string, aggregate-first** → Bug #3 decimal precision (`api/v1/dashboard.py`).
- **Real numbers at all (not mock)** → DB connectivity fix (`core/database_pool.py`).

---

## D. Side-by-side demo — buggy baseline vs fixed (best for the Loom)

Two full stacks run at once so every difference is visible in the browser:

| | **Baseline (buggy)** | **Fixed** |
|---|---|---|
| Frontend | http://localhost:3001 | http://localhost:3000 |
| Backend | http://localhost:8001 | http://localhost:8000 |
| Code | branch `baseline-buggy` (initial commit) | branch `main` |
| Worktree | `../the-flex-code-test-baseline` | this repo |

**Setup (already running).** Baseline came from a worktree off the initial commit:
```bash
git branch baseline-buggy d1835ec5d
git worktree add ../the-flex-code-test-baseline baseline-buggy
# in the worktree: docker-compose.yml ports remapped to 3001/8001/5434/6381,
# frontend built with VITE_BACKEND_URL=http://localhost:8001
cd ../the-flex-code-test-baseline && docker compose -p flex-baseline up --build -d
```

**What the two stacks show for `prop-001`:**

| Check | Baseline (:3001) | Fixed (:3000) | Bug |
|-------|------------------|---------------|-----|
| Sunset prop-001, March | **1000.0** · 3 bookings · float, no tenant/period | **"2250.00"** · 4 · string · tenant-a · March | #1 wrong number/timezone; #3 float→string |
| Ocean prop-001 | **1000.0** — identical to Sunset (leaked/fabricated) | **"0.00"** · 0 · tenant-b | #2 cross-tenant isolation |
| February boundary | no month concept in UI | **0.00** (booking correctly in March) | #1 timezone |
| Redis key | `revenue:prop-001` (tenant-blind) | `revenue:tenant-a:prop-001:2024:3` | #2 |
| Month/year selector | absent | present (defaults March 2024) | #1 |

**Loom flow:** put the two browser tabs side by side (3001 vs 3000). Log into both as
**Sunset** → prop-001: baseline shows **1000** float, fixed shows **2,250.00** and lets you flip
to February (→ 0.00). Then log into both as **Ocean** → prop-001: baseline shows the **same 1000**
(the privacy leak), fixed shows **0.00** for tenant-b.

**Teardown when done:**
```bash
cd ../the-flex-code-test-baseline && docker compose -p flex-baseline down -v
cd ../the-flex-code-test        && docker compose down            # fixed stack
git worktree remove ../the-flex-code-test-baseline               # optional
git branch -D baseline-buggy                                     # optional
```
