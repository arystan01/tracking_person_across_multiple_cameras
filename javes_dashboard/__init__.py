"""React Dashboard service shell and same-origin Controls BFF."""

from .app import create_dashboard_app
from .settings import DashboardSettings

__all__ = ["DashboardSettings", "create_dashboard_app"]
