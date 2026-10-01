"""遗物样本谱系：母样/子样登记、分装、取样、消耗、返还与守恒核对。

所有数量变动都以不可变的 sample_lineage_events 流水记录，样本实体上的
amount_available 只是派生余额；balance() 用流水重算并与账面比对，
verify_lineage() 对全库做守恒巡检。田野层位关联通过 finds 保留。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

from .database import Database
from .errors import ConflictError, ValidationError
from .identifiers import new_id
from .jsonutil import canonical_json
from .repository import EntityRepository, read_entity
from .security import AccessContext
from .timeutil import Clock


SAMPLE_TYPE = "samples"
FIND_TYPE = "finds"
REQUEST_TYPE = "research_requests"

# 影响可分配余量的事件方向：分装出库/检测消耗/销毁为负，退库为正；
# split_in 仅作父子谱系凭证，不重复计入余量。
NEGATIVE_EVENTS = ("split_out", "consume", "destroy")
POSITIVE_EVENTS = ("restock",)
BALANCE_EVENTS = NEGATIVE_EVENTS + POSITIVE_EVENTS
LINEAGE_EVENTS = ("register", "split_out", "split_in", "consume", "restock", "destroy", "custody")

USABLE_STATES = ("active", "exhausted")


def amount(value: object, label: str = "数量") -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{label}格式错误") from exc
    if not number.is_finite():
        raise ValidationError(f"{label}必须是有限数")
    if number <= 0:
        raise ValidationError(f"{label}必须大于零")
    return number


@dataclass(frozen=True)
class SampleBook:
    repository: EntityRepository
    database: Database
    clock: Clock

    # ---------------------------------------------------------------- 登记

    def register(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:samples")
        replay = self._replay(request_key)
        if replay is not None:
            return replay
        project_id = str(values.get("project_id", "")).strip()
        find_id = str(values.get("find_id", "")).strip()
        material = str(values.get("material", "")).strip()
        uom = str(values.get("uom", "")).strip()
        code = str(values.get("code", "")).strip()
        if not project_id or not find_id or not material or not uom or not code:
            raise ValidationError("project_id/find_id/material/uom/code 均不能为空")
        qty = amount(values.get("amount"), "登记数量")
        find = self.repository.get(FIND_TYPE, find_id)
        if find.get("project_id") != project_id:
            raise ValidationError("遗物与样本不属于同一项目")
        with self.database.transaction() as connection:
            sample_id = new_id(SAMPLE_TYPE)
            payload = {
                "state": "active",
                "project_id": project_id,
                "find_id": find_id,
                "code": code,
                "material": material,
                "sensitive": bool(values.get("sensitive", False)),
                "uom": uom,
                "amount_initial": str(qty),
                "amount_available": str(qty),
                "parent_sample_id": None,
                "root_sample_id": sample_id,
                "custodian_org": values.get("custodian_org") or find.get("holding_org"),
            }
            sample = self.repository.insert_within(connection, SAMPLE_TYPE, payload,
                                                   actor=context.actor_id, request_key=request_key,
                                                   entity_id=sample_id)
            self._insert_event(connection, sample_id=sample["entity_id"], event_type="register", delta=qty,
                               actor=context.actor_id, note=f"登记母样 {code}",
                               request_key=request_key + ":register", affects_balance=False,
                               project_id=project_id, uom=uom)
            result = read_entity(connection, SAMPLE_TYPE, sample["entity_id"])
            self._remember(connection, request_key, result)
            return result

    # ---------------------------------------------------------------- 分装

    def split(self, context: AccessContext, parent_id: str, children: Iterable[Mapping[str, object]], *, request_key: str) -> dict:
        """从母样分装子样：核对父子关系且出库总量不得超过余量，整组原子提交。"""
        context.require("write:samples")
        replay = self._replay(request_key)
        if replay is not None:
            return replay
        child_inputs = [dict(item) for item in children]
        if not child_inputs:
            raise ValidationError("分装至少需要一个子样")
        total = Decimal("0")
        for item in child_inputs:
            qty = amount(item.get("amount"), "子样数量")
            item["amount_dec"] = qty
            total += qty
            if not str(item.get("material", "")).strip():
                raise ValidationError("子样材料不能为空")
        with self.database.transaction() as connection:
            parent = read_entity(connection, SAMPLE_TYPE, parent_id)
            self._require_usable(parent)
            available = Decimal(parent["amount_available"])
            if total > available:
                raise ConflictError(f"分装总量 {total} 超过母样余量 {available} {parent['uom']}")
            created = []
            for index, item in enumerate(child_inputs, start=1):
                payload = {
                    "state": "active",
                    "project_id": parent["project_id"],
                    "find_id": parent["find_id"],
                    "code": str(item.get("code") or f"{parent['code']}-A{index}").strip(),
                    "material": str(item["material"]).strip(),
                    "sensitive": parent["sensitive"],
                    "uom": parent["uom"],
                    "amount_initial": str(item["amount_dec"]),
                    "amount_available": str(item["amount_dec"]),
                    "parent_sample_id": parent_id,
                    "root_sample_id": parent["root_sample_id"] or parent_id,
                    "custodian_org": parent["custodian_org"],
                    "purpose": str(item.get("purpose", "")).strip(),
                }
                child = self.repository.insert_within(connection, SAMPLE_TYPE, payload, actor=context.actor_id,
                                                      request_key=f"{request_key}:child:{index}")
                self._insert_event(connection, sample_id=child["entity_id"], event_type="split_in",
                                   delta=item["amount_dec"], actor=context.actor_id, parent_id=parent_id,
                                   child_id=child["entity_id"], note="分装入库（谱系凭证，不重复计余量）",
                                   request_key=f"{request_key}:in:{index}", affects_balance=False)
                self._insert_event(connection, sample_id=parent_id, event_type="split_out",
                                   delta=-item["amount_dec"], actor=context.actor_id, parent_id=parent_id,
                                   child_id=child["entity_id"], note=f"分装至 {child['code']}",
                                   request_key=f"{request_key}:out:{index}", affects_balance=True)
                created.append(self._brief(child))
            new_available = available - total
            changes = {"amount_available": str(new_available)}
            if new_available == 0:
                changes["state"] = "exhausted"
            updated = self.repository.update_within(connection, SAMPLE_TYPE, parent_id, changes,
                                                    actor=context.actor_id, expected_version=parent["version"],
                                                    request_key=request_key + ":parent")
            result = {"status": "split", "parent_id": parent_id, "children": created,
                      "parent_available": updated["amount_available"], "total": str(total)}
            self._remember(connection, request_key, result)
            return result

    # ---------------------------------------------------- 取样消耗与退库返还

    def consume(self, context: AccessContext, sample_id: str, qty: object, *, request_id: str, request_key: str, note: str = "") -> dict:
        """检测取样消耗：申请必须已批准、覆盖该样本，且累计消耗不得超过批准计划。"""
        context.require("write:samples")
        replay = self._replay(request_key)
        if replay is not None:
            return replay
        magnitude = amount(qty, "变动数量")
        with self.database.transaction() as connection:
            request = read_entity(connection, REQUEST_TYPE, request_id)
            if request["state"] != "approved":
                raise ConflictError(f"申请 {request_id} 未获批准（当前 {request['state']}），不得取样")
            if sample_id not in request["sample_ids"]:
                raise ConflictError(f"样本 {sample_id} 不在申请 {request_id} 的批准范围内")
            plan = Decimal(str(request.get("consume_plan", {}).get(sample_id, "0")))
            used_rows = connection.execute(
                "SELECT delta_amount FROM sample_lineage_events "
                "WHERE sample_id=? AND request_id=? AND event_type='consume'",
                (sample_id, request_id)).fetchall()
            used = sum((Decimal(row["delta_amount"]) for row in used_rows), Decimal("0"))
            # 流水里消耗记为负数，累计申请量取绝对值
            used = abs(used)
            if plan > 0 and used + magnitude > plan:
                uom = read_entity(connection, SAMPLE_TYPE, sample_id)["uom"]
                raise ConflictError(f"累计消耗 {used + magnitude} 超过申请批准的 {plan} {uom}")
        return self._balance_move(sample_id, qty, "consume", request_key, context.actor_id,
                                  request_id=request_id, note=note or f"检测消耗，申请 {request_id}")

    def destroy(self, context: AccessContext, sample_id: str, qty: object, *, reason: str, request_key: str) -> dict:
        context.require("write:samples")
        if not reason.strip():
            raise ValidationError("销毁必须说明原因")
        return self._balance_move(sample_id, qty, "destroy", request_key, context.actor_id, note=reason)

    def restock(self, context: AccessContext, sample_id: str, qty: object, *, reason: str, request_key: str) -> dict:
        """未用尽子样退回可用余量（实物保管责任由跨国交接域记录）。"""
        context.require("write:samples")
        if not reason.strip():
            raise ValidationError("退库必须说明原因")
        return self._balance_move(sample_id, qty, "restock", request_key, context.actor_id,
                                  note=reason, sign_positive=True)

    def _balance_move(self, sample_id: str, qty: object, event_type: str, request_key: str, actor: str, *,
                      request_id: str | None = None, note: str, sign_positive: bool = False) -> dict:
        replay = self._replay(request_key)
        if replay is not None:
            return replay
        magnitude = amount(qty, "变动数量")
        signed = magnitude if sign_positive else -magnitude
        with self.database.transaction() as connection:
            sample = read_entity(connection, SAMPLE_TYPE, sample_id)
            self._require_usable(sample)
            available = Decimal(sample["amount_available"])
            new_available = available + signed
            if new_available < 0:
                raise ConflictError(f"变动后余量为负：当前 {available}，申请变动 {signed} {sample['uom']}")
            self._insert_event(connection, sample_id=sample_id, event_type=event_type, delta=signed,
                               actor=actor, request_id=request_id, note=note,
                               request_key=request_key, affects_balance=True)
            changes = {"amount_available": str(new_available)}
            if new_available == 0 and event_type in NEGATIVE_EVENTS:
                changes["state"] = "exhausted"
            elif new_available > 0 and sample["state"] == "exhausted":
                changes["state"] = "active"
            self.repository.update_within(connection, SAMPLE_TYPE, sample_id, changes, actor=actor,
                                          expected_version=sample["version"], request_key=request_key + ":sample")
            result = {"status": event_type, "sample_id": sample_id, "delta": str(signed),
                      "amount_available": str(new_available)}
            self._remember(connection, request_key, result)
            return result

    def mark_quarantined(self, connection, sample_id: str, *, actor: str, request_key: str) -> None:
        """冲突隔离在调用方事务内冻结样本。"""
        sample = read_entity(connection, SAMPLE_TYPE, sample_id)
        if sample["state"] == "quarantined":
            return
        self.repository.update_within(connection, SAMPLE_TYPE, sample_id, {"state": "quarantined"},
                                      actor=actor, expected_version=sample["version"], request_key=request_key)

    def record_custody(self, connection, *, sample_id: str, transfer_id: str, actor: str,
                       custodian_org: str, note: str) -> None:
        """跨国交接收货/返还时在同一事务内登记保管责任变化（不动余量），重复执行不重复记账。"""
        event_key = f"custody:{transfer_id}:{sample_id}"
        if connection.execute("SELECT 1 FROM sample_lineage_events WHERE request_key=?", (event_key,)).fetchone():
            return
        sample = read_entity(connection, SAMPLE_TYPE, sample_id)
        self._insert_event(connection, sample_id=sample_id, event_type="custody", delta=Decimal("0"),
                           actor=actor, transfer_id=transfer_id, note=note, request_key=event_key,
                           affects_balance=False, project_id=sample["project_id"], uom=sample["uom"])
        self.repository.update_within(connection, SAMPLE_TYPE, sample_id, {"custodian_org": custodian_org},
                                      actor=actor, expected_version=sample["version"],
                                      request_key=event_key + ":ver")

    # ---------------------------------------------------------------- 查询核对

    def get(self, sample_id: str) -> dict:
        return self.repository.get(SAMPLE_TYPE, sample_id)

    def list_samples(self, *, state: str | None = None, limit: int = 100) -> list[dict]:
        return self.repository.list(SAMPLE_TYPE, state=state, limit=limit)

    def events(self, sample_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sample_lineage_events WHERE sample_id=? ORDER BY occurred_at,rowid",
                (sample_id,)).fetchall()
            return [dict(row) for row in rows]

    def children(self, sample_id: str) -> list[dict]:
        return [row for row in self.repository.list(SAMPLE_TYPE, limit=500)
                if row.get("parent_sample_id") == sample_id]

    def root_of(self, sample_id: str) -> str:
        sample = self.get(sample_id)
        return sample.get("root_sample_id") or sample_id

    def balance(self, sample_id: str) -> dict:
        sample = self.get(sample_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT delta_amount FROM sample_lineage_events WHERE sample_id=? AND affects_balance=1",
                (sample_id,)).fetchall()
        flow = sum((Decimal(row["delta_amount"]) for row in rows), Decimal("0"))
        recomputed = Decimal(sample["amount_initial"]) + flow
        booked = Decimal(sample["amount_available"])
        if recomputed != booked:
            raise ConflictError(f"样本 {sample_id} 余额不一致：流水重算 {recomputed}，账面 {booked}")
        return {"sample_id": sample_id, "initial": sample["amount_initial"], "flow": str(flow),
                "available": str(booked), "uom": sample["uom"], "state": sample["state"]}

    def verify_lineage(self) -> dict:
        """全库守恒巡检：每个样本账实相符；每次分装的父子数量相等、无孤立事件。"""
        samples = self.repository.list(SAMPLE_TYPE, limit=500)
        for sample in samples:
            self.balance(sample["entity_id"])
        groups: dict[str, dict[str, Decimal]] = {}
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sample_lineage_events WHERE event_type IN ('split_out','split_in') ORDER BY rowid").fetchall()
        for event in rows:
            parts = event["request_key"].rsplit(":", 2)
            base = parts[0] if len(parts) == 3 else event["request_key"]
            bucket = groups.setdefault(base, {"out": Decimal("0"), "in": Decimal("0"), "n": Decimal("0")})
            bucket["n"] += 1
            if event["event_type"] == "split_out":
                bucket["out"] += Decimal(event["delta_amount"])
            else:
                bucket["in"] += Decimal(event["delta_amount"])
        for key, bucket in groups.items():
            if -bucket["out"] != bucket["in"] or bucket["n"] % 2:
                raise ConflictError(f"分装 {key} 父子数量不平：出库 {bucket['out']}，入库 {bucket['in']}")
        return {"samples": len(samples), "split_groups": len(groups), "status": "balanced"}

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _require_usable(sample: Mapping[str, object]) -> None:
        if sample["state"] == "quarantined":
            raise ConflictError(f"样本 {sample['entity_id']} 已隔离，冻结一切数量变动")
        if sample["state"] == "returned":
            raise ConflictError(f"样本 {sample['entity_id']} 已返还，不得再变动")

    def _insert_event(self, connection, *, sample_id: str, event_type: str, delta: Decimal, actor: str,
                      note: str, request_key: str, affects_balance: bool, parent_id: str | None = None,
                      child_id: str | None = None, transfer_id: str | None = None,
                      request_id: str | None = None, project_id: str | None = None,
                      uom: str | None = None) -> str:
        if event_type not in LINEAGE_EVENTS:
            raise ValidationError("未知谱系事件类型")
        if project_id is None or uom is None:
            sample = read_entity(connection, SAMPLE_TYPE, sample_id)
            project_id = sample["project_id"]; uom = sample["uom"]
        event_id = new_id("lineage")
        connection.execute(
            "INSERT INTO sample_lineage_events(event_id,project_id,event_type,sample_id,parent_id,child_id,"
            "delta_amount,uom,actor_id,transfer_id,request_id,occurred_at,note,request_key,affects_balance) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, project_id, event_type, sample_id, parent_id, child_id, str(delta), uom, actor,
             transfer_id, request_id, self.clock.now(), note, request_key, 1 if affects_balance else 0))
        return event_id

    @staticmethod
    def _brief(sample: Mapping[str, object]) -> dict:
        return {"sample_id": sample["entity_id"], "code": sample["code"], "material": sample["material"],
                "amount_initial": sample["amount_initial"], "uom": sample["uom"],
                "parent_sample_id": sample["parent_sample_id"], "root_sample_id": sample["root_sample_id"]}

    def _replay(self, request_key: str) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT response_json FROM lineage_requests WHERE request_key=?",
                                     (request_key,)).fetchone()
        return json.loads(row["response_json"]) if row else None

    def _remember(self, connection, request_key: str, response: dict) -> None:
        connection.execute("INSERT INTO lineage_requests(request_key,response_json,created_at) VALUES(?,?,?)",
                           (request_key, canonical_json(response), self.clock.now()))
