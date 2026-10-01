"""跨国（跨机构）交接、包装扫描与冲突隔离。

- 交接单（arch_handovers）与样本明细在创建时固化申报编号与重量；
- 扫描重复提交只确认原交接，不产生新单据；
- 编号或重量与申报不符时立即写入 quarantine_cases 并冻结样本与交接单；
- 发货时为应当返还的交接安排持久化定时任务，服务重启后仍然生效。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

from .database import Database
from .errors import ConflictError, NotFoundError, ValidationError
from .identifiers import new_id
from .jsonutil import canonical_json, digest_json
from .jobs import JobQueue
from .repository import EntityRepository, read_entity
from .samples import SampleBook, SAMPLE_TYPE
from .security import AccessContext
from .timeutil import Clock, canonical_instant


HANDOVER_TYPE = "arch_handovers"
STATES = ("prepared", "in_transit", "received", "returned", "quarantined", "cancelled")
TRANSITIONS = {
    "prepared": {"in_transit", "cancelled", "quarantined"},
    "in_transit": {"received", "quarantined", "cancelled"},
    "received": set(),       # 原物返还另开 kind=return 的交接单
    "returned": set(),
    "quarantined": set(),
    "cancelled": set(),
}
KINDS = ("outbound", "return")


def _decimal(value: object, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{label}格式错误") from exc
    if not number.is_finite() or number <= 0:
        raise ValidationError(f"{label}必须是正有限数")
    return number


@dataclass(frozen=True)
class CustodyService:
    repository: EntityRepository
    samples: SampleBook
    jobs: JobQueue
    database: Database
    clock: Clock

    # ------------------------------------------------------------ 交接单

    def create_handover(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:handovers")
        project_id = _t(values, "project_id", "项目")
        from_org = _t(values, "from_org", "移交方")
        to_org = _t(values, "to_org", "接收方")
        if from_org == to_org:
            raise ValidationError("移交方与接收方不能相同")
        kind = _t(values, "kind", "交接类型")
        if kind not in KINDS:
            raise ValidationError(f"交接类型必须是 {KINDS} 之一")
        package_code = _t(values, "package_code", "包装编号")
        items_input = values.get("items")
        if not isinstance(items_input, list) or not items_input:
            raise ValidationError("交接明细不能为空")
        items = []
        seen = set()
        for raw in items_input:
            if not isinstance(raw, Mapping):
                raise ValidationError("交接明细格式错误")
            sample_id = _t(raw, "sample_id", "样本")
            if sample_id in seen:
                raise ValidationError(f"样本 {sample_id} 在同一包装中重复出现")
            seen.add(sample_id)
            sample = self.repository.get(SAMPLE_TYPE, sample_id)
            if sample["project_id"] != project_id:
                raise ValidationError(f"样本 {sample_id} 不属于项目 {project_id}")
            if sample["state"] == "quarantined":
                raise ConflictError(f"样本 {sample_id} 处于隔离状态，不得加入交接单")
            if self._active_handover(sample_id) is not None:
                raise ConflictError(f"样本 {sample_id} 已存在进行中的交接单，禁止重复流转")
            declared_code = _t(raw, "declared_code", "申报编号")
            declared_weight = _decimal(raw.get("declared_weight"), "申报重量")
            uom = str(raw.get("weight_uom") or sample["uom"]).strip()
            items.append({"sample_id": sample_id, "declared_code": declared_code,
                          "declared_weight": str(declared_weight), "weight_uom": uom,
                          "sample_code": sample["code"]})
        payload = {
            "state": "prepared",
            "project_id": project_id,
            "kind": kind,
            "from_org": from_org,
            "to_org": to_org,
            "package_code": package_code,
            "items": items,
            "customs_ref": _t(values, "customs_ref", "跨国许可/海关凭证"),
            "due_back_at": None,
        }
        if values.get("due_back_at") is not None:
            payload["due_back_at"] = canonical_instant(_t(values, "due_back_at", "应返还时间"))
        return self.repository.create(HANDOVER_TYPE, payload, actor=context.actor_id, request_key=request_key)

    def dispatch(self, context: AccessContext, handover_id: str, *, expected_version: int, request_key: str) -> dict:
        context.require("transition:handovers")
        handover = self._transition(handover_id, "in_transit", expected_version, context.actor_id, request_key, "发货")
        if handover.get("due_back_at"):
            self.jobs.schedule(job_type="return_due", subject_id=handover_id, run_at=handover["due_back_at"],
                               payload={"project_id": handover["project_id"], "package_code": handover["package_code"]})
        return handover

    def receive(self, context: AccessContext, handover_id: str, *, expected_version: int, request_key: str) -> dict:
        """接收方逐件核对后收货：同一事务内把保管责任落到接收机构。"""
        context.require("transition:handovers")
        with self.database.transaction() as connection:
            handover = read_entity(connection, HANDOVER_TYPE, handover_id)
            if handover["state"] != "in_transit":
                raise ConflictError(f"当前状态 {handover['state']} 不能收货")
            if handover["version"] != expected_version:
                raise ConflictError(f"版本冲突，当前为 {handover['version']}")
            target_state = "returned" if handover["kind"] == "return" else "received"
            for item in handover["items"]:
                self.samples.record_custody(
                    connection, sample_id=item["sample_id"], transfer_id=handover_id, actor=context.actor_id,
                    custodian_org=handover["to_org"],
                    note=f"{handover['kind']} 收货，保管责任转至 {handover['to_org']}")
            updated = self.repository.update_within(
                connection, HANDOVER_TYPE, handover_id, {"state": target_state}, actor=context.actor_id,
                expected_version=expected_version, request_key=request_key)
            return updated

    # ------------------------------------------------------------ 扫描核对

    def scan(self, context: AccessContext, handover_id: str, *, package_code: str, sample_id: str,
             observed_code: str, observed_weight: object | None = None) -> dict:
        """扫描一件包装。重复扫描只确认原交接；编号/重量冲突立即隔离。"""
        context.require("scan:handovers")
        handover = self.repository.get(HANDOVER_TYPE, handover_id)
        if handover["state"] in ("quarantined", "cancelled", "returned"):
            raise ConflictError(f"交接单处于 {handover['state']}，不能扫描")
        if package_code.strip() != handover["package_code"]:
            self._quarantine(handover, sample_id, reason="包装编号与交接单不符",
                             expected={"package_code": handover["package_code"]},
                             observed={"package_code": package_code.strip()}, actor=context.actor_id)
            raise ConflictError("包装编号冲突，已立即隔离")
        item = next((each for each in handover["items"] if each["sample_id"] == sample_id), None)
        if item is None:
            raise NotFoundError(f"样本 {sample_id} 不在交接单 {handover_id} 内")
        observed_code = observed_code.strip()
        observed = {"code": observed_code}
        if observed_weight is not None:
            observed["weight"] = str(_decimal(observed_weight, "实测重量"))
            observed["weight_uom"] = item["weight_uom"]
        conflict: dict | None = None
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM sample_scans WHERE transfer_id=? AND package_code=? AND sample_id=?",
                (handover_id, package_code.strip(), sample_id)).fetchone()
            if row:
                # 重复扫描：只要观测一致，就只确认原交接。
                if row["observed_code"] == observed_code and _null(row["observed_weight"]) == observed.get("weight"):
                    return {"status": "duplicate", "confirmed_scan": row["scan_id"],
                            "handover_id": handover_id, "confirmation": json_loads(row["confirmation_json"])}
                reason, expected = "同一包装重复扫描结果不一致", {
                    "scan_id": row["scan_id"], "code": row["observed_code"],
                    "weight": _null(row["observed_weight"])}
            elif observed_code != item["declared_code"] or observed_code != item["sample_code"]:
                reason, expected = "样本编号与申报/田野编号不符", {
                    "declared_code": item["declared_code"], "sample_code": item["sample_code"]}
            elif observed_weight is not None and observed["weight"] != item["declared_weight"]:
                reason, expected = "实测重量与交接单不符", {
                    "declared_weight": item["declared_weight"], "weight_uom": item["weight_uom"]}
            else:
                reason, expected = None, None
            if reason is not None:
                self._quarantine_within(connection, handover, sample_id, reason=reason,
                                        expected=expected, observed=observed, actor=context.actor_id)
                conflict = {"reason": reason}
            else:
                confirmation = {"handover_id": handover_id, "package_code": package_code.strip(),
                                "sample_id": sample_id, "confirmed_code": observed_code,
                                "confirmed_weight": observed.get("weight"),
                                "customs_ref": handover["customs_ref"],
                                "digest": digest_json({"handover_id": handover_id, "sample_id": sample_id,
                                                       "code": observed_code, "weight": observed.get("weight")})}
                scan_id = new_id("scan")
                connection.execute(
                    "INSERT INTO sample_scans(scan_id,transfer_id,package_code,sample_id,observed_code,"
                    "observed_weight,weight_uom,scanned_by,scanned_at,confirmation_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (scan_id, handover_id, package_code.strip(), sample_id, observed_code, observed.get("weight"),
                     item["weight_uom"], context.actor_id, self.clock.now(), canonical_json(confirmation)))
                return {"status": "accepted", "scan_id": scan_id, "handover_id": handover_id,
                        "confirmation": confirmation}
        raise ConflictError(f"{conflict['reason']}，已立即隔离")

    # ------------------------------------------------------------ 隔离

    def list_quarantine(self, *, state: str | None = None) -> list[dict]:
        sql = "SELECT * FROM quarantine_cases"; params: list[object] = []
        if state is not None:
            sql += " WHERE state=?"; params.append(state)
        sql += " ORDER BY raised_at, quarantine_id"
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def resolve_quarantine(self, context: AccessContext, quarantine_id: str, *, resolution: str) -> dict:
        context.require("resolve:quarantine")
        if not resolution.strip():
            raise ValidationError("隔离处置说明不能为空")
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE quarantine_cases SET state='resolved' WHERE quarantine_id=? AND state='open'",
                (quarantine_id,)).rowcount
            if changed != 1:
                raise ConflictError("隔离记录不存在或已处置")
            return {"quarantine_id": quarantine_id, "state": "resolved", "resolution": resolution.strip()}

    def _quarantine(self, handover: Mapping[str, object], sample_id: str, *, reason: str,
                    expected: dict, observed: dict, actor: str) -> None:
        with self.database.transaction() as connection:
            self._quarantine_within(connection, handover, sample_id, reason=reason, expected=expected,
                                    observed=observed, actor=actor)

    def _quarantine_within(self, connection, handover: Mapping[str, object], sample_id: str, *,
                           reason: str, expected: dict, observed: dict, actor: str) -> None:
        quarantine_id = new_id("quarantine")
        connection.execute(
            "INSERT INTO quarantine_cases(quarantine_id,subject_type,subject_id,reason,expected_json,"
            "observed_json,raised_by,raised_at,state) VALUES(?,?,?,?,?,?,?,?,?)",
            (quarantine_id, "sample", sample_id, reason, canonical_json(expected), canonical_json(observed),
             actor, self.clock.now(), "open"))
        self.samples.mark_quarantined(connection, sample_id, actor=actor,
                                      request_key=f"quarantine:{quarantine_id}:sample")
        if handover["state"] not in ("quarantined", "cancelled", "returned"):
            self.repository.update_within(connection, HANDOVER_TYPE, handover["entity_id"],
                                          {"state": "quarantined"}, actor=actor,
                                          expected_version=handover["version"],
                                          request_key=f"quarantine:{quarantine_id}:handover")

    # ------------------------------------------------------------ 内部

    def _active_handover(self, sample_id: str) -> str | None:
        """返回该样本尚在运输/待发货的交接单；已收货或已返还不再阻塞下一段流转。"""
        for handover in self.repository.list(HANDOVER_TYPE, limit=500):
            if handover["state"] in ("prepared", "in_transit", "quarantined") and \
                    any(item["sample_id"] == sample_id for item in handover["items"]):
                return handover["entity_id"]
        return None

    def _transition(self, handover_id: str, target: str, expected_version: int, actor: str,
                    request_key: str, reason: str) -> dict:
        current = self.repository.get(HANDOVER_TYPE, handover_id)
        if target not in TRANSITIONS.get(str(current["state"]), set()):
            raise ConflictError(f"不允许从 {current['state']} 转到 {target}")
        return self.repository.update(HANDOVER_TYPE, handover_id, {"state": target}, actor=actor,
                                      expected_version=expected_version, request_key=request_key)


def _t(values: Mapping[str, object], key: str, label: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}不能为空")
    return value.strip()


def _null(value: object) -> object:
    return None if value is None else str(value)


def json_loads(raw: str) -> dict:
    import json
    return json.loads(raw)
