"""Business logic for user registration, approval, and API key management."""

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chatops_shared.encryption import decrypt_api_key, encrypt_api_key
from chatops_shared.schemas.user import UserDTO

from api_server.config import ApiServerSettings
from api_server.db.models import User

logger = logging.getLogger(__name__)


async def register_user(
    telegram_id: int,
    username: str | None,
    display_name: str,
    settings: ApiServerSettings,
    db: AsyncSession,
) -> UserDTO:
    """Register a new user as a guest.

    If the telegram_id is in ADMIN_TELEGRAM_IDS, the user is automatically
    promoted to admin with is_approved=True (bootstrap flow).

    Returns the created user as a DTO. Raises ValueError if already registered.
    """
    existing = await get_user(telegram_id, db)
    if existing is not None:
        return existing

    is_admin_bootstrap = telegram_id in settings.admin_telegram_ids
    role = "admin" if is_admin_bootstrap else "guest"
    is_approved = is_admin_bootstrap

    user = User(
        telegram_id=telegram_id,
        telegram_username=username,
        display_name=display_name,
        role=role,
        is_approved=is_approved,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info("Registered user telegram_id=%s role=%s", telegram_id, role)
    return UserDTO.model_validate(user)


async def get_user(telegram_id: int, db: AsyncSession) -> UserDTO | None:
    """Fetch a user by Telegram ID. Returns None if not found."""
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        return None
    return UserDTO.model_validate(user)


async def get_user_model(telegram_id: int, db: AsyncSession) -> User | None:
    """Fetch the raw SQLAlchemy User model (needed for encrypted field access)."""
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def approve_user(telegram_id: int, db: AsyncSession) -> UserDTO:
    """Promote a guest user to approved user role."""
    user = await get_user_model(telegram_id, db)
    if user is None:
        raise ValueError(f"User {telegram_id} not found")

    user.role = "user"
    user.is_approved = True
    await db.commit()
    await db.refresh(user)
    logger.info("Approved user telegram_id=%s", telegram_id)
    return UserDTO.model_validate(user)


async def reject_user(telegram_id: int, db: AsyncSession) -> None:
    """Delete a guest user who has been rejected."""
    user = await get_user_model(telegram_id, db)
    if user is None:
        raise ValueError(f"User {telegram_id} not found")

    await db.delete(user)
    await db.commit()
    logger.info("Rejected and deleted user telegram_id=%s", telegram_id)


async def revoke_user(telegram_id: int, db: AsyncSession) -> UserDTO:
    """Revoke an approved user's access, demoting them back to guest."""
    user = await get_user_model(telegram_id, db)
    if user is None:
        raise ValueError(f"User {telegram_id} not found")

    user.role = "guest"
    user.is_approved = False
    await db.commit()
    await db.refresh(user)
    logger.info("Revoked user telegram_id=%s", telegram_id)
    return UserDTO.model_validate(user)


async def list_users(status_filter: str | None, db: AsyncSession) -> list[UserDTO]:
    """List users optionally filtered by approval status.

    status_filter values: 'pending' (not approved), 'approved', 'all'.
    """
    query = select(User)

    if status_filter == "pending":
        query = query.where(User.is_approved == False)  # noqa: E712
    elif status_filter == "approved":
        query = query.where(User.is_approved == True)  # noqa: E712

    result = await db.execute(query)
    users = result.scalars().all()
    return [UserDTO.model_validate(u) for u in users]


async def set_api_key(
    telegram_id: int,
    api_key_plaintext: str,
    provider: str,
    base_url: str | None,
    settings: ApiServerSettings,
    db: AsyncSession,
) -> None:
    """Encrypt and store the user's API key and provider configuration."""
    user = await get_user_model(telegram_id, db)
    if user is None:
        raise ValueError(f"User {telegram_id} not found")

    ciphertext, iv = encrypt_api_key(api_key_plaintext, settings.encryption_key)
    user.api_key_encrypted = ciphertext
    user.api_key_iv = iv
    user.provider_config = {"provider": provider, "base_url": base_url}
    await db.commit()
    logger.info("Stored encrypted API key for telegram_id=%s provider=%s", telegram_id, provider)


async def remove_api_key(telegram_id: int, db: AsyncSession) -> None:
    """Remove the stored API key and provider configuration for a user."""
    user = await get_user_model(telegram_id, db)
    if user is None:
        raise ValueError(f"User {telegram_id} not found")

    user.api_key_encrypted = None
    user.api_key_iv = None
    user.provider_config = None
    await db.commit()
    logger.info("Removed API key for telegram_id=%s", telegram_id)


def get_decrypted_api_key(user: User, settings: ApiServerSettings) -> str | None:
    """Decrypt the user's stored API key. Returns None if no key is stored."""
    if user.api_key_encrypted is None or user.api_key_iv is None:
        return None
    return decrypt_api_key(user.api_key_encrypted, user.api_key_iv, settings.encryption_key)
