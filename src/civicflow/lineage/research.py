"""研究侧：敏感资料授权、研究申请、检测结果、研究版本、发表许可。

人骨等敏感资料按项目和用途授权；申请人不得批准自己的取样；
批准即在样本账本上预留数量，从制度上杜绝同一母样被重复消耗；
年代或文化判断作为引用具体证据的研究版本追加，田野记录不被覆盖；
结论公开发表必须持有覆盖该版本的发表许可。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Iterable, Mapping

from ..database import Database
from ..errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from ..repository import EntityRepository
from ..security import AccessContext, assert_distinct
from ..timeutil import Clock, canonical_instant
from .samples import SampleService, to_quantity_minor


READ = "read:research"
GRANT = "grant:sensitive"
SUBMIT = "submit:requests"
APPROVE = "approve:requests"
RECORD = "record:results"
APPEND = "append:versions"
PUBLISH = "publish:versions"
ISSUE = "issue:permits"

GRANTS = "grants"
REQUESTS = "requests"
RESULTS = "results"
VERSIONS = "versions"
PERMITS = "permits"

EVIDENCE_TYPES = ("results", "artifacts", "excavations", "strata")
SUBJECT_TYPES = ("sites", "trenches", "features", "strata", "artifacts")
VERSION_KINDS = ("dating", "cultural_attribution", "other")


@dataclass(frozen=True)
class SensitiveGrantService:
    """敏感资料授权：按项目、用途和资料类别授予，可过期、可撤销。"""

    _repository: EntityRepository
    _clock: Clock

    ENTITY_TYPE: ClassVar[str] = GRANTS

    def grant(self, context: AccessContext, *, project: str, purpose: str, material_class: str, grantee_org: str, expires_at: str, request_key: str) -> dict:
        context.require(GRANT)
        for label, value in (("项目", project), ("用途", purpose), ("资料类别", material_class), ("机构", grantee_org)):
            if not str(value).strip():
                raise ValidationError(f"{label}不能为空")
        payload = {"project": project.strip(), "purpose": purpose.strip(), "material_class": material_class.strip(), "grantee_org": grantee_org.strip(), "expires_at": canonical_instant(expires_at), "state": "active"}
        return self._repository.create(GRANTS, payload, actor=context.actor_id, request_key=request_key)

    def revoke(self, context: AccessContext, grant_id: str, *, request_key: str) -> dict:
        context.require(GRANT)
        grant = self._repository.get(GRANTS, grant_id)
        if grant["state"] != "active":
            raise ConflictError("授权已撤销")
        return self._repository.update(GRANTS, grant_id, {"state": "revoked"}, actor=context.actor_id, expected_version=grant["version"], request_key=request_key)

    def allows(self, context: AccessContext, material_class: str, purpose: str | None) -> bool:
        """供遗物与样本服务裁剪敏感字段：调用方需持有匹配项目与用途的有效授权。"""
        if context.reveal_sensitive:
            return True
        if purpose is None:
            return False
        projects = {scope.split(":", 1)[1] for scope in context.scopes if scope.startswith("project:")}
        return any(self.has_active(project, purpose, material_class) for project in projects)

    def has_active(self, project: str, purpose: str, material_class: str) -> bool:
        now = self._clock.now()
        for grant in self._repository.list(GRANTS, state="active", limit=500):
            if grant["project"] == project and grant["purpose"] == purpose and grant["material_class"] in (material_class, "*") and grant["expires_at"] > now:
                return True
        return False

    def require_authorized(self, project: str, purpose: str, material_class: str) -> None:
        if not self.has_active(project, purpose, material_class):
            raise PermissionDenied(f"敏感资料需要按项目和用途授权: {project}/{purpose}")


@dataclass(frozen=True)
class ResearchRequestService:
    """研究申请与审批：批准即预留样本数量，申请人不得自批。"""

    _repository: EntityRepository
    _database: Database
    _samples: SampleService
    _grants: SensitiveGrantService

    ENTITY_TYPE: ClassVar[str] = REQUESTS

    def submit(self, context: AccessContext, *, project: str, purpose: str, sample_id: str, quantity: str, method: str, request_key: str) -> dict:
        context.require(SUBMIT)
        minor = to_quantity_minor(quantity)
        try:
            sample = self._repository.get("samples", sample_id)
        except NotFoundError as exc:
            raise ValidationError(f"样本不存在: {sample_id}") from exc
        if sample["state"] in ("quarantined", "consumed"):
            raise ConflictError(f"样本处于 {sample['state']} 状态，不能申请")
        if sample.get("sensitive"):
            self._grants.require_authorized(project.strip(), purpose.strip(), str(sample.get("material_class", "common")))
        payload = {"project": project.strip(), "purpose": purpose.strip(), "applicant": context.actor_id, "sample_id": sample_id, "quantity": str(quantity), "quantity_minor": minor, "method": method.strip(), "state": "submitted"}
        return self._repository.create(REQUESTS, payload, actor=context.actor_id, request_key=request_key)

    def approve(self, context: AccessContext, request_id: str, *, request_key: str) -> dict:
        """批准申请：审批人不得是申请人本人，批准同事务内预留样本数量。"""
        context.require(APPROVE)
        with self._database.transaction() as connection:
            def operation() -> dict:
                request = self._repository.get_in(connection, REQUESTS, request_id)
                if request["state"] != "submitted":
                    raise ConflictError(f"申请处于 {request['state']} 状态，不能审批")
                assert_distinct(request["applicant"], context.actor_id)
                sample = self._repository.get_in(connection, "samples", request["sample_id"])
                if sample.get("sensitive"):
                    self._grants.require_authorized(request["project"], request["purpose"], str(sample.get("material_class", "common")))
                reservation = self._samples.reserve_in(connection, sample_id=request["sample_id"], quantity_minor=int(request["quantity_minor"]), reference=request_id, actor=context.actor_id, request_key=f"{request_key}:reserve")
                updated = self._repository.update_in(connection, REQUESTS, request_id, {"state": "approved", "approver": context.actor_id, "approved_at": self._repository.clock.now()}, actor=context.actor_id, expected_version=request["version"], request_key=request_key)
                return {"request": updated, "reservation": reservation}
            return self._repository.idempotency.execute(connection, scope="requests:approve", request_key=request_key, request={"request_id": request_id}, operation=operation)

    def reject(self, context: AccessContext, request_id: str, *, reason: str, request_key: str) -> dict:
        context.require(APPROVE)
        if not reason.strip():
            raise ValidationError("驳回必须说明原因")
        request = self._repository.get(REQUESTS, request_id)
        if request["state"] != "submitted":
            raise ConflictError(f"申请处于 {request['state']} 状态，不能审批")
        assert_distinct(request["applicant"], context.actor_id)
        return self._repository.update(REQUESTS, request_id, {"state": "rejected", "reject_reason": reason.strip()}, actor=context.actor_id, expected_version=request["version"], request_key=request_key)

    def release_unused(self, context: AccessContext, request_id: str, *, quantity: str, request_key: str) -> dict:
        """返还：释放申请未消耗的预留数量。"""
        context.require(SUBMIT)
        minor = to_quantity_minor(quantity)
        request = self._repository.get(REQUESTS, request_id)
        if request["state"] not in ("approved", "completed"):
            raise ConflictError(f"申请处于 {request['state']} 状态，没有可返还的预留")
        with self._database.transaction() as connection:
            def operation() -> dict:
                movement = self._samples.release_in(connection, sample_id=request["sample_id"], quantity_minor=minor, reference=request_id, actor=context.actor_id, request_key=request_key)
                return {"movement": movement}
            return self._repository.idempotency.execute(connection, scope="requests:release", request_key=request_key, request={"request_id": request_id, "quantity": str(quantity)}, operation=operation)

    def get(self, context: AccessContext, entity_id: str) -> dict:
        context.require(READ)
        return self._repository.get(REQUESTS, entity_id)

    def list_current(self, context: AccessContext, *, state: str | None = None, limit: int = 100) -> list[dict]:
        context.require(READ)
        return self._repository.list(REQUESTS, state=state, limit=limit)


@dataclass(frozen=True)
class TestResultService:
    """检测结果：必须在批准预留的额度内消耗样本后登记。"""

    _repository: EntityRepository
    _database: Database
    _samples: SampleService

    ENTITY_TYPE: ClassVar[str] = RESULTS

    def record(self, context: AccessContext, *, request_id: str, method: str, lab: str, consumed_quantity: str, result: Mapping[str, object], request_key: str) -> dict:
        context.require(RECORD)
        minor = to_quantity_minor(consumed_quantity)
        request_doc = {"request_id": request_id, "consumed_quantity": str(consumed_quantity)}
        with self._database.transaction() as connection:
            def operation() -> dict:
                request = self._repository.get_in(connection, REQUESTS, request_id)
                if request["state"] != "approved":
                    raise ConflictError(f"申请处于 {request['state']} 状态，不能登记检测结果")
                if minor > int(request["quantity_minor"]):
                    raise ConflictError("消耗数量不能超过申请批准的数量")
                movements = self._samples.consume_in(connection, sample_id=request["sample_id"], quantity_minor=minor, reference=request_id, actor=context.actor_id, request_key=f"{request_key}:consume")
                payload = {"request_id": request_id, "sample_id": request["sample_id"], "project": request["project"], "method": method.strip(), "lab": lab.strip(), "consumed_quantity": str(consumed_quantity), "consumed_minor": minor, "result": dict(result), "state": "recorded"}
                created = self._repository.create_in(connection, RESULTS, payload, actor=context.actor_id, request_key=request_key)
                if self._samples.custody.reserved(connection, request["sample_id"], reference=request_id) == 0:
                    current = self._repository.get_in(connection, REQUESTS, request_id)
                    self._repository.update_in(connection, REQUESTS, request_id, {"state": "completed"}, actor=context.actor_id, expected_version=current["version"], request_key=f"{request_key}:complete")
                return {"result": created, "movements": movements}
            return self._repository.idempotency.execute(connection, scope="results:record", request_key=request_key, request=request_doc, operation=operation)

    def get(self, context: AccessContext, entity_id: str) -> dict:
        context.require(READ)
        return self._repository.get(RESULTS, entity_id)

    def find_by_request(self, context: AccessContext, request_id: str) -> list[dict]:
        context.require(READ)
        return self._repository.search(RESULTS, "request_id", request_id)


@dataclass(frozen=True)
class ResearchVersionService:
    """研究版本：年代或文化判断以追加方式登记，引用具体证据，不得修改田野记录。"""

    _repository: EntityRepository

    ENTITY_TYPE: ClassVar[str] = VERSIONS

    def append(self, context: AccessContext, *, subject_type: str, subject_id: str, kind: str, conclusion: str, evidence: Iterable[Mapping[str, object]], request_key: str, supersedes: str | None = None) -> dict:
        context.require(APPEND)
        if subject_type not in SUBJECT_TYPES:
            raise ValidationError("研究对象类型必须是 " + "/".join(SUBJECT_TYPES))
        if kind not in VERSION_KINDS:
            raise ValidationError("研究版本类型必须是 " + "/".join(VERSION_KINDS))
        if not conclusion.strip():
            raise ValidationError("结论不能为空")
        try:
            self._repository.get(subject_type, subject_id)
        except NotFoundError as exc:
            raise ValidationError(f"研究对象不存在: {subject_type}/{subject_id}") from exc
        refs = []
        for item in evidence:
            entity_type = str(item.get("entity_type", ""))
            entity_id = str(item.get("entity_id", ""))
            if entity_type not in EVIDENCE_TYPES:
                raise ValidationError("证据类型必须是 " + "/".join(EVIDENCE_TYPES))
            try:
                self._repository.get(entity_type, entity_id)
            except NotFoundError as exc:
                raise ValidationError(f"引用的证据不存在: {entity_type}/{entity_id}") from exc
            refs.append({"entity_type": entity_type, "entity_id": entity_id})
        if not refs:
            raise ValidationError("研究版本必须引用至少一条具体证据")
        if supersedes is not None:
            self._repository.get(VERSIONS, supersedes)
        payload = {"subject_type": subject_type, "subject_id": subject_id, "kind": kind, "conclusion": conclusion.strip(), "evidence": refs, "supersedes": supersedes, "state": "registered"}
        return self._repository.create(VERSIONS, payload, actor=context.actor_id, request_key=request_key)

    def publish(self, context: AccessContext, version_id: str, *, permit_id: str, request_key: str) -> dict:
        """发表：必须持有覆盖该版本的有效发表许可。"""
        context.require(PUBLISH)
        version = self._repository.get(VERSIONS, version_id)
        if version["state"] != "registered":
            raise ConflictError(f"研究版本处于 {version['state']} 状态")
        try:
            permit = self._repository.get(PERMITS, permit_id)
        except NotFoundError as exc:
            raise PermissionDenied("没有覆盖该结论的发表许可") from exc
        if permit["state"] != "issued" or version_id not in permit["version_ids"]:
            raise PermissionDenied("没有覆盖该结论的发表许可")
        return self._repository.update(VERSIONS, version_id, {"state": "published", "permit_id": permit_id}, actor=context.actor_id, expected_version=version["version"], request_key=request_key)

    def get(self, context: AccessContext, entity_id: str) -> dict:
        context.require(READ)
        return self._repository.get(VERSIONS, entity_id)

    def list_current(self, context: AccessContext, *, limit: int = 100) -> list[dict]:
        context.require(READ)
        return self._repository.list(VERSIONS, limit=limit)


@dataclass(frozen=True)
class PublicationPermitService:
    """发表许可：按项目签发，明确覆盖的研究版本。"""

    _repository: EntityRepository

    ENTITY_TYPE: ClassVar[str] = PERMITS

    def issue(self, context: AccessContext, *, project: str, scope: str, version_ids: Iterable[str], request_key: str) -> dict:
        context.require(ISSUE)
        ids = [str(value).strip() for value in version_ids]
        if not ids or any(not value for value in ids):
            raise ValidationError("发表许可必须覆盖至少一个研究版本")
        for version_id in ids:
            self._repository.get(VERSIONS, version_id)
        payload = {"project": project.strip(), "scope": scope.strip(), "version_ids": ids, "state": "issued"}
        return self._repository.create(PERMITS, payload, actor=context.actor_id, request_key=request_key)

    def revoke(self, context: AccessContext, permit_id: str, *, request_key: str) -> dict:
        context.require(ISSUE)
        permit = self._repository.get(PERMITS, permit_id)
        if permit["state"] != "issued":
            raise ConflictError("许可已撤销")
        return self._repository.update(PERMITS, permit_id, {"state": "revoked"}, actor=context.actor_id, expected_version=permit["version"], request_key=request_key)

    def get(self, context: AccessContext, entity_id: str) -> dict:
        context.require(READ)
        return self._repository.get(PERMITS, entity_id)
