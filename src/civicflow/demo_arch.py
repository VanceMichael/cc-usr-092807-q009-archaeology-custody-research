"""奥佐德墓地牙齿样本的端到端演示：田野谱系、分装守恒、跨国交接、
冲突隔离、授权回避、研究版本追加、持久化监控与结论反查。"""

from __future__ import annotations

from .errors import ConflictError
from .security import AccessContext


def run(app) -> dict:
    admin = AccessContext.system("admin")
    li = AccessContext.system("user:li")          # 中方申请人
    karimov = AccessContext.system("user:karimov")  # 乌方审批人

    # ------------------------------------------------------------ 田野谱系
    site = app.field.create_site(admin, {"project_id": "proj:uz-cn", "code": "OZD", "name": "奥佐德墓地"}, request_key="d:site")
    trench = app.field.create_trench(admin, {"site_id": site["entity_id"], "code": "T3"}, request_key="d:trench")
    burial = app.field.create_feature(admin, {"trench_id": trench["entity_id"], "kind": "burial", "code": "M12"}, request_key="d:burial")
    stratum = app.field.create_stratum(admin, {"trench_id": trench["entity_id"], "feature_id": burial["entity_id"], "code": "L2", "sequence": 2}, request_key="d:stratum")
    recovery = app.field.record_recovery(admin, {
        "trench_id": trench["entity_id"], "feature_id": burial["entity_id"], "stratum_id": stratum["entity_id"],
        "code": "OZD-M12-EX01", "occurred_at": app.clock.now(), "recorded_by": "user:field",
        "field_note": "墓葬 M12 人骨颌骨左侧出土单枚牙齿，原位埋藏。",
        "photo_refs": ["photo:M12-017", "photo:M12-018"]}, request_key="d:recovery")
    find = app.field.register_find(admin, {
        "recovery_event_id": recovery["entity_id"], "code": "OZD-M12-T01", "material": "human_remains",
        "registered_weight": "5.00", "weight_uom": "g", "holding_org": "org:uz-archive"}, request_key="d:find")

    # ------------------------------------------------------------ 母样与分装
    mother = app.samples.register(li, {"project_id": "proj:uz-cn", "find_id": find["entity_id"],
                                       "code": "OZD-M12-T01", "material": "human_remains",
                                       "uom": "g", "amount": "5.00", "sensitive": True}, request_key="d:mother")
    split = app.samples.split(li, mother["entity_id"], [
        {"code": "OZD-M12-T01-A1", "material": "human_remains", "amount": "2.00", "purpose": "C14 测年"},
        {"code": "OZD-M12-T01-A2", "material": "human_remains", "amount": "1.50", "purpose": "他机构复测"}], request_key="d:split")
    a1 = split["children"][0]["sample_id"]
    a2 = split["children"][1]["sample_id"]

    # ------------------------------------------------------------ 保管与授权
    vault = app.field.create_storage(admin, {"project_id": "proj:uz-cn", "code": "VAULT-A", "org": "org:uz-archive",
                                             "kind": "vault", "temperature_c": "2:8", "humidity_pct": "40:60"}, request_key="d:vault")
    app.monitoring.schedule_storage_check(storage_id=vault["entity_id"], first_run_at="2026-09-01T00:00:00Z")
    app.research.grant_access(admin, {"project_id": "proj:uz-cn", "subject_id": "user:li",
                                      "purpose": "C14 测年", "material_scope": "human_remains",
                                      "valid_from": "2026-09-01T00:00:00Z"}, request_key="d:grant")

    # ----------------------------------------------------- 研究申请与回避审批
    request = app.research.submit_request(li, {"project_id": "proj:uz-cn", "purpose": "C14 测年",
                                               "method": "AMS-C14", "sample_ids": [a1],
                                               "consume_plan": {a1: "0.50"}}, request_key="d:req")
    approval = app.research.decide_request(karimov, request["entity_id"], decision="approved",
                                          reason="联合考古队年度计划内项目，授权齐备",
                                          expected_version=1, request_key="d:approve")

    # ----------------------------- 跨国交接：主单正常扫描收货（重复扫描只确认）
    outbound = app.custody.create_handover(admin, {
        "project_id": "proj:uz-cn", "kind": "outbound", "from_org": "org:uz-archive", "to_org": "org:cn-lab",
        "package_code": "PKG-2026-0091", "customs_ref": "customs:UZ-CN-7781",
        "due_back_at": "2026-09-19T10:00:00+08:00",
        "items": [{"sample_id": a1, "declared_code": "OZD-M12-T01-A1", "declared_weight": "2.00"}]}, request_key="d:out")
    app.custody.dispatch(admin, outbound["entity_id"], expected_version=1, request_key="d:dispatch")
    scan1 = app.custody.scan(admin, outbound["entity_id"], package_code="PKG-2026-0091", sample_id=a1,
                             observed_code="OZD-M12-T01-A1", observed_weight="2.00")
    scan1_repeat = app.custody.scan(admin, outbound["entity_id"], package_code="PKG-2026-0091", sample_id=a1,
                                    observed_code="OZD-M12-T01-A1", observed_weight="2.00")
    received = app.custody.receive(admin, outbound["entity_id"], expected_version=2, request_key="d:receive")

    # ----------------------------- 另一机构的交接：重量与交接单不符，立即隔离
    other = app.custody.create_handover(admin, {
        "project_id": "proj:uz-cn", "kind": "outbound", "from_org": "org:uz-archive", "to_org": "org:other-lab",
        "package_code": "PKG-2026-0092", "customs_ref": "customs:UZ-XX-7782",
        "items": [{"sample_id": a2, "declared_code": "OZD-M12-T01-A2", "declared_weight": "1.50"}]}, request_key="d:other")
    quarantine_scan = None
    try:
        app.custody.scan(admin, other["entity_id"], package_code="PKG-2026-0092", sample_id=a2,
                         observed_code="OZD-M12-T01-A2", observed_weight="1.20")
    except ConflictError as exc:
        quarantine_scan = str(exc)

    # ------------------------------------------------------------ 检测与结论
    consumed = app.samples.consume(li, a1, "0.50", request_id=request["entity_id"], request_key="d:consume")
    result = app.research.record_result(li, {"request_id": request["entity_id"], "lab_org": "org:cn-lab",
                                             "method": "AMS-C14", "sample_ids": [a1],
                                             "values": {"age_bp": "2475", "sigma": "30",
                                                        "cal_bc_range": "780-540"},
                                             "raw_ref": "rawrun:CNL-2026-3320",
                                             "issued_at": app.clock.now()}, request_key="d:result")
    interpretation = app.research.add_interpretation(li, {
        "project_id": "proj:uz-cn", "kind": "dating",
        "claim": "奥佐德墓地 M12 墓葬的下葬年代约为公元前 780–540 年（AMS-C14，2σ）。",
        "evidence_ids": [
            {"entity_type": "samples", "entity_id": a1},
            {"entity_type": "test_results", "entity_id": result["entity_id"]},
            {"entity_type": "arch_recovery_events", "entity_id": recovery["entity_id"]}],
        "based_on_results": [result["entity_id"]]}, request_key="d:interp")
    permission = app.research.grant_publication(admin, {
        "project_id": "proj:uz-cn", "title": "奥佐德墓地 M12 测年与文化属性初报",
        "applicant": "user:li", "version_ids": [interpretation["entity_id"]],
        "scope": "联合报告与开放获取期刊", "granted_at": app.clock.now()}, request_key="d:pub")

    # 原物返还：另开 return 交接单，收货后保管责任回到乌方。
    returning = app.custody.create_handover(admin, {
        "project_id": "proj:uz-cn", "kind": "return", "from_org": "org:cn-lab", "to_org": "org:uz-archive",
        "package_code": "PKG-2026-0101", "customs_ref": "customs:UZ-CN-7790",
        "items": [{"sample_id": a1, "declared_code": "OZD-M12-T01-A1", "declared_weight": "1.50"}]}, request_key="d:return")
    app.custody.dispatch(admin, returning["entity_id"], expected_version=1, request_key="d:return-dispatch")
    app.custody.scan(admin, returning["entity_id"], package_code="PKG-2026-0101", sample_id=a1,
                     observed_code="OZD-M12-T01-A1", observed_weight="1.50")
    returned = app.custody.receive(admin, returning["entity_id"], expected_version=2, request_key="d:return-receive")

    # ------------------------------------------- 保存条件读数越界 + 到期监控
    app.monitoring.record_reading(storage_id=vault["entity_id"], temperature_c="14.2",
                                  humidity_pct="31", recorded_by="user:curator")
    findings = app.monitoring.run_due()
    trace = app.provenance.trace_version(interpretation["entity_id"])

    return {
        "field": {"site": site["entity_id"], "burial": burial["entity_id"], "find": find["entity_id"]},
        "lineage": {"mother": mother["entity_id"], "split": split,
                    "balance_a1": app.samples.balance(a1), "verify": app.samples.verify_lineage()},
        "request": {"id": request["entity_id"], "state": approval["state"], "reviewer": approval["reviewer"]},
        "handovers": {"outbound_scan": scan1["status"], "repeat_scan": scan1_repeat["status"],
                      "outbound_state": received["state"], "return_state": returned["state"],
                      "a2_quarantine": quarantine_scan,
                      "open_quarantine": app.custody.list_quarantine(state="open")},
        "research": {"consumed": consumed, "result": result["entity_id"],
                     "interpretation": interpretation["entity_id"], "permission": permission["entity_id"]},
        "monitoring": [{"job_type": item["job_type"], "status": item["status"],
                        "finding": None if item.get("finding") is None else item["finding"]["kind"]}
                       for item in findings],
        "trace_summary": {"conclusion": trace["conclusion"]["claim"],
                          "burials": [b["code"] for b in trace["burials"]],
                          "samples": [s["code"] for s in trace["samples"]],
                          "experiments": [e["result_id"] for e in trace["experiments"]],
                          "handovers": [h["handover_id"] for h in trace["cross_border_responsibility"]],
                          "permissions": [p["permission_id"] for p in trace["publication_permissions"]]},
    }
