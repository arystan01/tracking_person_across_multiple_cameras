from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from javes_contracts import FakeObservation, SiteGraph
from javes_fakes import (
    ArchiveWindow,
    FakeClipWorker,
    FakePerceptionWorker,
    FakeRetentionWorker,
    FakeVideoArchive,
)
from routing.contracts.params import RoutingParams

from .adapters import (
    FakeClipAdapter,
    FakePerceptionAdapter,
    FakeRetentionAdapter,
    InMemoryArtifactStore,
    InMemoryLiveFeedStore,
    InMemorySiteConfig,
    LocalRoutingAdapter,
)
from .adapters.system import ContentIdGenerator, FixedClock, SystemClock
from .application import AuthService, CaseManager, DEFAULT_RETENTION_TTL_S
from .domain import UserAccount
from .ports import (
    ArtifactStore,
    CaseRepository,
    ClipPort,
    Clock,
    IdGenerator,
    LiveFeedPort,
    PerceptionPort,
    RetentionPort,
    SiteConfigPort,
)
from .persistence import InMemoryRepository
from .scheduler import OutboxDispatcher, RoutingTriggerDispatcher, SchedulerRunner


@dataclass
class ApplicationSystem:
    manager: CaseManager
    repository: CaseRepository
    artifacts: ArtifactStore
    live_feed: LiveFeedPort
    sites: SiteConfigPort
    clock: Clock
    auth: AuthService
    outbox_dispatcher: OutboxDispatcher
    routing_dispatcher: RoutingTriggerDispatcher
    scheduler: SchedulerRunner


def assemble_system(
    *,
    repository: CaseRepository,
    artifacts: ArtifactStore,
    live_feed: LiveFeedPort,
    sites: SiteConfigPort,
    perception: PerceptionPort,
    clip: ClipPort,
    retention: RetentionPort,
    clock: Clock,
    ids: IdGenerator | None = None,
    retention_ttl_s: float = DEFAULT_RETENTION_TTL_S,
) -> ApplicationSystem:
    ids = ids or ContentIdGenerator()
    manager = CaseManager(
        repository=repository,
        artifacts=artifacts,
        sites=sites,
        routing=LocalRoutingAdapter(),
        perception=perception,
        clip=clip,
        retention=retention,
        live_feed=live_feed,
        clock=clock,
        ids=ids,
        retention_ttl_s=retention_ttl_s,
    )
    auth = AuthService(repository=repository, clock=clock, ids=ids)
    outbox_dispatcher = OutboxDispatcher(
        repository=repository,
        manager=manager,
        perception=perception,
        clip=clip,
        retention=retention,
        clock=clock,
    )
    routing_dispatcher = RoutingTriggerDispatcher(
        repository=repository,
        manager=manager,
        clock=clock,
    )
    return ApplicationSystem(
        manager=manager,
        repository=repository,
        artifacts=artifacts,
        live_feed=live_feed,
        sites=sites,
        clock=clock,
        auth=auth,
        outbox_dispatcher=outbox_dispatcher,
        routing_dispatcher=routing_dispatcher,
        scheduler=SchedulerRunner(manager, outbox_dispatcher, routing_dispatcher),
    )


@dataclass
class InMemorySystem:
    manager: CaseManager
    repository: InMemoryRepository
    artifacts: InMemoryArtifactStore
    live_feed_store: InMemoryLiveFeedStore
    sites: InMemorySiteConfig
    perception_worker: FakePerceptionWorker
    clip_worker: FakeClipWorker
    retention_worker: FakeRetentionWorker
    video_archive_worker: FakeVideoArchive
    clock: FixedClock | SystemClock
    auth: AuthService
    root_user: UserAccount
    outbox_dispatcher: OutboxDispatcher
    routing_dispatcher: RoutingTriggerDispatcher
    scheduler: SchedulerRunner


def build_in_memory_system(
    *,
    graph: SiteGraph,
    params: RoutingParams,
    observations: list[FakeObservation] | None = None,
    now: datetime | None = None,
    floor_assets: dict[str, bytes] | None = None,
    unavailable_cameras: set[str] | None = None,
    archive_windows: list[ArchiveWindow] | None = None,
    root_username: str = "root",
    root_password: str = "root-password-for-tests",
) -> InMemorySystem:
    repository = InMemoryRepository()
    artifacts = InMemoryArtifactStore()
    live_feed_store = InMemoryLiveFeedStore()
    sites = InMemorySiteConfig(params)
    sites.add(graph, floor_assets=floor_assets)
    perception_worker = FakePerceptionWorker(
        observations,
        unavailable_cameras=unavailable_cameras,
    )
    clip_worker = FakeClipWorker()
    retention_worker = FakeRetentionWorker()
    video_archive_worker = FakeVideoArchive(archive_windows)
    clock: FixedClock | SystemClock = FixedClock(now) if now is not None else SystemClock()
    perception = FakePerceptionAdapter(perception_worker)
    clip = FakeClipAdapter(clip_worker)
    retention = FakeRetentionAdapter(retention_worker)
    assembled = assemble_system(
        repository=repository,
        artifacts=artifacts,
        live_feed=live_feed_store,
        sites=sites,
        perception=perception,
        clip=clip,
        retention=retention,
        clock=clock,
    )
    manager = assembled.manager
    auth = assembled.auth
    root_user = auth.bootstrap_root(root_username, root_password)
    outbox_dispatcher = assembled.outbox_dispatcher
    routing_dispatcher = assembled.routing_dispatcher
    scheduler = assembled.scheduler
    return InMemorySystem(
        manager=manager,
        repository=repository,
        artifacts=artifacts,
        live_feed_store=live_feed_store,
        sites=sites,
        perception_worker=perception_worker,
        clip_worker=clip_worker,
        retention_worker=retention_worker,
        video_archive_worker=video_archive_worker,
        clock=clock,
        auth=auth,
        root_user=root_user,
        outbox_dispatcher=outbox_dispatcher,
        routing_dispatcher=routing_dispatcher,
        scheduler=scheduler,
    )
