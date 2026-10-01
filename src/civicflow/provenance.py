"""证据反查：从一句研究结论回溯到墓葬、样本、实验过程、跨国责任与发表许可。

trace_version(version_id) 沿 research_versions.evidence_ids 与 based_on_results
展开全部责任链；trace_claim 支持直接用结论文本或其片段定位研究版本。
"""

from __future__ import annotations

from dataclasses import dataclass

from .database import Database
from .errors import NotFoundError, ValidationError
from .repository import EntityRepository
from .samples import SAMPLE_TYPE
from .timeutil import Clock


FIND_TYPE = "finds"
RECOVERY_TYPE = "arch_recovery_events"
FEATURE_TYPE = "arch_features"
REQUEST_TYPE = "research_requests"
VERSION_TYPE = "research_versions"
RESULT_TYPE = "test_results"
HANDOVER_TYPE = "arch_handovers"
PERMISSION_TYPE = "publication_permissions"
STORAGE_TYPE = "storage_locations"
REPAIR_TYPE = "repairs"


@dataclass(frozen=True)
class ProvenanceService:
    repository: EntityRepository
    database: Database
    clock: Clock

    def trace_claim(self, text: str) -> dict:
        """用结论原文（或其唯一片段）定位研究版本并反查。"""
        needle = text.strip()
        if not needle:
            raise ValidationError("结论文本不能为空")
        matches = [row for row in self.repository.list(VERSION_TYPE, limit=500) if needle in str(row.get("claim", ""))]
        if not matches:
            raise NotFoundError("没有研究结论包含该文本")
        if len(matches) > 1:
            raise ValidationError(f"该文本匹配到 {len(matches)} 条结论，请使用更精确的原文或 trace_version")
        return self.trace_version(matches[0]["entity_id"])

    def trace_version(self, version_id: str) -> dict:
        version = self.repository.get(VERSION_TYPE, version_id)
        sample_ids: list[str] = []
        results = []
        field_refs = []
        other_versions = []
        for ref in version.get("evidence_ids", []):
            entity_type, entity_id = ref["entity_type"], ref["entity_id"]
            if entity_type == SAMPLE_TYPE:
                sample_ids.append(entity_id)
            elif entity_type == RESULT_TYPE:
                results.append(entity_id)
            elif entity_type in (FIND_TYPE, RECOVERY_TYPE):
                field_refs.append(ref)
            elif entity_type == VERSION_TYPE:
                other_versions.append(entity_id)
        for result_id in version.get("based_on_results", []):
            if result_id not in results:
                results.append(result_id)

        result_records = [self.repository.get(RESULT_TYPE, result_id) for result_id in results]
        for record in result_records:
            for sample_id in record.get("sample_ids", []):
                if sample_id not in sample_ids:
                    sample_ids.append(sample_id)

        samples = [self.repository.get(SAMPLE_TYPE, sample_id) for sample_id in sample_ids]
        sample_chains = [self._sample_chain(sample) for sample in samples]

        finds = {}
        for sample in samples:
            finds.setdefault(sample["find_id"], self.repository.get(FIND_TYPE, sample["find_id"]))
        features = {}
        for find in finds.values():
            feature_id = find.get("feature_id")
            if feature_id and feature_id not in features:
                features[feature_id] = self.repository.get(FEATURE_TYPE, feature_id)
        recoveries = {find_id: self.repository.get(RECOVERY_TYPE, find["recovery_event_id"])
                      for find_id, find in finds.items()}

        requests = {}
        for record in result_records:
            request_id = record["request_id"]
            requests.setdefault(request_id, self.repository.get(REQUEST_TYPE, request_id))

        custody = self._custody_chain(sample_ids)
        publications = self._publications(version_id)

        return {
            "conclusion": {"version_id": version_id, "kind": version["kind"], "claim": version["claim"],
                           "author": version["author"], "project_id": version["project_id"]},
            "burials": [self._feature_brief(feature) for feature in features.values() if feature["kind"] == "burial"],
            "features": [self._feature_brief(feature) for feature in features.values()],
            "field_records": [{"find_id": find_id, "find_code": find["code"],
                               "recovery": {"event_id": recoveries[find_id]["entity_id"],
                                            "code": recoveries[find_id]["code"],
                                            "occurred_at": recoveries[find_id]["occurred_at"],
                                            "recorded_by": recoveries[find_id]["recorded_by"],
                                            "photo_refs": recoveries[find_id]["photo_refs"],
                                            "field_note": recoveries[find_id]["field_note"]}}
                              for find_id, find in finds.items()],
            "samples": sample_chains,
            "experiments": [{"result_id": record["entity_id"], "request_id": record["request_id"],
                             "lab_org": record["lab_org"], "method": record["method"],
                             "values": record["values"], "raw_ref": record["raw_ref"],
                             "issued_at": record["issued_at"],
                             "approved_by": requests[record["request_id"]].get("reviewer"),
                             "applicant": requests[record["request_id"]]["applicant"]}
                            for record in result_records],
            "requests": [{"request_id": record["entity_id"], "applicant": record["applicant"],
                          "reviewer": record.get("reviewer"), "purpose": record["purpose"],
                          "state": record["state"], "materials": record["materials"]}
                         for record in requests.values()],
            "cross_border_responsibility": custody,
            "publication_permissions": publications,
            "superseded_by_versions": other_versions,
        }

    # ---------------------------------------------------------------- 内部

    def _sample_chain(self, sample: dict) -> dict:
        import json
        with self.database.connect() as connection:
            events = [dict(row) for row in connection.execute(
                "SELECT event_type,delta_amount,uom,actor_id,transfer_id,request_id,occurred_at,note "
                "FROM sample_lineage_events WHERE sample_id=? ORDER BY occurred_at,rowid",
                (sample["entity_id"],)).fetchall()]
        repairs = [row for row in self.repository.list(REPAIR_TYPE, limit=500) if row.get("find_id") == sample["find_id"]]
        return {"sample_id": sample["entity_id"], "code": sample["code"], "material": sample["material"],
                "sensitive": sample["sensitive"], "state": sample["state"],
                "initial": sample["amount_initial"], "available": sample["amount_available"], "uom": sample["uom"],
                "parent_sample_id": sample["parent_sample_id"], "root_sample_id": sample["root_sample_id"],
                "find_id": sample["find_id"], "custodian_org": sample.get("custodian_org"),
                "lineage_events": events,
                "repairs": [{"repair_id": row["entity_id"], "repaired_by": row["repaired_by"],
                             "state": row["state"], "description": row["description"],
                             "started_at": row["started_at"], "ended_at": row.get("ended_at")}
                            for row in repairs]}

    def _custody_chain(self, sample_ids: list[str]) -> list[dict]:
        wanted = set(sample_ids)
        chain = []
        for handover in self.repository.list(HANDOVER_TYPE, limit=500):
            items = [item for item in handover["items"] if item["sample_id"] in wanted]
            if not items:
                continue
            with self.database.connect() as connection:
                scans = [dict(row) for row in connection.execute(
                    "SELECT scan_id,package_code,sample_id,observed_code,observed_weight,scanned_by,scanned_at "
                    "FROM sample_scans WHERE transfer_id=? ORDER BY scanned_at", (handover["entity_id"],)).fetchall()]
            chain.append({"handover_id": handover["entity_id"], "kind": handover["kind"],
                          "state": handover["state"], "from_org": handover["from_org"],
                          "to_org": handover["to_org"], "package_code": handover["package_code"],
                          "customs_ref": handover["customs_ref"], "due_back_at": handover.get("due_back_at"),
                          "items": items, "scans": scans})
        return chain

    def _publications(self, version_id: str) -> list[dict]:
        result = []
        for permission in self.repository.list(PERMISSION_TYPE, limit=500):
            if version_id in permission.get("version_ids", []):
                result.append({"permission_id": permission["entity_id"], "title": permission["title"],
                               "applicant": permission["applicant"], "scope": permission["scope"],
                               "granted_at": permission["granted_at"],
                               "covered_versions": permission["version_ids"]})
        return result

    @staticmethod
    def _feature_brief(feature: dict) -> dict:
        return {"feature_id": feature["entity_id"], "code": feature["code"], "kind": feature["kind"],
                "site_id": feature["site_id"], "trench_id": feature["trench_id"]}
