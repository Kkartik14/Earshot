"""Optional Platform presentation adapter for the Earshot API."""

from .adapter import (
    PLATFORM_SUMMARY_DEPRECATION,
    PLATFORM_SUMMARY_DEPRECATION_LINK,
    PlatformSummaryDeprecationMiddleware,
    create_platform_router,
)

__all__ = [
    "PLATFORM_SUMMARY_DEPRECATION",
    "PLATFORM_SUMMARY_DEPRECATION_LINK",
    "PlatformSummaryDeprecationMiddleware",
    "create_platform_router",
]
