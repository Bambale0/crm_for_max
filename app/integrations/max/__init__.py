"""Read-only MAX diagnostics for the integration discovery stage."""

from app.integrations.max.client import MaxAPIError, MaxReadOnlyClient

__all__ = ["MaxAPIError", "MaxReadOnlyClient"]
