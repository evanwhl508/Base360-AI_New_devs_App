from datetime import datetime
from decimal import Decimal
from typing import Dict, Any, List
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.core.database_pool import db_pool


async def calculate_monthly_revenue(property_id: str, tenant_id: str, month: int, year: int) -> dict:
    """Timezone-aware monthly revenue for one property, aggregate-first exact Decimal."""

    await db_pool.initialize()  # idempotent

    async with db_pool.get_session() as session:
        # Resolve the property timezone, scoped to the tenant so we never
        # leak data across tenants.
        tz_row = (await session.execute(
            text("""
                SELECT timezone
                FROM properties
                WHERE id = :pid AND tenant_id = :tid
            """),
            {"pid": property_id, "tid": tenant_id},
        )).fetchone()

        if tz_row is None:
            # Property does not belong to this tenant (or does not exist).
            return {
                "property_id": property_id,
                "tenant_id": tenant_id,
                "total": "0",
                "currency": "USD",
                "count": 0,
            }

        tz = ZoneInfo(tz_row.timezone)

        # Timezone-aware month bounds in the property's local zone.
        start = datetime(year, month, 1, tzinfo=tz)
        if month == 12:
            end = datetime(year + 1, 1, 1, tzinfo=tz)
        else:
            end = datetime(year, month + 1, 1, tzinfo=tz)

        agg = (await session.execute(
            text("""
                SELECT SUM(total_amount) AS total, COUNT(*) AS cnt
                FROM reservations
                WHERE property_id = :pid
                  AND tenant_id = :tid
                  AND check_in_date >= :start
                  AND check_in_date < :end
            """),
            {"pid": property_id, "tid": tenant_id, "start": start, "end": end},
        )).fetchone()

        total = agg.total if agg is not None else None
        cnt = agg.cnt if agg is not None else None

        # Return the EXACT aggregate; the API layer rounds to 2dp once.
        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "total": str(Decimal(str(total or 0))),
            "currency": "USD",
            "count": int(cnt or 0),
        }


async def calculate_total_revenue(property_id: str, tenant_id: str) -> Dict[str, Any]:
    """
    Aggregates all-time revenue from the database for one property (tenant-scoped).
    """
    await db_pool.initialize()  # idempotent

    async with db_pool.get_session() as session:
        query = text("""
            SELECT
                SUM(total_amount) AS total_revenue,
                COUNT(*) AS reservation_count
            FROM reservations
            WHERE property_id = :property_id AND tenant_id = :tenant_id
        """)

        row = (await session.execute(query, {
            "property_id": property_id,
            "tenant_id": tenant_id,
        })).fetchone()

        total = row.total_revenue if row is not None else None
        count = row.reservation_count if row is not None else None

        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "total": str(Decimal(str(total or 0))),
            "currency": "USD",
            "count": int(count or 0),
        }
