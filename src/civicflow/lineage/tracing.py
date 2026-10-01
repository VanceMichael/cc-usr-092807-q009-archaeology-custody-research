"""结论反查：从一句研究结论回溯到墓葬、样本、实验过程、跨国责任与发表许可。"""

from __future__ import annotations

from dataclasses import dataclass

from ..repository import EntityRepository
from ..security import AccessContext
from .records import ArtifactService
from .research import PERMITS, REQUESTS, RESULTS, VERSIONS
from .samples import SampleService


@dataclass(frozen=True)
class TraceService:
    """论文审核视角的谱系反查。"""

    _repository: EntityRepository
    _artifacts: ArtifactService
    _samples: SampleService

    def trace_conclusion(self, context: AccessContext, version_id: str, *, purpose: str | None = None) -> dict:
        """从研究版本反查完整证据链。"""
        context.require("read:research")
        version = self._repository.get(VERSIONS, version_id)
        evidence = []
        sample_ids: list[str] = []
        for ref in version["evidence"]:
            record = self._repository.get(ref["entity_type"], ref["entity_id"])
            entry = {"entity_type": ref["entity_type"], "record": record}
            if ref["entity_type"] == RESULTS:
                entry["request"] = self._repository.get(REQUESTS, record["request_id"])
                sample_ids.append(record["sample_id"])
            elif ref["entity_type"] == "artifacts":
                entry["record"] = self._artifacts.get(context, ref["entity_id"], purpose=purpose)
            evidence.append(entry)
        chain = self._sample_chain(sample_ids)
        artifact = None
        if chain:
            artifact = self._artifacts.get(context, chain[0]["artifact_id"], purpose=purpose)
        context_chain = self._field_context(artifact) if artifact else {}
        handovers = self._handovers_for(sample_ids)
        permits = [permit for permit in self._repository.list(PERMITS, limit=500) if version_id in permit.get("version_ids", [])]
        return {
            "conclusion": version,
            "superseded_chain": self._superseded_chain(version),
            "evidence": evidence,
            "sample_chain": chain,
            "sample_movements": {sample_id: self._samples.custody.movements_of(sample_id) for sample_id in sample_ids},
            "artifact": artifact,
            "field_context": context_chain,
            "handovers": handovers,
            "permits": permits,
        }

    def _sample_chain(self, sample_ids: list[str]) -> list[dict]:
        chain: list[dict] = []
        seen: set[str] = set()
        for sample_id in sample_ids:
            current = sample_id
            while current and current not in seen:
                seen.add(current)
                sample = self._repository.get("samples", current)
                chain.append(sample)
                current = str(sample.get("parent_id") or "")
        chain.sort(key=lambda item: (item.get("parent_id") is not None, item["created_at"], item["entity_id"]))
        return chain

    def _field_context(self, artifact: dict) -> dict:
        event = self._repository.get("excavations", artifact["event_id"])
        feature = self._repository.get("features", event["feature_id"])
        trench = self._repository.get("trenches", feature["trench_id"])
        site = self._repository.get("sites", trench["site_id"])
        context = {"excavation": event, "feature": feature, "trench": trench, "site": site}
        if event.get("stratum_id"):
            context["stratum"] = self._repository.get("strata", event["stratum_id"])
        return context

    def _handovers_for(self, sample_ids: list[str]) -> list[dict]:
        wanted = set(sample_ids)
        if not wanted:
            return []
        return [handover for handover in self._repository.list("handovers", limit=500) if wanted & {line["sample_id"] for line in handover.get("items", [])}]

    def _superseded_chain(self, version: dict) -> list[dict]:
        chain = []
        current = version.get("supersedes")
        while current:
            older = self._repository.get(VERSIONS, str(current))
            chain.append(older)
            current = older.get("supersedes")
        return chain
