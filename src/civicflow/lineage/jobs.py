"""谱系持久定时任务：逾期返还检查与保存条件检查。

任务持久化在 scheduled_jobs 表中，服务重启后由执行器继续认领，
不会因进程退出而丢失。保存条件检查按间隔自动续期。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from ..database import Database
from ..errors import CivicFlowError, ValidationError
from ..jobs import JobQueue
from ..outbox import Outbox
from ..repository import EntityRepository
from ..security import AccessContext
from ..timeutil import Clock, parse_instant
from .handovers import RETURN_CHECK_JOB, HandoverService


RUN = "run:jobs"
CONDITION_CHECK_JOB = "lineage.condition_check"
STORAGE_ALERT_TOPIC = "lineage.storage.alert"
INSPECTIONS = "inspections"
RETRY_SECONDS = 300


@dataclass(frozen=True)
class LineageJobRunner:
    """谱系定时任务执行器：认领到期任务、执行、按结果完成或重试。"""

    _database: Database
    _clock: Clock
    _repository: EntityRepository
    _jobs: JobQueue
    _outbox: Outbox
    _handovers: HandoverService

    def schedule_condition_check(self, context: AccessContext, *, location_id: str, interval_hours: int, required: dict, start_at: str | None = None) -> str:
        """登记保存条件周期检查；任务持久化，重启后继续执行。"""
        context.require("write:lineage")
        if interval_hours < 1:
            raise ValidationError("检查间隔至少为一小时")
        self._repository.get("locations", location_id)
        run_at = start_at or (parse_instant(self._clock.now()) + timedelta(hours=interval_hours)).isoformat().replace("+00:00", "Z")
        payload = {"location_id": location_id, "interval_hours": interval_hours, "required": required}
        return self._jobs.schedule(job_type=CONDITION_CHECK_JOB, subject_id=location_id, run_at=run_at, payload=payload)

    def run_due(self, context: AccessContext, *, limit: int = 20) -> list[dict]:
        """认领并执行到期任务；失败按指数退避重试，重启后租约到期可被重新认领。"""
        context.require(RUN)
        claimed = self._jobs.claim_due(limit=limit)
        results = []
        for job in claimed:
            job_id = job["job_id"]
            try:
                detail = self._dispatch(job, context)
            except CivicFlowError as exc:
                retry_at = (parse_instant(self._clock.now()) + timedelta(seconds=RETRY_SECONDS)).isoformat().replace("+00:00", "Z")
                self._jobs.retry(job_id, error=str(exc), retry_at=retry_at)
                results.append({"job_id": job_id, "job_type": job["job_type"], "status": "retry", "error": str(exc)})
                continue
            self._jobs.finish(job_id)
            results.append({"job_id": job_id, "job_type": job["job_type"], "status": "succeeded", "detail": detail})
        return results

    def _dispatch(self, job: dict, context: AccessContext) -> dict:
        payload = json.loads(job["payload_json"]) if isinstance(job.get("payload_json"), str) else dict(job.get("payload_json") or {})
        if job["job_type"] == RETURN_CHECK_JOB:
            return self._handovers.check_return(str(payload["handover_id"]), actor=context.actor_id)
        if job["job_type"] == CONDITION_CHECK_JOB:
            return self._run_condition_check(payload, context, job_id=job["job_id"])
        raise ValidationError(f"未知任务类型: {job['job_type']}")

    def _run_condition_check(self, payload: dict, context: AccessContext, *, job_id: str) -> dict:
        location_id = str(payload["location_id"])
        interval_hours = int(payload["interval_hours"])
        required = dict(payload.get("required") or {})
        location = self._repository.get("locations", location_id)
        observed = dict(location.get("conditions") or {})
        breaches = []
        for key, bounds in required.items():
            low, high = bounds
            if key not in observed or not (low <= observed[key] <= high):
                breaches.append(key)
        result = "alert" if breaches else "ok"
        inspection = self._repository.create(INSPECTIONS, {"location_id": location_id, "observed_at": self._clock.now(), "result": result, "breaches": breaches, "observed": observed, "state": "recorded"}, actor=context.actor_id, request_key=f"inspection:{job_id}")
        if breaches:
            self._outbox.enqueue(topic=STORAGE_ALERT_TOPIC, aggregate_id=location_id, payload={"location_id": location_id, "breaches": breaches, "observed": observed})
        next_run = (parse_instant(self._clock.now()) + timedelta(hours=interval_hours)).isoformat().replace("+00:00", "Z")
        next_job = self._jobs.schedule(job_type=CONDITION_CHECK_JOB, subject_id=location_id, run_at=next_run, payload={"location_id": location_id, "interval_hours": interval_hours, "required": required})
        return {"location_id": location_id, "result": result, "breaches": breaches, "inspection_id": inspection["entity_id"], "next_run_at": next_run, "next_job_id": next_job}
