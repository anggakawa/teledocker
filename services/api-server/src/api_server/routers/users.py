"""User management endpoints.

All endpoints require a valid X-Service-Token header (internal service auth).
Admin operations (approve, reject, revoke, list) require the caller to verify
the requesting user is an admin before calling — the bot handles that logic.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from chatops_shared.schemas.user import RegisterRequest, SetApiKeyRequest, UserDTO

from api_server.config import settings
from api_server.db.engine import get_db
from api_server.dependencies import get_redis, verify_service_token
from api_server.services import user_service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/users",
    tags=["users"],
    dependencies=[Depends(verify_service_token)],
)


@router.post("/register", response_model=UserDTO, status_code=status.HTTP_201_CREATED)
async def register_user(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> UserDTO:
    """Register a new user as a guest and publish an admin notification."""
    user = await user_service.register_user(
        telegram_id=payload.telegram_id,
        username=payload.telegram_username,
        display_name=payload.display_name,
        settings=settings,
        db=db,
    )

    # Notify admin channel if this is a new guest (not an auto-approved admin).
    if not user.is_approved:
        notification = {
            "event": "new_user_registration",
            "telegram_id": user.telegram_id,
            "display_name": user.display_name,
            "telegram_username": user.telegram_username,
        }
        await redis.publish("admin:notifications", json.dumps(notification))

    return user


@router.get("/{telegram_id}", response_model=UserDTO)
async def get_user(
    telegram_id: int,
    db: AsyncSession = Depends(get_db),
) -> UserDTO:
    user = await user_service.get_user(telegram_id, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.post("/{telegram_id}/approve", response_model=UserDTO)
async def approve_user(
    telegram_id: int,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> UserDTO:
    try:
        user = await user_service.approve_user(telegram_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    # Notify the bot to send the approval message to the user.
    notification = {
        "event": "user_approved",
        "telegram_id": user.telegram_id,
    }
    await redis.publish("admin:notifications", json.dumps(notification))
    return user


@router.post("/{telegram_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
async def reject_user(
    telegram_id: int,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> None:
    try:
        await user_service.reject_user(telegram_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    notification = {"event": "user_rejected", "telegram_id": telegram_id}
    await redis.publish("admin:notifications", json.dumps(notification))


@router.post("/{telegram_id}/revoke", response_model=UserDTO)
async def revoke_user(
    telegram_id: int,
    db: AsyncSession = Depends(get_db),
) -> UserDTO:
    try:
        return await user_service.revoke_user(telegram_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get("", response_model=list[UserDTO])
async def list_users(
    status_filter: str | None = Query(default=None, alias="status"),
    db: AsyncSession = Depends(get_db),
) -> list[UserDTO]:
    return await user_service.list_users(status_filter, db)


@router.put("/{telegram_id}/apikey", status_code=status.HTTP_204_NO_CONTENT)
async def set_api_key(
    telegram_id: int,
    payload: SetApiKeyRequest,
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await user_service.set_api_key(
            telegram_id=telegram_id,
            api_key_plaintext=payload.api_key,
            provider=payload.provider,
            base_url=payload.base_url,
            settings=settings,
            db=db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.delete("/{telegram_id}/apikey", status_code=status.HTTP_204_NO_CONTENT)
async def remove_api_key(
    telegram_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await user_service.remove_api_key(telegram_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
