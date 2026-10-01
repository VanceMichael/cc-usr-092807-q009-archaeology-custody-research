"""持久化监控：逾期返还与保存条件巡检。

检查项以 scheduled_jobs 落库，进程重启后 claim_due 仍能取到；
每次发现写入 monitoring_findings，保存条件巡检按周期自动续排下一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Mapping

from .database import Database
from .identifiers import new_id
from .jobs import JobQueue
from .jsonutil import canonical_json
from .repository import EntityRepository
from .timeutil import Clock, parse_instant


HANDOVER_TYPE = "arch_handovers"
STORAGE_TYPE = "storage_locations"


@dataclass(frozen=True)
class MonitoringService:
    repository: EntityRepository
    database: Database
    clock: Clock
    jobs: JobQueue

    def schedule_storage_check(self, *, storage_id: str, first_run_at: str, interval_seconds: int = 86400) -> str:
        if interval_seconds < 60:
            raise ValueError("巡检间隔不能短于 60 秒")
        location = self.repository.get(STORAGE_TYPE, storage_id)
        return self.jobs.schedule(job_type="storage_check", subject_id=storage_id, run_at=first_run_at,
                                  payload={"storage_id": storage_id, "project_id": location["project_id"],
                                           "interval_seconds": interval_seconds})

    def record_reading(self, *, storage_id: str, temperature_c: object | None = None,
                       humidity_pct: object | None = None, recorded_by: str) -> dict:
        self.repository.get(STORAGE_TYPE, storage_id)
        temp = _number(temperature_c, "温度") if temperature_c is not None else None
        humidity = _number(humidity_pct, "湿度") if humidity_pct is not None else None
        reading_id = new_id("reading")
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO storage_readings(reading_id,storage_id,temperature_c,humidity_pct,read_at,"
                "recorded_by,state) VALUES(?,?,?,?,?,?,?)",
                (reading_id, storage_id, None if temp is None else str(temp),
                 None if humidity is None else str(humidity), self.clock.now(), recorded_by, "recorded"))
        return {"reading_id": reading_id, "storage_id": storage_id,
                "temperature_c": None if temp is None else str(temp),
                "humidity_pct": None if humidity is None else str(humidity)}

    def run_due(self, *, limit: int = 20) -> list[dict]:
        """领取并处理所有到期任务。任务与发现均已落库，调用本身可在重启后重复进行。"""
        handled = []
        for job in self.jobs.claim_due(limit=limit):
            try:
                if job["job_type"] == "return_due":
                    finding = self._check_return(job)
                elif job["job_type"] == "storage_check":
                    finding = self._check_storage(job)
                else:
                    finding = None
                self.jobs.finish(job["job_id"])
                handled.append({"job_id": job["job_id"], "job_type": job["job_type"],
                                "finding": finding, "status": "succeeded"})
            except Exception as exc:  # 巡检失败必须保留并退避重试，不能静默丢失
                retry_at = (parse_instant(self.clock.now()) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
                self.jobs.retry(job["job_id"], error=str(exc), retry_at=retry_at)
                handled.append({"job_id": job["job_id"], "job_type": job["job_type"],
                                "status": "retry", "error": str(exc)})
        return handled

    def findings(self, *, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM monitoring_findings"; params: list[object] = []
        if kind is not None:
            sql += " WHERE kind=?"; params.append(kind)
        sql += " ORDER BY created_at,finding_id"
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    # ---------------------------------------------------------------- 内部

    def _check_return(self, job: Mapping[str, object]) -> dict | None:
        handover_id = job["subject_id"]
        handover = self.repository.get(HANDOVER_TYPE, handover_id)
        if handover["state"] == "returned":
            return None
        if handover["kind"] == "outbound" and self._returned_back(handover):
            return None
        return self._raise_finding(kind="overdue_return", subject_type="arch_handovers", subject_id=handover_id,
                                   job_id=job["job_id"],
                                   detail={"package_code": handover["package_code"],
                                           "from_org": handover["from_org"], "to_org": handover["to_org"],
                                           "due_back_at": handover.get("due_back_at"),
                                           "state": handover["state"],
                                           "message": "超过应返还时间，样本仍未返还发出方"})

    def _returned_back(self, outbound: Mapping[str, object]) -> bool:
        """原物已通过 kind=return 的交接单全部返还发出方。"""
        outbound_samples = {item["sample_id"] for item in outbound["items"]}
        returned_samples: set[str] = set()
        for handover in self.repository.list(HANDOVER_TYPE, limit=500):
            if (handover["kind"] == "return" and handover["state"] == "returned"
                    and handover["to_org"] == outbound["from_org"]):
                returned_samples.update(item["sample_id"] for item in handover["items"])
        return outbound_samples <= returned_samples

    def _check_storage(self, job: Mapping[str, object]) -> dict | None:
        import json
        payload = json.loads(job["payload_json"])
        storage_id = payload["storage_id"]
        location = self.repository.get(STORAGE_TYPE, storage_id)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM storage_readings WHERE storage_id=? ORDER BY read_at DESC,rowid DESC LIMIT 1",
                (storage_id,)).fetchone()
        problems = []
        if row is None:
            problems.append("没有任何保存条件读数")
            latest = None
        else:
            latest = {"temperature_c": row["temperature_c"], "humidity_pct": row["humidity_pct"],
                      "read_at": row["read_at"]}
            if row["temperature_c"] is not None and location.get("temperature_c") is not None:
                target = _bounds(location["temperature_c"])
                value = Decimal(row["temperature_c"])
                if value < target[0] or value > target[1]:
                    problems.append(f"温度 {value}℃ 超出允许区间 {target[0]}~{target[1]}℃")
            if row["humidity_pct"] is not None and location.get("humidity_pct") is not None:
                target = _bounds(location["humidity_pct"])
                value = Decimal(row["humidity_pct"])
                if value < target[0] or value > target[1]:
                    problems.append(f"湿度 {value}% 超出允许区间 {target[0]}~{target[1]}%")
        # 周期巡检：本次处理成功后续排下一次（任务记录本身保留历史）。
        interval = int(payload.get("interval_seconds", 86400))
        next_at = (parse_instant(self.clock.now()) + timedelta(seconds=interval)).isoformat().replace("+00:00", "Z")
        self.jobs.schedule(job_type="storage_check", subject_id=storage_id, run_at=next_at, payload=payload)
        if not problems:
            return None
        return self._raise_finding(kind="storage_condition", subject_type="storage_locations",
                                   subject_id=storage_id, job_id=job["job_id"],
                                   detail={"storage_id": storage_id, "latest": latest,
                                           "limits": {"temperature_c": location.get("temperature_c"),
                                                      "humidity_pct": location.get("humidity_pct")},
                                           "problems": problems})

    def _raise_finding(self, *, kind: str, subject_type: str, subject_id: str, job_id: str, detail: dict) -> dict:
        finding_id = new_id("finding")
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO monitoring_findings(finding_id,kind,subject_type,subject_id,detail_json,job_id,"
                "created_at) VALUES(?,?,?,?,?,?,?)",
                (finding_id, kind, subject_type, subject_id, canonical_json(detail), job_id, self.clock.now()))
        return {"finding_id": finding_id, "kind": kind, "subject_id": subject_id, "detail": detail}


def _number(value: object, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label}格式错误") from exc
    if not number.is_finite():
        raise ValueError(f"{label}必须是有限数")
    return number


def _bounds(spec: str) -> tuple[Decimal, Decimal]:
    """规格支持 'min:max' 区间或单值（单值即 ±2 的宽松区间）。"""
    text = str(spec).strip()
    if ":" in text:
        low, high = text.split(":", 1)
        return Decimal(low), Decimal(high)
    center = Decimal(text)
    return center - 2, center + 2
