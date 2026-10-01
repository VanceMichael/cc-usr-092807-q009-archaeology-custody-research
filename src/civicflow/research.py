"""研究治理：敏感资料授权、研究申请与回避、研究版本追加、检测结果、发表许可。

- 人骨等敏感资料按项目+用途授权（access_grants），取样申请必须命中授权；
- 申请人不能批准自己的取样申请；
- 年代/文化判断以 research_versions 追加，引用具体样本与田野证据，
  田野记录（出土事件/遗物）永不被研究结论覆盖；
- 检测结果挂接取样消耗事件；发表许可覆盖结论与所用证据清单。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .database import Database
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id
from .repository import EntityRepository
from .samples import SAMPLE_TYPE
from .security import AccessContext
from .timeutil import canonical_instant


GRANT_SCOPE = "access_grants"
REQUEST_TYPE = "research_requests"
VERSION_TYPE = "research_versions"
RESULT_TYPE = "test_results"
PERMISSION_TYPE = "publication_permissions"

REQUEST_STATES = ("submitted", "approved", "rejected", "withdrawn", "closed")
SENSITIVE_MATERIALS = {"human_remains", "bone"}


def _t(values: Mapping[str, object], key: str, label: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}不能为空")
    return value.strip()


@dataclass(frozen=True)
class ResearchService:
    repository: EntityRepository
    database: Database

    # ------------------------------------------------------------ 授权

    def grant_access(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("grant:research")
        project_id = _t(values, "project_id", "项目")
        subject_id = _t(values, "subject_id", "授权对象")
        purpose = _t(values, "purpose", "用途")
        material_scope = _t(values, "material_scope", "材料范围")
        valid_from = canonical_instant(_t(values, "valid_from", "生效时间"))
        valid_until = values.get("valid_until")
        if valid_until is not None:
            valid_until = canonical_instant(_t(values, "valid_until", "失效时间"))
            if valid_until <= valid_from:
                raise ValidationError("失效时间必须晚于生效时间")
        grant_id = new_id("grant")
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO access_grants(grant_id,project_id,subject_id,purpose,material_scope,granted_by,"
                "valid_from,valid_until,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (grant_id, project_id, subject_id, purpose, material_scope, context.actor_id,
                 valid_from, valid_until, self.repository.clock.now()))
        return {"grant_id": grant_id, "project_id": project_id, "subject_id": subject_id,
                "purpose": purpose, "material_scope": material_scope,
                "valid_from": valid_from, "valid_until": valid_until}

    def assert_authorized(self, *, project_id: str, subject_id: str, purpose: str, material: str, at: str) -> None:
        instant = canonical_instant(at)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM access_grants WHERE project_id=? AND subject_id=? AND valid_from<=? "
                "AND (valid_until IS NULL OR valid_until>?) ORDER BY created_at",
                (project_id, subject_id, instant, instant)).fetchall()
        for row in rows:
            if row["purpose"] != purpose:
                continue
            scope = row["material_scope"]
            if scope == "*" or scope == material:
                return
        raise PermissionDenied(f"{subject_id} 未获得项目 {project_id} 中 {material} 用于 {purpose} 的授权")

    # ------------------------------------------------------------ 研究申请

    def submit_request(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:research")
        project_id = _t(values, "project_id", "项目")
        purpose = _t(values, "purpose", "研究用途")
        sample_ids = values.get("sample_ids")
        if not isinstance(sample_ids, list) or not sample_ids:
            raise ValidationError("申请样本不能为空")
        materials = set()
        for sample_id in sample_ids:
            sample = self.repository.get(SAMPLE_TYPE, str(sample_id))
            if sample["project_id"] != project_id:
                raise ValidationError(f"样本 {sample_id} 不属于项目 {project_id}")
            materials.add(str(sample["material"]))
        for material in materials:
            if material in SENSITIVE_MATERIALS:
                self.assert_authorized(project_id=project_id, subject_id=context.actor_id,
                                       purpose=purpose, material=material, at=self.repository.clock.now())
        payload = {
            "state": "submitted",
            "project_id": project_id,
            "applicant": context.actor_id,
            "purpose": purpose,
            "method": _t(values, "method", "检测方法"),
            "sample_ids": [str(item) for item in sample_ids],
            "materials": sorted(materials),
            "consume_plan": dict(values["consume_plan"]) if isinstance(values.get("consume_plan"), Mapping) else {},
        }
        return self.repository.create(REQUEST_TYPE, payload, actor=context.actor_id, request_key=request_key)

    def decide_request(self, context: AccessContext, request_id: str, *, decision: str, reason: str,
                       expected_version: int, request_key: str) -> dict:
        """批准/拒绝取样申请。申请人不得批准自己的申请。"""
        context.require("approve:research")
        if decision not in ("approved", "rejected"):
            raise ValidationError("决定必须是 approved 或 rejected")
        if not reason.strip():
            raise ValidationError("审批必须说明理由")
        record = self.repository.get(REQUEST_TYPE, request_id)
        if record["state"] != "submitted":
            raise ConflictError(f"申请处于 {record['state']}，不能审批")
        if record["applicant"] == context.actor_id:
            raise PermissionDenied("申请人不能批准自己的取样申请")
        if decision == "approved":
            # 批准时再核一次授权，防止授权在申请期间失效。
            for material in record["materials"]:
                if material in SENSITIVE_MATERIALS:
                    self.assert_authorized(project_id=record["project_id"], subject_id=record["applicant"],
                                           purpose=record["purpose"], material=material,
                                           at=self.repository.clock.now())
        return self.repository.update(REQUEST_TYPE, request_id,
                                      {"state": decision, "reviewer": context.actor_id,
                                       "decision_reason": reason.strip()},
                                      actor=context.actor_id, expected_version=expected_version,
                                      request_key=request_key)

    def approved_request(self, request_id: str) -> dict:
        record = self.repository.get(REQUEST_TYPE, request_id)
        if record["state"] != "approved":
            raise ConflictError(f"申请 {request_id} 未获批准（当前 {record['state']}）")
        return record

    # ------------------------------------------------------------ 研究版本

    def add_interpretation(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        """追加年代或文化判断。研究版本只追加、不改写，且必须引用具体证据。"""
        context.require("write:research")
        kind = _t(values, "kind", "判断类型")
        if kind not in ("dating", "cultural_attribution"):
            raise ValidationError("判断类型必须是 dating 或 cultural_attribution")
        claim = _t(values, "claim", "结论")
        evidence_ids = values.get("evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise ValidationError("研究结论必须引用具体证据（样本/检测结果/出土事件）")
        for ref in evidence_ids:
            if not isinstance(ref, Mapping) or not str(ref.get("entity_type", "")).strip() or not str(ref.get("entity_id", "")).strip():
                raise ValidationError("证据引用必须包含 entity_type 与 entity_id")
            self._evidence_exists(str(ref["entity_type"]), str(ref["entity_id"]))
        based_on = values.get("based_on_results", [])
        if not isinstance(based_on, list) or not based_on:
            raise ValidationError("研究判断必须基于至少一条检测结果")
        for result_id in based_on:
            self._evidence_exists(RESULT_TYPE, str(result_id))
        payload = {
            "state": "recorded",
            "project_id": _t(values, "project_id", "项目"),
            "author": context.actor_id,
            "kind": kind,
            "claim": claim,
            "evidence_ids": [{"entity_type": str(r["entity_type"]).strip(), "entity_id": str(r["entity_id"]).strip()} for r in evidence_ids],
            "based_on_results": [str(item) for item in based_on],
            "supersedes_version_id": values.get("supersedes_version_id"),
        }
        return self.repository.create(VERSION_TYPE, payload, actor=context.actor_id, request_key=request_key)

    def interpretations(self, sample_id: str) -> list[dict]:
        """读取引用某样本的全部研究版本（含被后续版本修正者），证明结论只追加。"""
        result = []
        for row in self.repository.list(VERSION_TYPE, limit=500):
            if any(ref["entity_type"] == SAMPLE_TYPE and ref["entity_id"] == sample_id
                   for ref in row.get("evidence_ids", [])):
                result.append(row)
            elif any(sample_id == sid for sid in self._result_samples(row)):
                result.append(row)
        return result

    def _result_samples(self, version: Mapping[str, object]) -> list[str]:
        sample_ids: list[str] = []
        for result_id in version.get("based_on_results", []):
            try:
                result = self.repository.get(RESULT_TYPE, result_id)
            except NotFoundError:
                continue
            sample_ids.extend(result.get("sample_ids", []))
        return sample_ids

    def _evidence_exists(self, entity_type: str, entity_id: str) -> None:
        allowed = {SAMPLE_TYPE, RESULT_TYPE, "finds", "arch_recovery_events", VERSION_TYPE}
        if entity_type not in allowed:
            raise ValidationError(f"证据类型 {entity_type} 不能作为研究依据")
        self.repository.get(entity_type, entity_id)

    # ------------------------------------------------------------ 检测结果

    def record_result(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:research")
        request_id = _t(values, "request_id", "研究申请")
        request = self.approved_request(request_id)
        sample_ids = values.get("sample_ids")
        if not isinstance(sample_ids, list) or not sample_ids:
            raise ValidationError("检测结果必须挂接样本")
        for sample_id in sample_ids:
            sample = self.repository.get(SAMPLE_TYPE, str(sample_id))
            if sample["project_id"] != request["project_id"]:
                raise ValidationError(f"样本 {sample_id} 不属于项目 {request['project_id']}")
            if str(sample_id) not in request["sample_ids"]:
                raise ValidationError(f"样本 {sample_id} 不在申请 {request_id} 的批准范围内")
        method = _t(values, "method", "实验方法")
        if method != request["method"]:
            raise ValidationError("实验方法与批准申请不一致")
        payload = {
            "state": "issued",
            "project_id": request["project_id"],
            "request_id": request_id,
            "lab_org": _t(values, "lab_org", "检测机构"),
            "method": method,
            "sample_ids": [str(item) for item in sample_ids],
            "values": dict(values["values"]) if isinstance(values.get("values"), Mapping) else {},
            "raw_ref": _t(values, "raw_ref", "原始记录引用"),
            "issued_at": canonical_instant(_t(values, "issued_at", "出具时间")),
        }
        return self.repository.create(RESULT_TYPE, payload, actor=context.actor_id, request_key=request_key)

    # ------------------------------------------------------------ 发表许可

    def grant_publication(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("permit:publication")
        project_id = _t(values, "project_id", "项目")
        title = _t(values, "title", "论文题目")
        version_ids = values.get("version_ids")
        if not isinstance(version_ids, list) or not version_ids:
            raise ValidationError("发表许可必须覆盖研究结论")
        evidence = []
        for version_id in version_ids:
            version = self.repository.get(VERSION_TYPE, str(version_id))
            if version["project_id"] != project_id:
                raise ValidationError(f"结论 {version_id} 不属于项目 {project_id}")
            evidence.append({"version_id": str(version_id), "evidence_ids": version["evidence_ids"],
                             "based_on_results": version["based_on_results"]})
        payload = {
            "state": "granted",
            "project_id": project_id,
            "title": title,
            "applicant": _t(values, "applicant", "申请人"),
            "version_ids": [str(item) for item in version_ids],
            "evidence_manifest": evidence,
            "scope": _t(values, "scope", "许可范围"),
            "granted_at": canonical_instant(_t(values, "granted_at", "许可时间")),
        }
        return self.repository.create(PERMISSION_TYPE, payload, actor=context.actor_id, request_key=request_key)
