from fastapi import APIRouter
from app.api.response_dto import LiveView


router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=LiveView)
def liveness() -> dict[str, str]:
    return {"status": "ok", "phase": "runtime"}
