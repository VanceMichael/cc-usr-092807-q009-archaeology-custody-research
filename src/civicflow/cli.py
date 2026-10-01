"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from datetime import timedelta
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .errors import CivicFlowError
from .lineage import Lineage
from .security import AccessContext
from .timeutil import parse_instant


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def demo(app: CivicFlow) -> dict:
    context = AccessContext.system("demo-operator")
    cases = CaseService(app.repository)
    created = cases.create(context, {"case_type": "协同事项", "subject": "示例联合处置", "owner_org": "org:demo", "priority": "high", "opened_at": app.clock.now()}, request_key="demo-case")
    accepted = app.inbox.receive(source="demo", source_key=created["entity_id"], sequence=1, payload={"kind": "opened"}, occurred_at=app.clock.now())
    reservation = app.reservations.reserve(resource_id="room:joint", subject_id=created["entity_id"], quantity=2, capacity=10, start_at="2026-09-28T10:00:00+08:00", end_at="2026-09-28T11:00:00+08:00", actor=context.actor_id)
    debit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="debit", reference="demo-debit", actor=context.actor_id)
    credit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="credit", reference="demo-credit", actor=context.actor_id)
    message = app.outbox.enqueue(topic="case.opened", aggregate_id=created["entity_id"], payload={"case_id": created["entity_id"]})
    return {"case": created, "inbox": accepted, "reservation": reservation, "entries": [debit, credit], "balance": app.ledger.balance("demo", currency="CNY"), "message_id": message, "verification": app.verify()}


