"""应用装配。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .archaeology import FieldService
from .audit import AuditLog
from .custody import CustodyService
from .database import Database
from .idempotency import IdempotencyStore
from .inbox import Inbox
from .jobs import JobQueue
from .ledger import Ledger
from .monitoring import MonitoringService
from .outbox import Outbox
from .provenance import ProvenanceService
from .repository import EntityRepository
from .research import ResearchService
from .reservations import ReservationBook
from .samples import SampleBook
from .timeutil import Clock


@dataclass(frozen=True)
class CivicFlow:
    database: Database
    clock: Clock
    repository: EntityRepository
    inbox: Inbox
    outbox: Outbox
    ledger: Ledger
    reservations: ReservationBook
    jobs: JobQueue
    field: FieldService
    samples: SampleBook
    custody: CustodyService
    research: ResearchService
    monitoring: MonitoringService
    provenance: ProvenanceService

    @classmethod
    def open(cls, path: str | Path, *, fixed_now: str | None = None) -> "CivicFlow":
        database = Database(path); database.initialize(); clock = Clock(fixed_now)
        audit = AuditLog(clock); idempotency = IdempotencyStore(clock)
        repository = EntityRepository(database, clock, audit, idempotency)
        inbox = Inbox(database, clock)
        outbox = Outbox(database, clock)
        ledger = Ledger(database, clock)
        reservations = ReservationBook(database)
        jobs = JobQueue(database, clock)
        field = FieldService(repository)
        sample_book = SampleBook(repository, database, clock)
        custody = CustodyService(repository, sample_book, jobs, database, clock)
        research = ResearchService(repository, database)
        monitoring = MonitoringService(repository, database, clock, jobs)
        provenance = ProvenanceService(repository, database, clock)
        return cls(database, clock, repository, inbox, outbox, ledger, reservations, jobs,
                   field, sample_book, custody, research, monitoring, provenance)

    def verify(self) -> dict:
        with self.database.connect() as connection:
            audit_count = AuditLog(self.clock).verify(connection)
            entity_count = connection.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
            conflict_count = connection.execute("SELECT COUNT(*) AS n FROM inbox_conflicts").fetchone()["n"]
            quarantine_count = connection.execute("SELECT COUNT(*) AS n FROM quarantine_cases WHERE state='open'").fetchone()["n"]
        lineage = self.samples.verify_lineage()
        return {"audit_entries": audit_count, "entities": entity_count, "inbox_conflicts": conflict_count,
                "open_quarantine": quarantine_count, "lineage": lineage}
