"""Compatibility entry point for the Hearo final v2 FastAPI application.

Production may run either ``dashboard_api_v2:app`` (the historic deployment
command) or ``hearo_backend.main:app`` (the package-native command). Both
resolve to the same application object.
"""

from hearo_backend.main import app

__all__ = ["app"]
