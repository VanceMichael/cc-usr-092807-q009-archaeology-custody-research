"""田野谱系：遗址、探方、墓葬/灰坑、层位、出土事件、遗物、保管地点与修复。

层级关系在创建时逐级核对。出土事件与遗物是田野事实（field record），
研究域只能引用、不能覆盖；田野更正通过实体版本链留痕，旧版本永不消失。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .errors import ValidationError
from .repository import EntityRepository
from .security import AccessContext
from .timeutil import canonical_instant


SITES = "arch_sites"
TRENCHES = "arch_trenches"
FEATURES = "arch_features"
STRATA = "arch_strata"
RECOVERY_EVENTS = "arch_recovery_events"
FINDS = "finds"
STORAGE = "storage_locations"
REPAIRS = "repairs"

FEATURE_KINDS = ("burial", "pit", "ditch", "hearth", "other")


def _text(values: Mapping[str, object], key: str, label: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}不能为空")
    return value.strip()


@dataclass(frozen=True)
class FieldService:
    repository: EntityRepository

    def _create(self, context: AccessContext, entity_type: str, payload: dict, *, request_key: str, state: str = "active") -> dict:
        context.require(f"write:{entity_type}")
        payload["state"] = state
        return self.repository.create(entity_type, payload, actor=context.actor_id, request_key=request_key)

    # ------------------------------------------------------------ 空间层级

    def create_site(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        payload = {
            "project_id": _text(values, "project_id", "项目"),
            "code": _text(values, "code", "遗址编号"),
            "name": _text(values, "name", "遗址名称"),
        }
        return self._create(context, SITES, payload, request_key=request_key)

    def create_trench(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        site_id = _text(values, "site_id", "遗址")
        site = self._must_be(SITES, site_id, project_id=_optional_text(values, "project_id"))
        payload = {
            "project_id": site["project_id"],
            "site_id": site_id,
            "code": _text(values, "code", "探方编号"),
        }
        return self._create(context, TRENCHES, payload, request_key=request_key)

    def create_feature(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        """墓葬或灰坑等遗迹单位。"""
        trench_id = _text(values, "trench_id", "探方")
        trench = self._must_be(TRENCHES, trench_id)
        kind = _text(values, "kind", "遗迹类型")
        if kind not in FEATURE_KINDS:
            raise ValidationError(f"遗迹类型必须是 {FEATURE_KINDS} 之一")
        payload = {
            "project_id": trench["project_id"],
            "site_id": trench["site_id"],
            "trench_id": trench_id,
            "code": _text(values, "code", "遗迹编号"),
            "kind": kind,
        }
        return self._create(context, FEATURES, payload, request_key=request_key)

    def create_stratum(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        trench_id = _text(values, "trench_id", "探方")
        trench = self._must_be(TRENCHES, trench_id)
        feature_id = _optional_text(values, "feature_id")
        if feature_id is not None:
            feature = self._must_be(FEATURES, feature_id)
            if feature["trench_id"] != trench_id:
                raise ValidationError("层位所属遗迹与探方不一致")
        sequence = values.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValidationError("层位序序必须是非负整数")
        payload = {
            "project_id": trench["project_id"],
            "site_id": trench["site_id"],
            "trench_id": trench_id,
            "feature_id": feature_id,
            "code": _text(values, "code", "层位编号"),
            "sequence": sequence,
        }
        return self._create(context, STRATA, payload, request_key=request_key)

    # ------------------------------------------------------------ 出土事实

    def record_recovery(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        """登记出土事件：田野照片、田野记录与层位在此固化，后续研究不得改写。"""
        trench_id = _text(values, "trench_id", "探方")
        trench = self._must_be(TRENCHES, trench_id)
        feature_id = _optional_text(values, "feature_id")
        if feature_id is not None:
            feature = self._must_be(FEATURES, feature_id)
            if feature["trench_id"] != trench_id:
                raise ValidationError("出土事件所属遗迹与探方不一致")
        stratum_id = _text(values, "stratum_id", "层位")
        stratum = self._must_be(STRATA, stratum_id)
        if stratum["trench_id"] != trench_id:
            raise ValidationError("出土事件所属层位与探方不一致")
        photo_refs = values.get("photo_refs", [])
        if not isinstance(photo_refs, list) or not all(isinstance(item, str) and item.strip() for item in photo_refs):
            raise ValidationError("田野照片编号必须是非空字符串列表")
        payload = {
            "project_id": trench["project_id"],
            "site_id": trench["site_id"],
            "trench_id": trench_id,
            "feature_id": feature_id,
            "stratum_id": stratum_id,
            "code": _text(values, "code", "出土事件编号"),
            "occurred_at": canonical_instant(_text(values, "occurred_at", "出土时间")),
            "recorded_by": _text(values, "recorded_by", "田野记录人"),
            "field_note": _text(values, "field_note", "田野记录"),
            "photo_refs": [item.strip() for item in photo_refs],
            "record_kind": "field",
        }
        return self._create(context, RECOVERY_EVENTS, payload, request_key=request_key)

    def register_find(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        """遗物登记：编号、重量与出土事件绑定，重量是后续交接核对的基准。"""
        recovery_id = _text(values, "recovery_event_id", "出土事件")
        recovery = self._must_be(RECOVERY_EVENTS, recovery_id)
        weight = values.get("registered_weight")
        if weight is not None:
            _weight(weight)
        payload = {
            "project_id": recovery["project_id"],
            "site_id": recovery["site_id"],
            "trench_id": recovery["trench_id"],
            "feature_id": recovery["feature_id"],
            "stratum_id": recovery["stratum_id"],
            "recovery_event_id": recovery_id,
            "code": _text(values, "code", "遗物编号"),
            "material": _text(values, "material", "材质"),
            "registered_weight": None if weight is None else str(weight).strip(),
            "weight_uom": _optional_text(values, "weight_uom") or "g",
            "holding_org": _text(values, "holding_org", "当前保管机构"),
        }
        return self._create(context, FINDS, payload, request_key=request_key)

    # ------------------------------------------------------------ 保管与修复

    def create_storage(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        payload = {
            "project_id": _text(values, "project_id", "项目"),
            "code": _text(values, "code", "保管地点编号"),
            "org": _text(values, "org", "保管机构"),
            "kind": _text(values, "kind", "地点类型"),
            "temperature_c": _optional_text(values, "temperature_c"),
            "humidity_pct": _optional_text(values, "humidity_pct"),
        }
        return self._create(context, STORAGE, payload, request_key=request_key)

    def open_repair(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        find_id = _text(values, "find_id", "遗物")
        find = self._must_be(FINDS, find_id)
        payload = {
            "project_id": find["project_id"],
            "find_id": find_id,
            "storage_id": _text(values, "storage_id", "修复地点"),
            "repaired_by": _text(values, "repaired_by", "修复负责人"),
            "started_at": canonical_instant(_text(values, "started_at", "开始时间")),
            "description": _text(values, "description", "修复说明"),
        }
        return self._create(context, REPAIRS, payload, request_key=request_key, state="open")

    def close_repair(self, context: AccessContext, repair_id: str, *, expected_version: int, request_key: str) -> dict:
        context.require(f"write:{REPAIRS}")
        record = self.repository.get(REPAIRS, repair_id)
        if record["state"] != "open":
            raise ValidationError("只有进行中的修复可以关闭")
        changes = {"state": "closed", "ended_at": self.repository.clock.now()}
        return self.repository.update(REPAIRS, repair_id, changes, actor=context.actor_id,
                                     expected_version=expected_version, request_key=request_key)

    # ------------------------------------------------------------ 查询

    def get(self, entity_type: str, entity_id: str) -> dict:
        return self.repository.get(entity_type, entity_id)

    def finds_of_feature(self, feature_id: str) -> list[dict]:
        return [row for row in self.repository.list(FINDS, limit=500) if row.get("feature_id") == feature_id]

    def _must_be(self, entity_type: str, entity_id: str, *, project_id: str | None = None) -> dict:
        record = self.repository.get(entity_type, entity_id)
        if project_id is not None and record.get("project_id") != project_id:
            raise ValidationError(f"{entity_id} 不属于项目 {project_id}")
        return record


def _optional_text(values: Mapping[str, object], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{key} 必须是非空字符串")
    return value.strip()


def _weight(value: object) -> None:
    from decimal import Decimal, InvalidOperation
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("重量格式错误") from exc
    if not number.is_finite() or number <= 0:
        raise ValidationError("重量必须是正有限数")
