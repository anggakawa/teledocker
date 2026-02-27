"""Shared test configuration for container-manager tests.

Sets required environment variables before any module that uses
pydantic-settings gets imported (e.g. routers.py imports config.settings
at module level).
"""

import os

# Must be set before any container_manager module that touches settings.
os.environ.setdefault("SERVICE_TOKEN", "test-token")
