from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from app.services.cache import get_revenue_summary
from app.core.auth import authenticate_request as get_current_user

router = APIRouter()

@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    year: int | None = None,
    month: int | None = None,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:

    now = datetime.now(timezone.utc)
    if year is None:
        year = now.year
    if month is None:
        month = now.month
    if not 1 <= month <= 12:
        raise HTTPException(status_code=400, detail="Invalid month")

    tenant_id = getattr(current_user, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Tenant context required")

    try:
        revenue_data = await get_revenue_summary(property_id, tenant_id, year, month)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Revenue data temporarily unavailable")

    total = Decimal(str(revenue_data['total'])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return {
        "property_id": revenue_data['property_id'],
        "tenant_id": revenue_data.get('tenant_id', tenant_id),
        "total_revenue": str(total),
        "currency": revenue_data['currency'],
        "reservations_count": revenue_data['count'],
        "period": {"year": year, "month": month}
    }
