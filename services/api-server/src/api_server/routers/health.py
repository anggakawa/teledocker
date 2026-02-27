"""Simple health check endpoint for liveness probes."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict:
    return {"status": "ok", "service": "api-server"}
