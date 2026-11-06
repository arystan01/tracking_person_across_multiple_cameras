"""Stateless HTTP shell around the pure Routing package."""

from .api import create_routing_app

__all__ = ["create_routing_app"]
