"""遗物与样本谱系：田野记录、样本账本、跨国交接、研究版本与反查。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .handovers import HandoverService
from .jobs import LineageJobRunner
from .records import (
    ArtifactService,
    ExcavationService,
    FeatureService,
    LocationService,
    RestorationService,
    SiteService,
    StratumService,
    TrenchService,
)
from .research import (
    PublicationPermitService,
    ResearchRequestService,
    ResearchVersionService,
    SensitiveGrantService,
    TestResultService,
)
from .samples import SampleService
from .tracing import TraceService

if TYPE_CHECKING:
    from ..application import CivicFlow


@dataclass(frozen=True)
class Lineage:
    """谱系模块装配：在既有协同事务平台之上组合全部谱系服务。"""

    sites: SiteService
    trenches: TrenchService
    features: FeatureService
    strata: StratumService
    excavations: ExcavationService
    artifacts: ArtifactService
    locations: LocationService
    restorations: RestorationService
    samples: SampleService
    handovers: HandoverService
    grants: SensitiveGrantService
    requests: ResearchRequestService
    results: TestResultService
    versions: ResearchVersionService
    permits: PublicationPermitService
    trace: TraceService
    jobs: LineageJobRunner

    @classmethod
    def open(cls, app: "CivicFlow") -> "Lineage":
        repository = app.repository
        grants = SensitiveGrantService(repository, app.clock)
        artifacts = ArtifactService(repository, grants.allows)
        samples = SampleService(repository, app.database, app.clock, grants.allows)
        handovers = HandoverService(repository, app.database, app.clock, app.jobs, samples, app.outbox)
        return cls(
            sites=SiteService(repository),
            trenches=TrenchService(repository),
            features=FeatureService(repository),
            strata=StratumService(repository),
            excavations=ExcavationService(repository),
            artifacts=artifacts,
            locations=LocationService(repository),
            restorations=RestorationService(repository),
            samples=samples,
            handovers=handovers,
            grants=grants,
            requests=ResearchRequestService(repository, app.database, samples, grants),
            results=TestResultService(repository, app.database, samples),
            versions=ResearchVersionService(repository),
            permits=PublicationPermitService(repository),
            trace=TraceService(repository, artifacts, samples),
            jobs=LineageJobRunner(app.database, app.clock, repository, app.jobs, app.outbox, handovers),
        )


__all__ = [
    "Lineage",
    "HandoverService",
    "LineageJobRunner",
    "SampleService",
    "SensitiveGrantService",
    "TraceService",
]
