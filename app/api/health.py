"""Health endpoints.

Two, not one. Liveness answers *should this process be restarted?*; readiness
answers *should this process receive traffic?* One endpoint cannot answer both.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from app.shared.config import get_settings
from app.shared.health import readiness

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness. Deliberately checks nothing external.

    A backend that cannot reach Postgres is not a backend that should be killed
    and restarted — restarting it fixes nothing. That distinction is the entire
    reason this endpoint does no dependency work.
    """
    return {"status": "alive"}


@router.get("/health/ready")
async def health_ready(response: Response) -> dict[str, Any]:
    """Readiness. Reports each dependency individually, so the failing one is named."""
    report = await readiness(get_settings())

    if not report.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "ready": report.ready,
        "dependencies": [
            {
                "name": d.name,
                "status": d.status,
                "detail": d.detail,
                "required": d.required,
            }
            for d in report.dependencies
        ],
    }