def lineage_demo(app: CivicFlow) -> dict:
    """奥佐德墓地牙齿样本：登记、分装、跨国交接、测年、发表与反查。"""
    lineage = Lineage.open(app)
    operator = AccessContext.system("lineage-operator")
    customs = AccessContext(actor_id="person:customs-cn", permissions=frozenset({"scan:handovers", "read:handovers"}))
    applicant = AccessContext(actor_id="person:uz-researcher", permissions=frozenset({"submit:requests", "read:research"}), scopes=frozenset({"project:proj-c14"}))
    approver = AccessContext(actor_id="person:cn-lead", permissions=frozenset({"approve:requests", "read:research"}))
    now = parse_instant(app.clock.now())

    site = lineage.sites.create(operator, {"name": "奥佐德墓地", "code": "OZD", "country": "乌兹别克斯坦"}, request_key="ld-site")
    trench = lineage.trenches.create(operator, {"site_id": site["entity_id"], "code": "T1", "opened_at": app.clock.now()}, request_key="ld-trench")
    feature = lineage.features.create(operator, {"trench_id": trench["entity_id"], "kind": "tomb", "code": "M12"}, request_key="ld-feature")
    stratum = lineage.strata.create(operator, {"trench_id": trench["entity_id"], "code": "L3", "description": "扰乱层下原生堆积"}, request_key="ld-stratum")
    event = lineage.excavations.create(operator, {"feature_id": feature["entity_id"], "stratum_id": stratum["entity_id"], "occurred_at": app.clock.now(), "recorder": "person:field-uz", "field_notes": "墓葬M12人骨左侧上颌第二臼齿，现场拍照编号OZD-M12-P07"}, request_key="ld-event")
    artifact = lineage.artifacts.create(operator, {"event_id": event["entity_id"], "field_number": "OZD-M12-P07", "kind": "牙齿", "material": "人骨", "material_class": "human_remains", "sensitive": True, "weight": "2.5", "description": "完整臼齿，牙根未断"}, request_key="ld-artifact")

    uz_store = lineage.locations.create(operator, {"org": "org:uz-iu", "facility": "乌方考古所库房", "conditions": {"temperature_c": 21, "humidity_pct": 50}}, request_key="ld-loc-uz")
    cn_lab = lineage.locations.create(operator, {"org": "org:cn-lab", "facility": "中方科技考古实验室", "conditions": {"temperature_c": 22, "humidity_pct": 48}}, request_key="ld-loc-cn")

    parent = lineage.samples.register(operator, artifact_id=artifact["entity_id"], label="OZD-M12-P07-TOOTH", unit="mg", quantity="2500", location_id=uz_store["entity_id"], request_key="ld-parent")
    child = lineage.samples.split(operator, parent_id=parent["sample"]["entity_id"], label="OZD-M12-P07-A", quantity="500", location_id=cn_lab["entity_id"], request_key="ld-split")

    grant = lineage.grants.grant(operator, project="proj-c14", purpose="碳十四测年", material_class="human_remains", grantee_org="org:uz-iu", expires_at=(now + timedelta(days=365)).isoformat(), request_key="ld-grant")

    due_at = (now + timedelta(days=30)).isoformat()
    handover = lineage.handovers.prepare(operator, package_no="PKG-OZD-M12-001", from_org="org:uz-iu", to_org="org:cn-lab", responsible="person:escort-uz", items=[{"sample_id": child["sample"]["entity_id"], "quantity": "500", "weight": "500"}], expected_weight="500", permit_ref="乌文物出境许可2026-118", due_at=due_at, request_key="ld-handover")
    lineage.handovers.dispatch(operator, handover["handover"]["entity_id"], request_key="ld-dispatch")
    scan_first = lineage.handovers.scan(customs, package_no="PKG-OZD-M12-001", weight="500", to_org="org:cn-lab", request_key="ld-scan-1")
    scan_again = lineage.handovers.scan(customs, package_no="PKG-OZD-M12-001", weight="500", to_org="org:cn-lab", request_key="ld-scan-2")

    spare = lineage.samples.split(operator, parent_id=parent["sample"]["entity_id"], label="OZD-M12-P07-B", quantity="300", location_id=cn_lab["entity_id"], request_key="ld-split-2")
    second = lineage.handovers.prepare(operator, package_no="PKG-OZD-M12-002", from_org="org:uz-iu", to_org="org:cn-lab", responsible="person:escort-uz", items=[{"sample_id": spare["sample"]["entity_id"], "quantity": "300", "weight": "300"}], expected_weight="300", permit_ref="乌文物出境许可2026-119", due_at=due_at, request_key="ld-handover-2")
    conflict_error = ""
    try:
        lineage.handovers.scan(customs, package_no="PKG-OZD-M12-002", weight="260", to_org="org:cn-lab", request_key="ld-scan-3")
    except CivicFlowError as exc:
        conflict_error = str(exc)
    quarantined = lineage.handovers.get(operator, second["handover"]["entity_id"])

    request = lineage.requests.submit(applicant, project="proj-c14", purpose="碳十四测年", sample_id=child["sample"]["entity_id"], quantity="200", method="AMS-14C", request_key="ld-request")
    approved = lineage.requests.approve(approver, request["entity_id"], request_key="ld-approve")
    result = lineage.results.record(operator, request_id=request["entity_id"], method="AMS-14C", lab="中方加速器质谱实验室", consumed_quantity="200", result={"bp": "2450±30", "calibrated": "公元前750-401年(2σ)"}, request_key="ld-result")

    rival = AccessContext(actor_id="person:other-institute", permissions=frozenset({"submit:requests", "read:research"}), scopes=frozenset({"project:proj-c14"}))
    rival_request = lineage.requests.submit(rival, project="proj-c14", purpose="碳十四测年", sample_id=child["sample"]["entity_id"], quantity="400", method="AMS-14C", request_key="ld-rival")
    blocked_error = ""
    try:
        lineage.requests.approve(approver, rival_request["entity_id"], request_key="ld-rival-approve")
    except CivicFlowError as exc:
        blocked_error = str(exc)

    version = lineage.versions.append(operator, subject_type="features", subject_id=feature["entity_id"], kind="dating", conclusion="墓葬M12年代为公元前8-5世纪", evidence=[{"entity_type": "results", "entity_id": result["result"]["entity_id"]}], request_key="ld-version")
    permit = lineage.permits.issue(operator, project="proj-c14", scope="联合考古报告与期刊论文", version_ids=[version["entity_id"]], request_key="ld-permit")
    published = lineage.versions.publish(operator, version["entity_id"], permit_id=permit["entity_id"], request_key="ld-publish")

    condition_job = lineage.jobs.schedule_condition_check(operator, location_id=cn_lab["entity_id"], interval_hours=24, required={"temperature_c": [18, 24], "humidity_pct": [40, 60]})
    trace = lineage.trace.trace_conclusion(operator, version["entity_id"], purpose="碳十四测年")

    return {
        "site": site, "trench": trench, "feature": feature, "stratum": stratum, "excavation": event, "artifact": artifact,
        "parent_sample": parent["sample"], "child_sample": child["sample"],
        "grant": grant, "handover": handover["handover"], "return_check_job": handover["return_check_job"],
        "scan_first": scan_first["status"], "scan_duplicate": scan_again["status"],
        "weight_conflict": {"error": conflict_error, "handover_state": quarantined["state"]},
        "request": approved["request"], "result": result["result"],
        "duplicate_consumption_blocked": blocked_error,
        "version": published, "permit": permit, "condition_check_job": condition_job,
        "trace": trace, "verification": app.verify(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("lineage-demo")
    commands.add_parser("run-lineage-jobs")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    args = parser.parse_args(argv)
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo": emit(demo(app))
    elif args.command == "lineage-demo": emit(lineage_demo(app))
    elif args.command == "run-lineage-jobs": emit(Lineage.open(app).jobs.run_due(AccessContext.system("cli")))
    elif args.command == "verify": emit(app.verify())
    elif args.command == "list-cases": emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
