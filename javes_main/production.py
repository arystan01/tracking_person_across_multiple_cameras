from __future__ import annotations

from dataclasses import dataclass

from javes_site_config import HttpSiteConfigClient

from .adapters.artifacts import FileArtifactStore
from .adapters.http_routing import HttpRoutingAdapter
from .adapters.remote_only import RemoteWorkerOnlyPort
from .adapters.system import ContentIdGenerator, SystemClock
from .application import AuthService, CaseManager
from .persistence.sqlalchemy import SqlAlchemyRepository
from .scheduler import RoutingTriggerDispatcher
from .settings import MainSettings


@dataclass
class ApiFirstSystem:
    manager: CaseManager
    auth: AuthService
    routing_dispatcher: RoutingTriggerDispatcher


def build_api_first_system(settings: MainSettings) -> ApiFirstSystem:
    required = {
        "routing_url": settings.routing_url,
        "routing_token": settings.routing_token,
        "site_config_url": settings.site_config_url,
        "site_config_token": settings.site_config_token,
        "worker_lease_secret": settings.worker_lease_secret,
        "perception_worker_token": settings.perception_worker_token,
        "clip_worker_token": settings.clip_worker_token,
        "retention_worker_token": settings.retention_worker_token,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ValueError(
            "API-first production configuration is incomplete: " + ", ".join(missing)
        )
    repository = SqlAlchemyRepository.from_url(settings.database_url)
    artifacts = FileArtifactStore(settings.artifact_root)
    clock = SystemClock()
    ids = ContentIdGenerator()
    sites = HttpSiteConfigClient(
        settings.site_config_url or "",
        settings.site_config_token.get_secret_value(),  # type: ignore[union-attr]
        default_params_version=settings.routing_params_version,
    )
    routing = HttpRoutingAdapter(
        settings.routing_url or "",
        settings.routing_token.get_secret_value(),  # type: ignore[union-attr]
    )
    manager = CaseManager(
        repository=repository,
        artifacts=artifacts,
        sites=sites,
        routing=routing,
        perception=RemoteWorkerOnlyPort("perception"),
        clip=RemoteWorkerOnlyPort("clip"),
        retention=RemoteWorkerOnlyPort("retention"),
        live_feed=artifacts,
        clock=clock,
        ids=ids,
    )
    return ApiFirstSystem(
        manager=manager,
        auth=AuthService(repository=repository, clock=clock, ids=ids),
        routing_dispatcher=RoutingTriggerDispatcher(
            repository=repository,
            manager=manager,
            clock=clock,
            worker_id="main-routing-http",
        ),
    )


def worker_tokens(settings: MainSettings) -> dict[str, str]:
    return {
        "perception": settings.perception_worker_token.get_secret_value(),  # type: ignore[union-attr]
        "clip": settings.clip_worker_token.get_secret_value(),  # type: ignore[union-attr]
        "retention": settings.retention_worker_token.get_secret_value(),  # type: ignore[union-attr]
    }
