"""样本保管账本：母样与子样的分装、预留、消耗与返还。

数量以整数最小单位（千分之一）记录在 sample_movements 流水中，
held（实有）与 available（可用）由流水推导，任何分装、取样、消耗、返还
都在同一事务内核对父子关系与可用数量，防止同一母样被重复消耗。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..database import Database
from ..errors import ConflictError, NotFoundError, ValidationError
from ..identifiers import new_id, require_safe
from ..jsonutil import canonical_json, digest_json
from ..ledger import to_minor
from ..repository import EntityRepository
from ..security import AccessContext
from ..timeutil import Clock
from .records import SensitiveAuthorizer, _deny_sensitive


READ = "read:samples"
WRITE = "write:samples"

ENTITY_TYPE = "samples"
EXPONENT = 3

SIGNS = {"register": 1, "split_in": 1, "release": 1, "split_out": -1, "reserve": -1, "consume": -1}
FROZEN_STATES = ("quarantined", "consumed", "on_loan")


def to_quantity_minor(value: object) -> int:
    """把数量字符串转换为整数最小单位。"""
    minor = to_minor(str(value), EXPONENT)
    if minor <= 0:
        raise ValidationError("数量必须大于零")
    return minor


@dataclass(frozen=True)
class Custody:
    """样本数量流水：所有变动先查幂等键再追加，重放安全。"""

    database: Database
    clock: Clock

    def post(self, connection, *, kind: str, sample_id: str, counterpart_id: str, quantity_minor: int, unit: str, reference: str, request_key: str, actor: str) -> dict:
        if kind not in SIGNS:
            raise ValidationError(f"未知流水类型: {kind}")
        if quantity_minor <= 0:
            raise ValidationError("流水数量必须大于零")
        request = {"sample_id": sample_id, "counterpart_id": counterpart_id, "quantity_minor": quantity_minor, "unit": unit, "reference": reference}
        digest = digest_json(request)
        scope = f"movement:{kind}"
        row = connection.execute("SELECT * FROM sample_movements WHERE scope=? AND request_key=?", (scope, request_key)).fetchone()
        if row:
            if row["request_digest"] != digest:
                raise ConflictError("相同请求标识对应不同流水内容")
            return dict(row)
        movement_id = new_id("movement")
        connection.execute("INSERT INTO sample_movements(movement_id,scope,request_key,request_digest,sample_id,counterpart_id,kind,quantity_minor,unit,reference,occurred_at,actor_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (movement_id, scope, request_key, digest, sample_id, counterpart_id, kind, quantity_minor, unit, reference, self.clock.now(), actor))
        return dict(connection.execute("SELECT * FROM sample_movements WHERE movement_id=?", (movement_id,)).fetchone())

    def _sum(self, connection, sample_id: str, kinds: tuple[str, ...]) -> int:
        marks = ",".join("?" for _ in kinds)
        row = connection.execute(f"SELECT COALESCE(SUM(quantity_minor),0) AS total FROM sample_movements WHERE sample_id=? AND kind IN ({marks})", (sample_id, *kinds)).fetchone()
        return int(row["total"])

    def held(self, connection, sample_id: str) -> int:
        """实有数量：登记与分入减去分出与消耗。"""
        return self._sum(connection, sample_id, ("register", "split_in")) - self._sum(connection, sample_id, ("split_out", "consume"))

    def available(self, connection, sample_id: str) -> int:
        """可用数量：实有减去尚未释放的研究预留。"""
        total = 0
        for kind, sign in SIGNS.items():
            total += sign * self._sum(connection, sample_id, (kind,))
        return total

    def reserved(self, connection, sample_id: str, *, reference: str | None = None) -> int:
        sql = "SELECT COALESCE(SUM(CASE kind WHEN 'reserve' THEN quantity_minor ELSE -quantity_minor END),0) AS total FROM sample_movements WHERE sample_id=? AND kind IN ('reserve','release')"
        params: list[object] = [sample_id]
        if reference is not None:
            sql += " AND reference=?"
            params.append(reference)
        row = connection.execute(sql, params).fetchone()
        return int(row["total"])

    def movements_of(self, sample_id: str) -> list[dict]:
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM sample_movements WHERE sample_id=? ORDER BY rowid", (sample_id,))]


@dataclass(frozen=True)
class SampleService:
    """样本登记、分装与数量核对。"""

    _repository: EntityRepository
    _database: Database
    _clock: Clock
    _authorize: SensitiveAuthorizer = _deny_sensitive

    ENTITY_TYPE: ClassVar[str] = ENTITY_TYPE
    RESTRICTED_FIELDS: ClassVar[tuple[str, ...]] = ("label",)

    @property
    def entity_type(self) -> str:
        return ENTITY_TYPE

    @property
    def custody(self) -> Custody:
        return Custody(self._database, self._clock)

    def register(self, context: AccessContext, *, artifact_id: str, label: str, unit: str, quantity: str, location_id: str, request_key: str) -> dict:
        """从遗物登记母样，数量与保管地点一并入账。"""
        context.require(WRITE)
        require_safe(unit, "单位")
        minor = to_quantity_minor(quantity)
        artifact = self._artifact(artifact_id)
        request = {"artifact_id": artifact_id, "label": label, "unit": unit, "quantity": quantity, "location_id": location_id}
        with self._database.transaction() as connection:
            def operation() -> dict:
                self._repository.get_in(connection, "locations", location_id)
                payload = {"artifact_id": artifact_id, "label": label.strip(), "unit": unit, "quantity": str(quantity), "quantity_minor": minor, "location_id": location_id, "sensitive": bool(artifact.get("sensitive")), "material_class": str(artifact.get("material_class", "common")), "state": "registered"}
                sample = self._repository.create_in(connection, ENTITY_TYPE, payload, actor=context.actor_id, request_key=request_key)
                movement = self.custody.post(connection, kind="register", sample_id=sample["entity_id"], counterpart_id=artifact_id, quantity_minor=minor, unit=unit, reference=label.strip(), request_key=f"{request_key}:register", actor=context.actor_id)
                return {"sample": sample, "movement": movement}
            return self._repository.idempotency.execute(connection, scope="samples:register", request_key=request_key, request=request, operation=operation)

    def split(self, context: AccessContext, *, parent_id: str, label: str, quantity: str, location_id: str, request_key: str) -> dict:
        """分装：从母样分出子样，同一事务内核对父子关系与可用数量。"""
        context.require(WRITE)
        minor = to_quantity_minor(quantity)
        request = {"parent_id": parent_id, "label": label, "quantity": quantity, "location_id": location_id}
        with self._database.transaction() as connection:
            def operation() -> dict:
                parent = self._repository.get_in(connection, ENTITY_TYPE, parent_id)
                if parent["state"] in FROZEN_STATES:
                    raise ConflictError(f"母样处于 {parent['state']} 状态，不能分装")
                self._repository.get_in(connection, "locations", location_id)
                if minor > self.custody.available(connection, parent_id):
                    raise ConflictError("母样可用数量不足，可能存在其他研究预留")
                payload = {"artifact_id": parent["artifact_id"], "parent_id": parent_id, "label": label.strip(), "unit": parent["unit"], "quantity": str(quantity), "quantity_minor": minor, "location_id": location_id, "sensitive": bool(parent.get("sensitive")), "material_class": str(parent.get("material_class", "common")), "state": "registered"}
                child = self._repository.create_in(connection, ENTITY_TYPE, payload, actor=context.actor_id, request_key=request_key)
                out = self.custody.post(connection, kind="split_out", sample_id=parent_id, counterpart_id=child["entity_id"], quantity_minor=minor, unit=parent["unit"], reference=label.strip(), request_key=f"{request_key}:out", actor=context.actor_id)
                into = self.custody.post(connection, kind="split_in", sample_id=child["entity_id"], counterpart_id=parent_id, quantity_minor=minor, unit=parent["unit"], reference=label.strip(), request_key=f"{request_key}:in", actor=context.actor_id)
                return {"sample": child, "parent_id": parent_id, "movements": [out, into]}
            return self._repository.idempotency.execute(connection, scope="samples:split", request_key=request_key, request=request, operation=operation)

    def reserve_in(self, connection, *, sample_id: str, quantity_minor: int, reference: str, actor: str, request_key: str) -> dict:
        """为已批准的研究申请预留数量（在同一事务内由研究模块调用）。"""
        sample = self._repository.get_in(connection, ENTITY_TYPE, sample_id)
        if sample["state"] in FROZEN_STATES:
            raise ConflictError(f"样本处于 {sample['state']} 状态，不能预留")
        if quantity_minor > self.custody.available(connection, sample_id):
            raise ConflictError("样本可用数量不足，不能重复消耗同一母样")
        return self.custody.post(connection, kind="reserve", sample_id=sample_id, counterpart_id=reference, quantity_minor=quantity_minor, unit=sample["unit"], reference=reference, request_key=request_key, actor=actor)

    def consume_in(self, connection, *, sample_id: str, quantity_minor: int, reference: str, actor: str, request_key: str) -> dict:
        """在批准预留的额度内消耗样本，消耗与释放预留成对入账。"""
        sample = self._repository.get_in(connection, ENTITY_TYPE, sample_id)
        if sample["state"] == "quarantined":
            raise ConflictError("样本已隔离，不能消耗")
        if quantity_minor > self.custody.reserved(connection, sample_id, reference=reference):
            raise ConflictError("消耗数量超过该申请已批准的预留额度")
        consumed = self.custody.post(connection, kind="consume", sample_id=sample_id, counterpart_id=reference, quantity_minor=quantity_minor, unit=sample["unit"], reference=reference, request_key=f"{request_key}:consume", actor=actor)
        released = self.custody.post(connection, kind="release", sample_id=sample_id, counterpart_id=reference, quantity_minor=quantity_minor, unit=sample["unit"], reference=reference, request_key=f"{request_key}:release", actor=actor)
        if self.custody.held(connection, sample_id) == 0 and sample["state"] != "consumed":
            self._repository.update_in(connection, ENTITY_TYPE, sample_id, {"state": "consumed"}, actor=actor, expected_version=sample["version"], request_key=f"{request_key}:state")
        return {"consume": consumed, "release": released}

    def release_in(self, connection, *, sample_id: str, quantity_minor: int, reference: str, actor: str, request_key: str) -> dict:
        """返还：释放申请未使用的预留数量。"""
        sample = self._repository.get_in(connection, ENTITY_TYPE, sample_id)
        if quantity_minor > self.custody.reserved(connection, sample_id, reference=reference):
            raise ConflictError("返还数量超过该申请的预留余额")
        return self.custody.post(connection, kind="release", sample_id=sample_id, counterpart_id=reference, quantity_minor=quantity_minor, unit=sample["unit"], reference=reference, request_key=request_key, actor=actor)

    def balance(self, context: AccessContext, sample_id: str) -> dict:
        context.require(READ)
        sample = self._repository.get(ENTITY_TYPE, sample_id)
        with self._database.connect() as connection:
            return {"sample_id": sample_id, "unit": sample["unit"], "held_minor": self.custody.held(connection, sample_id), "available_minor": self.custody.available(connection, sample_id), "reserved_minor": self.custody.reserved(connection, sample_id)}

    def movements(self, context: AccessContext, sample_id: str) -> list[dict]:
        context.require(READ)
        return self.custody.movements_of(sample_id)

    def get(self, context: AccessContext, entity_id: str, *, purpose: str | None = None) -> dict:
        context.require(READ)
        return self._protect(self._repository.get(ENTITY_TYPE, entity_id), context, purpose)

    def list_current(self, context: AccessContext, *, limit: int = 100, purpose: str | None = None) -> list[dict]:
        context.require(READ)
        return [self._protect(row, context, purpose) for row in self._repository.list(ENTITY_TYPE, limit=limit)]

    def history(self, context: AccessContext, entity_id: str) -> list[dict]:
        context.require("history:lineage")
        rows = self._repository.history(ENTITY_TYPE, entity_id)
        if not rows:
            raise NotFoundError(f"{ENTITY_TYPE}/{entity_id} 不存在")
        return rows

    def children_of(self, context: AccessContext, parent_id: str) -> list[dict]:
        context.require(READ)
        return self._repository.search(ENTITY_TYPE, "parent_id", parent_id)

    def _artifact(self, artifact_id: str) -> dict:
        try:
            return self._repository.get("artifacts", artifact_id)
        except NotFoundError as exc:
            raise ValidationError(f"关联的遗物不存在: {artifact_id}") from exc

    def _protect(self, record: dict, context: AccessContext, purpose: str | None) -> dict:
        if not record.get("sensitive"):
            return record
        if self._authorize(context, str(record.get("material_class", "common")), purpose):
            return record
        masked = dict(record)
        for field_name in self.RESTRICTED_FIELDS:
            if field_name in masked:
                masked[field_name] = "***"
        return masked
