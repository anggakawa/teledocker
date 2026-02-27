"""Service token authentication dependency.

Re-exported here for clarity; the actual implementation lives in dependencies.py.
"""

from api_server.dependencies import verify_service_token

__all__ = ["verify_service_token"]
