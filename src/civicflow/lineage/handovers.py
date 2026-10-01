"""跨国交接：包装编号幂等扫描、编号或重量冲突隔离、逾期返还。

交接单在 prepare 时把包装编号、接收机构与清单重量固化为摘要；
扫描时重复扫描只确认原交接，编号或重量与交接单冲突则立即隔离交接单
并冻结所涉样本。返还按交接单逐项核对，逾期由持久定时任务标记。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import ClassVar, Iterable, Mapping

from ..database import Database
from ..errors import ConflictError, NotFoundError, ValidationError
from ..jobs import JobQueue
from ..jsonutil import digest_json
from ..outbox import Outbox
from ..repository import EntityRepository
from ..security import AccessContext
from ..timeutil import Clock, canonical_instant
from .samples import SampleService, to_quantity_minor


READ = "read:handovers"
WRITE = "write:handovers"
SCAN = "scan:handovers"

ENTITY_TYPE = "handovers"
QUARANTINE_TYPE = "quarantines"
RETURN_CHECK_JOB = "lineage.return_check"
OVERDUE_TOPIC = "lineage.handover.overdue"

OPEN_STATES = ("prepared", "in_transit", "received", "overdue")


def canon_weight(value: object) -> str:
    """重量的规范字符串，用于交接单摘要与扫描比对。"""
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError("重量格式错误") from exc
    if not number.is_finite():
        raise ValidationError("重量必须是有限数")
    return format(number.normalize(), "f")


def scan_digest(package_no: str, to_org: str, weight: object) -> str:
    return digest_json({"package_no": package_no, "to_org": to_org, "weight": canon_weight(weight)})


def _validate_items(items: Iterable[Mapping[str, object]]) -> list[dict]:
    result = []
    for item in items:
        sample_id = str(item.get("sample_id", "")).strip()
        if not sample_id:
            raise ValidationError("交接清单缺少样本标识")
        result.append({"sample_id": sample_id, "quantity": str(item.get("quantity", "")).strip(), "weight": canon_weight(item.get("weight", "0"))})
    if not result:
        raise ValidationError("交接清单不能为空")
    return result


@dataclass(frozen=True)
class HandoverService:
    """跨国交接单与扫描确认。"""

    _repository: EntityRepository
    _database: Database
    _clock: Clock
    _jobs: JobQueue
    _samples: SampleService
    _outbox: Outbox

    ENTITY_TYPE: ClassVar[str] = ENTITY_TYPE

    @property
    def entity_type(self) -> str:
        return ENTITY_TYPE

    def prepare(self, context: AccessContext, *, package_no: str, from_org: str, to_org: str, responsible: str, items: Iterable[Mapping[str, object]], expected_weight: str, permit_ref: str, due_at: str, request_key: str) -> dict:
        """登记交接单：核对样本在库与数量，冻结为在途，并调度逾期返还检查。"""
        context.require(WRITE)
        lines = _validate_items(items)
        due_at = canonical_instant(due_at)
        expected = canon_weight(expected_weight)
        request = {"package_no": package_no, "items": lines, "expected_weight": expected, "to_org": to_org}
        with self._database.transaction() as connection:
            def operation() -> dict:
                if self._repository.search(ENTITY_TYPE, "package_no", package_no.strip()):
                    raise ConflictError(f"包装编号已存在交接单: {package_no}")
                for line in lines:
                    sample = self._repository.get_in(connection, "samples", line["sample_id"])
                    if sample["state"] != "registered":
                        raise ConflictError(f"样本 {line['sample_id']} 处于 {sample['state']} 状态，不能交接")
                    minor = to_quantity_minor(line["quantity"])
                    if minor > self._samples.custody.available(connection, line["sample_id"]):
                        raise ConflictError(f"样本 {line['sample_id']} 可用数量不足，不能交接")
                payload = {"package_no": package_no.strip(), "from_org": from_org, "to_org": to_org, "responsible": responsible, "items": lines, "expected_weight": expected, "permit_ref": permit_ref, "due_at": due_at, "scan_digest": scan_digest(package_no, to_org, expected), "scans": [], "returned_items": [], "state": "prepared"}
                handover = self._repository.create_in(connection, ENTITY_TYPE, payload, actor=context.actor_id, request_key=request_key)
                for line in lines:
                    sample = self._repository.get_in(connection, "samples", line["sample_id"])
                    self._repository.update_in(connection, "samples", line["sample_id"], {"state": "on_loan"}, actor=context.actor_id, expected_version=sample["version"], request_key=f"{request_key}:loan:{line['sample_id']}")
                job_id = self._jobs.schedule_in(connection, job_type=RETURN_CHECK_JOB, subject_id=handover["entity_id"], run_at=due_at, payload={"handover_id": handover["entity_id"]})
                return {"handover": handover, "return_check_job": job_id}
            return self._repository.idempotency.execute(connection, scope="handovers:prepare", request_key=request_key, request=request, operation=operation)

    def dispatch(self, context: AccessContext, handover_id: str, *, request_key: str) -> dict:
        context.require(WRITE)
        handover = self._repository.get(ENTITY_TYPE, handover_id)
        if handover["state"] != "prepared":
            raise ConflictError(f"交接单处于 {handover['state']} 状态，不能发运")
        return self._repository.update(ENTITY_TYPE, handover_id, {"state": "in_transit"}, actor=context.actor_id, expected_version=handover["version"], request_key=request_key)

    def scan(self, context: AccessContext, *, package_no: str, weight: str, to_org: str, request_key: str) -> dict:
        """扫描确认：重复扫描只确认原交接；编号或重量冲突立即隔离。"""
        context.require(SCAN)
        found = self._repository.search(ENTITY_TYPE, "package_no", package_no.strip())
        if not found:
            raise NotFoundError(f"未知包装编号: {package_no}")
        handover = found[0]
        if handover["state"] == "quarantined":
            raise ConflictError("该交接单已隔离，等待人工处置")
        incoming = scan_digest(package_no, to_org, weight)
        if incoming != handover["scan_digest"]:
            quarantine = self._quarantine(context, handover, scanned_weight=canon_weight(weight), request_key=request_key)
            raise ConflictError(f"编号或重量与交接单冲突，已立即隔离: {quarantine['entity_id']}")
        if handover["state"] in ("received", "returned"):
            return {"status": "duplicate", "handover_id": handover["entity_id"], "confirmed_at": handover["scans"][-1]["scanned_at"] if handover["scans"] else handover["updated_at"]}
        if handover["state"] not in ("prepared", "in_transit", "overdue"):
            raise ConflictError(f"交接单处于 {handover['state']} 状态，不能扫描确认")
        request = {"package_no": package_no, "weight": canon_weight(weight), "to_org": to_org}
        with self._database.transaction() as connection:
            def operation() -> dict:
                current = self._repository.get_in(connection, ENTITY_TYPE, handover["entity_id"])
                scans = list(current.get("scans", []))
                scans.append({"scanned_by": context.actor_id, "scanned_at": self._clock.now(), "weight": canon_weight(weight)})
                updated = self._repository.update_in(connection, ENTITY_TYPE, handover["entity_id"], {"state": "received", "scans": scans}, actor=context.actor_id, expected_version=current["version"], request_key=request_key)
                for line in current["items"]:
                    sample = self._repository.get_in(connection, "samples", line["sample_id"])
                    if sample["state"] == "on_loan":
                        self._repository.update_in(connection, "samples", line["sample_id"], {"state": "registered"}, actor=context.actor_id, expected_version=sample["version"], request_key=f"{request_key}:arrive:{line['sample_id']}")
                return {"status": "received", "handover": updated}
            return self._repository.idempotency.execute(connection, scope="handovers:scan", request_key=request_key, request=request, operation=operation)

    def record_return(self, context: AccessContext, handover_id: str, *, items: Iterable[Mapping[str, object]], request_key: str) -> dict:
        """返还登记：逐项核对交接清单，全部返还后样本回到在库状态。"""
        context.require(WRITE)
        lines = _validate_items(items)
        request = {"handover_id": handover_id, "items": lines}
        with self._database.transaction() as connection:
            def operation() -> dict:
                handover = self._repository.get_in(connection, ENTITY_TYPE, handover_id)
                if handover["state"] not in ("received", "overdue"):
                    raise ConflictError(f"交接单处于 {handover['state']} 状态，不能登记返还")
                handed = {line["sample_id"]: to_quantity_minor(line["quantity"]) for line in handover["items"]}
                returned = {line["sample_id"]: int(line["quantity_minor"]) for line in handover.get("returned_items", [])}
                for line in lines:
                    sample_id = line["sample_id"]
                    if sample_id not in handed:
                        raise ValidationError(f"返还样本不在交接清单内: {sample_id}")
                    returned[sample_id] = returned.get(sample_id, 0) + to_quantity_minor(line["quantity"])
                    if returned[sample_id] > handed[sample_id]:
                        raise ConflictError(f"返还数量超过交接数量: {sample_id}")
                returned_items = [{"sample_id": sample_id, "quantity_minor": minor} for sample_id, minor in sorted(returned.items())]
                complete = all(returned.get(sample_id, 0) >= minor for sample_id, minor in handed.items())
                changes: dict = {"returned_items": returned_items}
                if complete:
                    changes["state"] = "returned"
                updated = self._repository.update_in(connection, ENTITY_TYPE, handover_id, changes, actor=context.actor_id, expected_version=handover["version"], request_key=request_key)
                if complete:
                    fresh = self._repository.get_in(connection, ENTITY_TYPE, handover_id)
                    for line in fresh["items"]:
                        sample = self._repository.get_in(connection, "samples", line["sample_id"])
                        if sample["state"] == "on_loan":
                            self._repository.update_in(connection, "samples", line["sample_id"], {"state": "registered"}, actor=context.actor_id, expected_version=sample["version"], request_key=f"{request_key}:back:{line['sample_id']}")
                return {"handover": updated, "complete": complete}
            return self._repository.idempotency.execute(connection, scope="handovers:return", request_key=request_key, request=request, operation=operation)

    def check_return(self, handover_id: str, *, actor: str) -> dict:
        """逾期返还检查（由持久定时任务调用）：到期未返还标记为逾期并通知。"""
        handover = self._repository.get(ENTITY_TYPE, handover_id)
        if handover["state"] not in ("prepared", "in_transit", "received"):
            return {"handover_id": handover_id, "status": handover["state"], "changed": False}
        if not self._clock.is_due(handover["due_at"]):
            return {"handover_id": handover_id, "status": handover["state"], "changed": False}
        updated = self._repository.update(ENTITY_TYPE, handover_id, {"state": "overdue"}, actor=actor, expected_version=handover["version"], request_key=f"overdue:{handover_id}:{handover['due_at']}")
        self._outbox.enqueue(topic=OVERDUE_TOPIC, aggregate_id=handover_id, payload={"package_no": handover["package_no"], "to_org": handover["to_org"], "responsible": handover["responsible"], "due_at": handover["due_at"]})
        return {"handover_id": handover_id, "status": updated["state"], "changed": True}

    def release_quarantine(self, context: AccessContext, quarantine_id: str, *, reason: str, request_key: str) -> dict:
        """人工解除隔离：交接单回到待发运，样本回到在库。"""
        context.require(WRITE)
        if not reason.strip():
            raise ValidationError("解除隔离必须说明原因")
        with self._database.transaction() as connection:
            def operation() -> dict:
                quarantine = self._repository.get_in(connection, QUARANTINE_TYPE, quarantine_id)
                if quarantine["state"] != "isolated":
                    raise ConflictError("隔离记录已处置")
                handover = self._repository.get_in(connection, ENTITY_TYPE, quarantine["handover_id"])
                self._repository.update_in(connection, ENTITY_TYPE, handover["entity_id"], {"state": "prepared"}, actor=context.actor_id, expected_version=handover["version"], request_key=f"{request_key}:handover")
                for line in handover["items"]:
                    sample = self._repository.get_in(connection, "samples", line["sample_id"])
                    if sample["state"] == "quarantined":
                        self._repository.update_in(connection, "samples", line["sample_id"], {"state": "registered"}, actor=context.actor_id, expected_version=sample["version"], request_key=f"{request_key}:sample:{line['sample_id']}")
                resolved = self._repository.update_in(connection, QUARANTINE_TYPE, quarantine_id, {"state": "resolved", "resolution": reason.strip()}, actor=context.actor_id, expected_version=quarantine["version"], request_key=request_key)
                return {"quarantine": resolved}
            return self._repository.idempotency.execute(connection, scope="handovers:release_quarantine", request_key=request_key, request={"quarantine_id": quarantine_id, "reason": reason}, operation=operation)

    def get(self, context: AccessContext, entity_id: str) -> dict:
        context.require(READ)
        return self._repository.get(ENTITY_TYPE, entity_id)

    def list_current(self, context: AccessContext, *, state: str | None = None, limit: int = 100) -> list[dict]:
        context.require(READ)
        return self._repository.list(ENTITY_TYPE, state=state, limit=limit)

    def history(self, context: AccessContext, entity_id: str) -> list[dict]:
        context.require("history:lineage")
        rows = self._repository.history(ENTITY_TYPE, entity_id)
        if not rows:
            raise NotFoundError(f"{ENTITY_TYPE}/{entity_id} 不存在")
        return rows

    def _quarantine(self, context: AccessContext, handover: dict, *, scanned_weight: str, request_key: str) -> dict:
        """隔离交接单并冻结所涉样本；隔离写入独立提交后再向调用方报错。"""
        request = {"handover_id": handover["entity_id"], "scanned_weight": scanned_weight}
        with self._database.transaction() as connection:
            def operation() -> dict:
                payload = {"handover_id": handover["entity_id"], "package_no": handover["package_no"], "reason": "编号或重量与交接单冲突", "expected_weight": handover["expected_weight"], "scanned_weight": scanned_weight, "state": "isolated"}
                quarantine = self._repository.create_in(connection, QUARANTINE_TYPE, payload, actor=context.actor_id, request_key=request_key)
                current = self._repository.get_in(connection, ENTITY_TYPE, handover["entity_id"])
                self._repository.update_in(connection, ENTITY_TYPE, handover["entity_id"], {"state": "quarantined"}, actor=context.actor_id, expected_version=current["version"], request_key=f"{request_key}:handover")
                for line in current["items"]:
                    sample = self._repository.get_in(connection, "samples", line["sample_id"])
                    if sample["state"] != "quarantined":
                        self._repository.update_in(connection, "samples", line["sample_id"], {"state": "quarantined"}, actor=context.actor_id, expected_version=sample["version"], request_key=f"{request_key}:sample:{line['sample_id']}")
                return quarantine
            return self._repository.idempotency.execute(connection, scope="handovers:quarantine", request_key=request_key, request=request, operation=operation)
