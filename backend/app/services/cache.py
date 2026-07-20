import json
import redis.asyncio as redis
from typing import Dict, Any
import os

# Initialize Redis client (typically configured centrally).
redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))

async def get_revenue_summary(property_id: str, tenant_id: str, year: int, month: int) -> Dict[str, Any]:
    """
    Fetches revenue summary, utilizing caching to improve performance.
    """
    # Key is scoped by tenant and reporting period to prevent cross-tenant collisions.
    cache_key = f"revenue:{tenant_id}:{property_id}:{year}:{month}"

    # Try to get from cache
    cached = await redis_client.get(cache_key)
    if cached:
        payload = json.loads(cached)
        # defense in depth: never return another tenant's cached data
        if payload.get("tenant_id") == tenant_id:
            return payload
        # else fall through and recompute

    # Revenue calculation is delegated to the reservation service.
    from app.services.reservations import calculate_monthly_revenue

    # Calculate revenue
    result = await calculate_monthly_revenue(property_id, tenant_id, month, year)

    # Cache the result for 5 minutes
    await redis_client.setex(cache_key, 300, json.dumps(result))

    return result
