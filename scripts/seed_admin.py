"""Seed script to upsert admin users from ADMIN_TELEGRAM_IDS environment variable.

Run once after database migrations:
  uv run python scripts/seed_admin.py

This is also called by setup.sh automatically.
"""

import asyncio
import os
import sys

# Ensure the api_server package is importable when running from the scripts dir.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../services/api-server/src"))

from api_server.config import settings
from api_server.db.engine import get_db_session, initialize_engine
from api_server.services.user_service import get_user_model, register_user


async def seed_admins() -> None:
    initialize_engine(settings.database_url)

    if not settings.admin_telegram_ids:
        print("No ADMIN_TELEGRAM_IDS configured — nothing to seed.")
        return

    async with get_db_session() as db:
        for telegram_id in settings.admin_telegram_ids:
            existing = await get_user_model(telegram_id, db)
            if existing is not None:
                if existing.role != "admin":
                    existing.role = "admin"
                    existing.is_approved = True
                    await db.commit()
                    print(f"Updated existing user {telegram_id} to admin.")
                else:
                    print(f"User {telegram_id} is already admin — skipping.")
            else:
                user = await register_user(
                    telegram_id=telegram_id,
                    username=None,
                    display_name=f"Admin {telegram_id}",
                    settings=settings,
                    db=db,
                )
                print(f"Created admin user: {telegram_id}")


if __name__ == "__main__":
    asyncio.run(seed_admins())
