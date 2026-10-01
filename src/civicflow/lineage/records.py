"""田野记录链：遗址、探方、遗迹单位、层位、出土事件、遗物、保管地点、修复。

田野记录一经登记即不可修改，只能通过 corrections 追加更正；
年代或文化判断等研究结论不得写回这些记录，见 research.ResearchVersionService。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, ClassVar, Iterable, Mapping

from ..errors import NotFoundError, ValidationError
from ..repository import EntityRepository
from ..security import AccessContext


READ = "read:lineage"
WRITE = "write:lineage"
HISTORY = "history:lineage"

CORRECTIONS_TYPE = "corrections"


@dataclass(frozen=True)
class RecordService:
    """谱系记录服务基类：创建时校验父链，登记后不可修改。"""

    _repository: EntityRepository

    ENTITY_TYPE: ClassVar[str] = ""
    REQUIRED: ClassVar[tuple[str, ...]] = ()
    OPTIONAL: ClassVar[tuple[str, ...]] = ()
    PARENT: ClassVar[tuple[str, str, str] | None] = None
    OPTIONAL_LINKS: ClassVar[tuple[tuple[str, str, str], ...]] = ()
    MUTABLE: ClassVar[bool] = False

    @property
    def entity_type(self) -> str:
        return self.ENTITY_TYPE

    def create(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require(WRITE)
        payload = self._validate(values)
        self._require_parent(payload)
        for field, entity_type, label in self.OPTIONAL_LINKS:
            if field in payload:
                self._require_exists(entity_type, str(payload[field]), label)
        payload["state"] = "recorded"
        return self._repository.create(self.entity_type, payload, actor=context.actor_id, request_key=request_key)

    def revise(self, context: AccessContext, entity_id: str, values: Mapping[str, object], *, expected_version: int, request_key: str) -> dict:
        context.require(WRITE)
        if not self.MUTABLE:
            raise ValidationError("田野记录不可修改，只能通过 correct 追加更正")
        payload = self._validate_partial(values)
        return self._repository.update(self.entity_type, entity_id, payload, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def correct(self, context: AccessContext, entity_id: str, changes: Mapping[str, object], *, reason: str, request_key: str) -> dict:
        """以追加更正记录的方式修正田野记录，原始记录保持不变。"""
        context.require(WRITE)
        if not reason.strip():
            raise ValidationError("更正必须说明原因")
        target = self._repository.get(self.entity_type, entity_id)
        payload = {"target_type": self.entity_type, "target_id": entity_id, "changes": dict(changes), "reason": reason.strip(), "state": "recorded"}
        correction = self._repository.create(CORRECTIONS_TYPE, payload, actor=context.actor_id, request_key=request_key)
        return {"correction": correction, "target": target}

    def get(self, context: AccessContext, entity_id: str) -> dict:
        context.require(READ)
        return self._repository.get(self.entity_type, entity_id)

    def list_current(self, context: AccessContext, *, limit: int = 100) -> list[dict]:
        context.require(READ)
        return self._repository.list(self.entity_type, limit=limit)

    def history(self, context: AccessContext, entity_id: str) -> list[dict]:
        context.require(HISTORY)
        rows = self._repository.history(self.entity_type, entity_id)
        if not rows:
            raise NotFoundError(f"{self.entity_type}/{entity_id} 不存在")
        return rows

    def find_by(self, context: AccessContext, field: str, value: object, *, limit: int = 100) -> list[dict]:
        context.require(READ)
        return self._repository.search(self.entity_type, field, value, limit=limit)

    def corrections_of(self, context: AccessContext, entity_id: str) -> list[dict]:
        context.require(READ)
        return self._repository.search(CORRECTIONS_TYPE, "target_id", entity_id)

    def _require_parent(self, payload: Mapping[str, object]) -> None:
        if self.PARENT is not None:
            field, entity_type, label = self.PARENT
            self._require_exists(entity_type, str(payload[field]), label)

    def _require_exists(self, entity_type: str, entity_id: str, label: str) -> None:
        try:
            self._repository.get(entity_type, entity_id)
        except NotFoundError as exc:
            raise ValidationError(f"关联的{label}不存在: {entity_id}") from exc

    def _validate(self, values: Mapping[str, object]) -> dict:
        unknown = set(values) - set(self.REQUIRED) - set(self.OPTIONAL)
        if unknown:
            raise ValidationError("未知字段: " + ", ".join(sorted(unknown)))
        missing = [field for field in self.REQUIRED if field not in values]
        if missing:
            raise ValidationError("缺少字段: " + ", ".join(missing))
        return self._clean(dict(values))

    def _validate_partial(self, values: Mapping[str, object]) -> dict:
        unknown = set(values) - set(self.REQUIRED) - set(self.OPTIONAL)
        if unknown:
            raise ValidationError("未知字段: " + ", ".join(sorted(unknown)))
        if not values:
            raise ValidationError("修改内容不能为空")
        return self._clean(dict(values))

    @staticmethod
    def _clean(payload: dict) -> dict:
        for key, value in payload.items():
            if value is None:
                raise ValidationError(f"{key} 不能为空")
            if isinstance(value, str):
                payload[key] = value.strip()
                if not payload[key]:
                    raise ValidationError(f"{key} 不能为空字符串")
        return payload


class SiteService(RecordService):
    """遗址。"""

    ENTITY_TYPE = "sites"
    REQUIRED = ("name", "code", "country")
    OPTIONAL = ("description",)


class TrenchService(RecordService):
    """探方。"""

    ENTITY_TYPE = "trenches"
    REQUIRED = ("site_id", "code", "opened_at")
    PARENT = ("site_id", "sites", "遗址")


FEATURE_KINDS = ("tomb", "ash_pit", "house", "other")


class FeatureService(RecordService):
    """遗迹单位（墓葬、灰坑等）。"""

    ENTITY_TYPE = "features"
    REQUIRED = ("trench_id", "kind", "code")
    OPTIONAL = ("description",)
    PARENT = ("trench_id", "trenches", "探方")

    def create(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        if str(values.get("kind", "")) not in FEATURE_KINDS:
            raise ValidationError("遗迹类型必须是 " + "/".join(FEATURE_KINDS))
        return super().create(context, values, request_key=request_key)


class StratumService(RecordService):
    """层位。"""

    ENTITY_TYPE = "strata"
    REQUIRED = ("trench_id", "code")
    OPTIONAL = ("description",)
    PARENT = ("trench_id", "trenches", "探方")


class ExcavationService(RecordService):
    """出土事件。"""

    ENTITY_TYPE = "excavations"
    REQUIRED = ("feature_id", "occurred_at", "recorder", "field_notes")
    OPTIONAL = ("stratum_id",)
    PARENT = ("feature_id", "features", "遗迹单位")
    OPTIONAL_LINKS = (("stratum_id", "strata", "层位"),)


SensitiveAuthorizer = Callable[[AccessContext, str, str | None], bool]


def _deny_sensitive(context: AccessContext, material_class: str, purpose: str | None) -> bool:
    return bool(context.reveal_sensitive)


@dataclass(frozen=True)
class ArtifactService(RecordService):
    """遗物。敏感遗物（如人骨）的田野编号与描述按项目和用途授权后可见。"""

    _authorize: SensitiveAuthorizer = _deny_sensitive

    ENTITY_TYPE = "artifacts"
    REQUIRED = ("event_id", "field_number", "kind", "material")
    OPTIONAL = ("material_class", "sensitive", "weight", "description")
    PARENT = ("event_id", "excavations", "出土事件")
    RESTRICTED_FIELDS: ClassVar[tuple[str, ...]] = ("field_number", "description")

    def create(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        payload = dict(values)
        payload.setdefault("material_class", "common")
        payload.setdefault("sensitive", False)
        if not isinstance(payload["sensitive"], bool):
            raise ValidationError("sensitive 必须是布尔值")
        return super().create(context, payload, request_key=request_key)

    def get(self, context: AccessContext, entity_id: str, *, purpose: str | None = None) -> dict:
        context.require(READ)
        record = self._repository.get(self.entity_type, entity_id)
        return self._protect(record, context, purpose)

    def list_current(self, context: AccessContext, *, limit: int = 100, purpose: str | None = None) -> list[dict]:
        context.require(READ)
        return [self._protect(row, context, purpose) for row in self._repository.list(self.entity_type, limit=limit)]

    def history(self, context: AccessContext, entity_id: str, *, purpose: str | None = None) -> list[dict]:
        rows = super().history(context, entity_id)
        return [self._protect(row, context, purpose) for row in rows]

    def _protect(self, record: Mapping[str, object], context: AccessContext, purpose: str | None) -> dict:
        if not record.get("sensitive"):
            return dict(record)
        if self._authorize(context, str(record.get("material_class", "common")), purpose):
            return dict(record)
        masked = dict(record)
        for field_name in self.RESTRICTED_FIELDS:
            if field_name in masked:
                masked[field_name] = "***"
        return masked


class LocationService(RecordService):
    """保管地点。保存条件可随检查更新，历史版本全部保留。"""

    ENTITY_TYPE = "locations"
    REQUIRED = ("org", "facility")
    OPTIONAL = ("room", "conditions")
    MUTABLE = True

    def create(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        payload = dict(values)
        conditions = payload.get("conditions", {})
        if not isinstance(conditions, dict):
            raise ValidationError("保存条件必须是对象")
        payload["conditions"] = conditions
        return super().create(context, payload, request_key=request_key)


class RestorationService(RecordService):
    """修复记录，每次修复追加一条。"""

    ENTITY_TYPE = "restorations"
    REQUIRED = ("artifact_id", "action", "performed_by")
    OPTIONAL = ("started_at", "finished_at")
    PARENT = ("artifact_id", "artifacts", "遗物")
