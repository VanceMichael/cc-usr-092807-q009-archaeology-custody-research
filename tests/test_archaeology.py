from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from civicflow.security import AccessContext


class ArchaeologyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "arch.sqlite3")
        self.app = CivicFlow.open(self.db_path, fixed_now="2026-10-01T12:00:00+08:00")
        self.admin = AccessContext.system("admin")
        self.li = AccessContext("user:li", frozenset({"write:samples", "write:research"}))
        self.karimov = AccessContext("user:karimov", frozenset({"approve:research"}))
        self.wang = AccessContext("user:wang", frozenset({"write:samples", "write:research"}))

    def tearDown(self):
        self.temp.cleanup()

    # ------------------------------------------------------------ 辅助

    def _field_chain(self):
        site = self.app.field.create_site(self.admin, {"project_id": "p1", "code": "OZD", "name": "奥佐德"}, request_key="site")
        trench = self.app.field.create_trench(self.admin, {"site_id": site["entity_id"], "code": "T1"}, request_key="trench")
        pit = self.app.field.create_feature(self.admin, {"trench_id": trench["entity_id"], "kind": "pit", "code": "H1"}, request_key="pit")
        burial = self.app.field.create_feature(self.admin, {"trench_id": trench["entity_id"], "kind": "burial", "code": "M9"}, request_key="burial")
        stratum = self.app.field.create_stratum(self.admin, {"trench_id": trench["entity_id"], "feature_id": burial["entity_id"], "code": "L1", "sequence": 1}, request_key="stratum")
        recovery = self.app.field.record_recovery(self.admin, {
            "trench_id": trench["entity_id"], "feature_id": burial["entity_id"], "stratum_id": stratum["entity_id"],
            "code": "EX1", "occurred_at": "2026-09-01T09:00:00+08:00", "recorded_by": "user:field",
            "field_note": "原位出土", "photo_refs": ["ph:1"]}, request_key="recovery")
        find = self.app.field.register_find(self.admin, {
            "recovery_event_id": recovery["entity_id"], "code": "F1", "material": "human_remains",
            "registered_weight": "5.00", "holding_org": "org:uz"}, request_key="find")
        return {"site": site, "trench": trench, "pit": pit, "burial": burial,
                "stratum": stratum, "recovery": recovery, "find": find}

    def _mother(self, find, request_key="mother", amount="5.00"):
        return self.app.samples.register(self.li, {
            "project_id": "p1", "find_id": find["entity_id"], "code": "F1-S0",
            "material": "human_remains", "uom": "g", "amount": amount, "sensitive": True},
            request_key=request_key)

    # ------------------------------------------------------------ 田野

    def test_field_hierarchy_must_align(self):
        chain = self._field_chain()
        other_site = self.app.field.create_site(self.admin, {"project_id": "p1", "code": "OZD2", "name": "他处"}, request_key="site2")
        other_trench = self.app.field.create_trench(self.admin, {"site_id": other_site["entity_id"], "code": "T9"}, request_key="trench2")
        # 出土事件的层位必须与探方一致
        with self.assertRaises(ValidationError):
            self.app.field.record_recovery(self.admin, {
                "trench_id": other_trench["entity_id"], "feature_id": chain["burial"]["entity_id"],
                "stratum_id": chain["stratum"]["entity_id"], "code": "EX-BAD",
                "occurred_at": "2026-09-01T09:00:00+08:00", "recorded_by": "u",
                "field_note": "x", "photo_refs": ["ph:2"]}, request_key="bad-recovery")
        # 重量必须为正数
        with self.assertRaises(ValidationError):
            self.app.field.register_find(self.admin, {
                "recovery_event_id": chain["recovery"]["entity_id"], "code": "F", "material": "bone",
                "registered_weight": "-1", "holding_org": "org:uz"}, request_key="bad-weight")
        # 遗迹类型受限
        with self.assertRaises(ValidationError):
            self.app.field.create_feature(self.admin, {
                "trench_id": chain["trench"]["entity_id"], "kind": "spaceship", "code": "X1"},
                request_key="bad-feature")

    def test_research_never_overwrites_field_record(self):
        chain = self._field_chain()
        mother = self._mother(chain["find"])
        history = self.app.repository.history("finds", chain["find"]["entity_id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["field_note"] if "field_note" in history[0] else
                         self.app.repository.get("arch_recovery_events", chain["recovery"]["entity_id"])["field_note"],
                         "原位出土")
        # 无论追加多少研究结论，出土事件与遗物仍是第 1 版
        self.assertEqual(self.app.repository.get("finds", chain["find"]["entity_id"])["version"], 1)
        self.assertEqual(mother["version"], 1)  # 母样登记一次成型，研究追加不回写样本

    # ------------------------------------------------------------ 分装守恒

    def test_split_quantity_conservation_and_overdraft(self):
        chain = self._field_chain()
        mother = self._mother(chain["find"])
        split = self.app.samples.split(self.li, mother["entity_id"], [
            {"code": "A1", "material": "human_remains", "amount": "2.00"},
            {"code": "A2", "material": "human_remains", "amount": "1.50"}], request_key="split1")
        self.assertEqual(split["parent_available"], "1.50")
        a1 = split["children"][0]["sample_id"]
        self.assertEqual(self.app.samples.balance(a1)["available"], "2.00")
        self.assertEqual(self.app.samples.balance(mother["entity_id"])["available"], "1.50")
        # 幂等重放：同一分装请求不重复扣减
        replay = self.app.samples.split(self.li, mother["entity_id"], [
            {"code": "A1", "material": "human_remains", "amount": "2.00"},
            {"code": "A2", "material": "human_remains", "amount": "1.50"}], request_key="split1")
        self.assertEqual([c["sample_id"] for c in replay["children"]],
                         [c["sample_id"] for c in split["children"]])
        self.assertEqual(self.app.samples.balance(mother["entity_id"])["available"], "1.50")
        # 超出余量必须拒绝
        with self.assertRaises(ConflictError):
            self.app.samples.split(self.li, mother["entity_id"], [
                {"code": "A3", "material": "human_remains", "amount": "2.00"}], request_key="split2")
        self.assertEqual(self.app.samples.verify_lineage()["status"], "balanced")

    def test_consume_overdraft_and_restock(self):
        chain = self._field_chain()
        mother = self._mother(chain["find"], amount="0.50")
        self.app.samples.split(self.li, mother["entity_id"], [
            {"code": "A1", "material": "human_remains", "amount": "0.50"}], request_key="sp")
        a1 = self.app.samples.children(mother["entity_id"])[0]["entity_id"]
        request, _ = self._approved_request(a1)  # 批准计划量 0.5
        moved = self.app.samples.consume(self.li, a1, "0.40", request_id=request["entity_id"], request_key="c1")
        self.assertEqual(moved["amount_available"], "0.10")
        replay = self.app.samples.consume(self.li, a1, "0.40", request_id=request["entity_id"], request_key="c1")
        self.assertEqual(replay["amount_available"], "0.10")  # 重放不重复消耗
        # 累计超过批准计划量必须拒绝（0.40 + 0.20 > 0.50）
        with self.assertRaises(ConflictError):
            self.app.samples.consume(self.li, a1, "0.20", request_id=request["entity_id"], request_key="c2")
        # 未批准/不存在的申请不得取样
        with self.assertRaises(Exception):
            self.app.samples.consume(self.li, a1, "0.10", request_id="research_requests:ghost", request_key="c4")
        # 用满计划量后样本标记耗尽
        self.app.samples.consume(self.li, a1, "0.10", request_id=request["entity_id"], request_key="c3")
        self.assertEqual(self.app.samples.get(a1)["state"], "exhausted")
        self.app.samples.restock(self.li, a1, "0.20", reason="未用粉末退回", request_key="r1")
        self.assertEqual(self.app.samples.get(a1)["state"], "active")
        self.assertEqual(self.app.samples.balance(a1)["available"], "0.20")

    # ------------------------------------------------------------ 扫描与隔离

    def _handover(self, find, sample_id, *, code="A1", weight="2.00", package="PKG1",
                  to_org="org:cn-lab", key="ho", due_back=None):
        payload = {"project_id": "p1", "kind": "outbound", "from_org": "org:uz", "to_org": to_org,
                   "package_code": package, "customs_ref": "cus:1",
                   "items": [{"sample_id": sample_id, "declared_code": code, "declared_weight": weight}]}
        if due_back:
            payload["due_back_at"] = due_back
        return self.app.custody.create_handover(self.admin, payload, request_key=key)

    def test_duplicate_scan_confirms_only(self):
        chain = self._field_chain()
        mother = self._mother(chain["find"])
        split = self.app.samples.split(self.li, mother["entity_id"], [
            {"code": "A1", "material": "human_remains", "amount": "2.00"}], request_key="sp")
        a1 = split["children"][0]["sample_id"]
        ho = self._handover(chain["find"], a1)
        self.app.custody.dispatch(self.admin, ho["entity_id"], expected_version=1, request_key="d1")
        first = self.app.custody.scan(self.admin, ho["entity_id"], package_code="PKG1", sample_id=a1,
                                      observed_code="A1", observed_weight="2.00")
        second = self.app.custody.scan(self.admin, ho["entity_id"], package_code="PKG1", sample_id=a1,
                                       observed_code="A1", observed_weight="2.00")
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["confirmed_scan"], first["scan_id"])
        received = self.app.custody.receive(self.admin, ho["entity_id"], expected_version=2, request_key="rcv")
        self.assertEqual(received["state"], "received")
        self.assertEqual(self.app.samples.get(a1)["custodian_org"], "org:cn-lab")

    def test_weight_conflict_quarantines_immediately(self):
        chain = self._field_chain()
        mother = self._mother(chain["find"])
        split = self.app.samples.split(self.li, mother["entity_id"], [
            {"code": "A2", "material": "human_remains", "amount": "1.50"}], request_key="sp")
        a2 = split["children"][0]["sample_id"]
        ho = self._handover(chain["find"], a2, code="A2", weight="1.50", package="PKG2",
                            to_org="org:other", key="ho2")
        self.app.custody.dispatch(self.admin, ho["entity_id"], expected_version=1, request_key="d2")
        with self.assertRaises(ConflictError):
            self.app.custody.scan(self.admin, ho["entity_id"], package_code="PKG2", sample_id=a2,
                                  observed_code="A2", observed_weight="1.20")
        open_cases = self.app.custody.list_quarantine(state="open")
        self.assertEqual(len(open_cases), 1)
        self.assertEqual(open_cases[0]["subject_id"], a2)
        self.assertEqual(self.app.samples.get(a2)["state"], "quarantined")
        self.assertEqual(self.app.repository.get("arch_handovers", ho["entity_id"])["state"], "quarantined")
        # 隔离后一切数量变动冻结（即使持有覆盖该样本的批准申请）
        request_q, _ = self._approved_request(a2)
        with self.assertRaises(ConflictError):
            self.app.samples.consume(self.li, a2, "0.1", request_id=request_q["entity_id"], request_key="cx")
        # 编号冲突同样隔离（包装编号不符）
        split2 = self.app.samples.split(self.li, mother["entity_id"], [
            {"code": "A3", "material": "human_remains", "amount": "0.50"}], request_key="sp3")
        a3 = split2["children"][0]["sample_id"]
        ho3 = self._handover(chain["find"], a3, code="A3", weight="0.50", package="PKG3", key="ho3")
        with self.assertRaises(ConflictError):
            self.app.custody.scan(self.admin, ho3["entity_id"], package_code="WRONG", sample_id=a3,
                                  observed_code="A3", observed_weight="0.50")
        self.assertEqual(len(self.app.custody.list_quarantine(state="open")), 2)

    # ------------------------------------------------------------ 授权与回避

    def _approved_request(self, sample_id):
        self.app.research.grant_access(self.admin, {
            "project_id": "p1", "subject_id": "user:li", "purpose": "C14",
            "material_scope": "human_remains", "valid_from": "2026-09-01T00:00:00Z"}, request_key="g1")
        request = self.app.research.submit_request(self.li, {
            "project_id": "p1", "purpose": "C14", "method": "AMS-C14",
            "sample_ids": [sample_id], "consume_plan": {sample_id: "0.5"}}, request_key="q1")
        decision = self.app.research.decide_request(self.karimov, request["entity_id"], decision="approved",
                                                    reason="计划内", expected_version=1, request_key="ap1")
        return request, decision

    def test_sensitive_access_and_self_approval(self):
        chain = self._field_chain()
        mother = self._mother(chain["find"])
        split = self.app.samples.split(self.li, mother["entity_id"], [
            {"code": "A1", "material": "human_remains", "amount": "2.00"}], request_key="sp")
        a1 = split["children"][0]["sample_id"]
        # 未授权的 wang 不能申请敏感人骨
        with self.assertRaises(PermissionDenied):
            self.app.research.submit_request(self.wang, {
                "project_id": "p1", "purpose": "C14", "method": "AMS-C14", "sample_ids": [a1]},
                request_key="q-denied")
        # 申请人不能批准自己的申请
        self.app.research.grant_access(self.admin, {
            "project_id": "p1", "subject_id": "user:li", "purpose": "C14",
            "material_scope": "human_remains", "valid_from": "2026-09-01T00:00:00Z"}, request_key="g-self")
        own = self.app.research.submit_request(self.li, {
            "project_id": "p1", "purpose": "C14", "method": "AMS-C14",
            "sample_ids": [a1]}, request_key="q-self")
        li_reviewer = AccessContext("user:li", frozenset({"approve:research"}))
        with self.assertRaises(PermissionDenied):
            self.app.research.decide_request(li_reviewer, own["entity_id"], decision="approved",
                                            reason="自批", expected_version=1, request_key="ap-self")
        # 无审批权限也被拒绝
        with self.assertRaises(PermissionDenied):
            self.app.research.decide_request(self.li, own["entity_id"], decision="approved",
                                            reason="越权", expected_version=1, request_key="ap-denied")

    def test_consumption_and_results_stay_within_approved_scope(self):
        chain = self._field_chain()
        mother = self._mother(chain["find"])
        split = self.app.samples.split(self.li, mother["entity_id"], [
            {"code": "A1", "material": "human_remains", "amount": "2.00"},
            {"code": "A2", "material": "human_remains", "amount": "1.00"}], request_key="sp")
        a1, a2 = [c["sample_id"] for c in split["children"]]
        request, _ = self._approved_request(a1)
        self.app.samples.consume(self.li, a1, "0.50", request_id=request["entity_id"], request_key="use")
        # 检测结果只能登记批准范围内的样本
        with self.assertRaises(ValidationError):
            self.app.research.record_result(self.li, {
                "request_id": request["entity_id"], "lab_org": "org:cn-lab", "method": "AMS-C14",
                "sample_ids": [a2], "values": {"age_bp": "2475"}, "raw_ref": "raw:1",
                "issued_at": "2026-10-01T10:00:00+08:00"}, request_key="res-bad")
        with self.assertRaises(ValidationError):
            self.app.research.record_result(self.li, {
                "request_id": request["entity_id"], "lab_org": "org:cn-lab", "method": "DNA",
                "sample_ids": [a1], "values": {}, "raw_ref": "raw:2",
                "issued_at": "2026-10-01T10:00:00+08:00"}, request_key="res-method")

    def test_interpretation_is_versioned_append_with_evidence(self):
        chain = self._field_chain()
        mother = self._mother(chain["find"])
        split = self.app.samples.split(self.li, mother["entity_id"], [
            {"code": "A1", "material": "human_remains", "amount": "2.00"}], request_key="sp")
        a1 = split["children"][0]["sample_id"]
        request, _ = self._approved_request(a1)
        self.app.samples.consume(self.li, a1, "0.50", request_id=request["entity_id"], request_key="use")
        result = self.app.research.record_result(self.li, {
            "request_id": request["entity_id"], "lab_org": "org:cn-lab", "method": "AMS-C14",
            "sample_ids": [a1], "values": {"age_bp": "2475"}, "raw_ref": "raw:1",
            "issued_at": "2026-10-01T10:00:00+08:00"}, request_key="res")
        # 没有证据 / 不存在的证据 / 没有检测结果都不允许成结论
        with self.assertRaises(ValidationError):
            self.app.research.add_interpretation(self.li, {
                "project_id": "p1", "kind": "dating", "claim": "无证据结论",
                "evidence_ids": [], "based_on_results": [result["entity_id"]]}, request_key="i0")
        with self.assertRaises(NotFoundError):
            self.app.research.add_interpretation(self.li, {
                "project_id": "p1", "kind": "dating", "claim": "幽灵证据",
                "evidence_ids": [{"entity_type": "samples", "entity_id": "samples:nope"}],
                "based_on_results": [result["entity_id"]]}, request_key="i1")
        v1 = self.app.research.add_interpretation(self.li, {
            "project_id": "p1", "kind": "dating",
            "claim": "M9 下葬于公元前 800–500 年。",
            "evidence_ids": [{"entity_type": "samples", "entity_id": a1},
                             {"entity_type": "arch_recovery_events", "entity_id": chain["recovery"]["entity_id"]}],
            "based_on_results": [result["entity_id"]]}, request_key="i2")
        v2 = self.app.research.add_interpretation(self.li, {
            "project_id": "p1", "kind": "cultural_attribution",
            "claim": "结合新校准曲线，M9 下葬收窄为公元前 760–540 年，文化属性归入早期游牧遗存。",
            "evidence_ids": [{"entity_type": "research_versions", "entity_id": v1["entity_id"]},
                             {"entity_type": "test_results", "entity_id": result["entity_id"]}],
            "based_on_results": [result["entity_id"]], "supersedes_version_id": v1["entity_id"]},
            request_key="i3")
        versions = self.app.research.interpretations(a1)
        self.assertEqual({v["entity_id"] for v in versions}, {v1["entity_id"], v2["entity_id"]})
        # 田野事实始终未被研究版本触碰
        self.assertEqual(self.app.repository.get("arch_recovery_events", chain["recovery"]["entity_id"])["version"], 1)
        return chain, a1, result, v1, v2, request

    # ------------------------------------------------------------ 反查

    def test_trace_claim_back_to_burial_sample_experiment_custody_permission(self):
        chain, a1, result, v1, v2, request = self.test_interpretation_is_versioned_append_with_evidence()
        ho = self._handover(chain["find"], a1, key="ho-trace")
        self.app.custody.dispatch(self.admin, ho["entity_id"], expected_version=1, request_key="d-trace")
        self.app.custody.scan(self.admin, ho["entity_id"], package_code="PKG1", sample_id=a1,
                              observed_code="A1", observed_weight="2.00")
        self.app.custody.receive(self.admin, ho["entity_id"], expected_version=2, request_key="r-trace")
        permission = self.app.research.grant_publication(self.admin, {
            "project_id": "p1", "title": "M9 初报", "applicant": "user:li",
            "version_ids": [v1["entity_id"], v2["entity_id"]], "scope": "联合报告",
            "granted_at": "2026-10-01T11:00:00+08:00"}, request_key="pub")
        trace = self.app.provenance.trace_claim("公元前 760–540")
        self.assertEqual(trace["conclusion"]["version_id"], v2["entity_id"])
        self.assertEqual([b["code"] for b in trace["burials"]], ["M9"])
        self.assertEqual([s["code"] for s in trace["samples"]], ["A1"])
        self.assertEqual(trace["experiments"][0]["result_id"], result["entity_id"])
        self.assertEqual(trace["experiments"][0]["approved_by"], "user:karimov")
        self.assertEqual(len(trace["cross_border_responsibility"]), 1)
        custody = trace["cross_border_responsibility"][0]
        self.assertEqual(custody["from_org"], "org:uz")
        self.assertEqual(custody["to_org"], "org:cn-lab")
        self.assertEqual(custody["scans"][0]["observed_weight"], "2.00")
        self.assertEqual(trace["publication_permissions"][0]["permission_id"], permission["entity_id"])
        # 样本谱系链包含登记、分装、消耗、保管转移
        kinds = [e["event_type"] for e in trace["samples"][0]["lineage_events"]]
        self.assertIn("split_in", kinds)
        self.assertIn("consume", kinds)
        self.assertIn("custody", kinds)

    # ------------------------------------------------------------ 持久化监控

    def test_monitoring_jobs_survive_restart(self):
        # 任务排定于未来，进程重启后仍在持久化队列中
        self.app.jobs.schedule(job_type="return_due", subject_id="arch_handovers:x",
                               run_at="2026-10-02T00:00:00Z", payload={})
        self.assertEqual(self.app.jobs.claim_due(), [])
        restarted = CivicFlow.open(self.db_path, fixed_now="2026-10-03T00:00:00Z")
        claimed = restarted.jobs.claim_due()
        self.assertEqual(len(claimed), 1)
        restarted.jobs.finish(claimed[0]["job_id"])

    def test_overdue_return_fires_and_resolves_after_return(self):
        chain = self._field_chain()
        mother = self._mother(chain["find"])
        split = self.app.samples.split(self.li, mother["entity_id"], [
            {"code": "A1", "material": "human_remains", "amount": "2.00"}], request_key="sp")
        a1 = split["children"][0]["sample_id"]
        ho = self._handover(chain["find"], a1, due_back="2026-09-20T10:00:00+08:00", key="ho-due")
        self.app.custody.dispatch(self.admin, ho["entity_id"], expected_version=1, request_key="d-due")
        self.app.custody.scan(self.admin, ho["entity_id"], package_code="PKG1", sample_id=a1,
                              observed_code="A1", observed_weight="2.00")
        self.app.custody.receive(self.admin, ho["entity_id"], expected_version=2, request_key="r-due")
        handled = self.app.monitoring.run_due()
        self.assertEqual([h["finding"]["kind"] for h in handled if h["finding"]], ["overdue_return"])
        # 原物通过 return 单返还后，再次巡检不再报逾期
        ret = self.app.custody.create_handover(self.admin, {
            "project_id": "p1", "kind": "return", "from_org": "org:cn-lab", "to_org": "org:uz",
            "package_code": "PKG-R", "customs_ref": "cus:r",
            "items": [{"sample_id": a1, "declared_code": "A1", "declared_weight": "1.00"}]},
            request_key="ho-ret")
        self.app.custody.dispatch(self.admin, ret["entity_id"], expected_version=1, request_key="d-ret")
        self.app.custody.scan(self.admin, ret["entity_id"], package_code="PKG-R", sample_id=a1,
                              observed_code="A1", observed_weight="1.00")
        self.app.custody.receive(self.admin, ret["entity_id"], expected_version=2, request_key="r-ret")
        # 直接重跑检查逻辑：同一交接不应再次产生逾期发现
        later = CivicFlow.open(self.db_path, fixed_now="2026-10-05T12:00:00+08:00")
        before = len(later.monitoring.findings(kind="overdue_return"))
        later.jobs.schedule(job_type="return_due", subject_id=ho["entity_id"],
                            run_at="2026-10-05T00:00:00Z", payload={})
        later.monitoring.run_due()
        after = later.monitoring.findings(kind="overdue_return")
        self.assertEqual(len(after), before)

    def test_storage_condition_checks_recur_and_flag_breaches(self):
        vault = self.app.field.create_storage(self.admin, {
            "project_id": "p1", "code": "V1", "org": "org:uz", "kind": "vault",
            "temperature_c": "2:8", "humidity_pct": "40:60"}, request_key="vault")
        self.app.monitoring.schedule_storage_check(storage_id=vault["entity_id"],
                                                   first_run_at="2026-10-01T00:00:00Z")
        self.app.monitoring.record_reading(storage_id=vault["entity_id"], temperature_c="14.2",
                                           humidity_pct="31", recorded_by="u")
        handled = self.app.monitoring.run_due()
        self.assertEqual([h["finding"]["kind"] for h in handled if h["finding"]], ["storage_condition"])
        detail = next(h["finding"]["detail"] for h in handled if h["finding"])
        self.assertEqual(len(detail["problems"]), 2)
        # 周期性巡检自动续排（下次为本次 +24h），重启后仍然继续
        restarted = CivicFlow.open(self.db_path, fixed_now="2026-10-03T00:30:00+08:00")
        self.assertTrue(any(item["job_type"] == "storage_check" for item in restarted.jobs.claim_due()))

    def test_quarantined_and_in_transit_samples_cannot_rejoin_handovers(self):
        chain = self._field_chain()
        mother = self._mother(chain["find"])
        split = self.app.samples.split(self.li, mother["entity_id"], [
            {"code": "A1", "material": "human_remains", "amount": "2.00"}], request_key="sp")
        a1 = split["children"][0]["sample_id"]
        ho = self._handover(chain["find"], a1, key="ho-active")
        self.app.custody.dispatch(self.admin, ho["entity_id"], expected_version=1, request_key="d-active")
        # 同一样本在在途交接期间不得再进第二份交接单
        with self.assertRaises(ConflictError):
            self._handover(chain["find"], a1, package="PKG-DUP", key="ho-dup")
        # 收货后可以发起返还
        self.app.custody.scan(self.admin, ho["entity_id"], package_code="PKG1", sample_id=a1,
                              observed_code="A1", observed_weight="2.00")
        self.app.custody.receive(self.admin, ho["entity_id"], expected_version=2, request_key="r-active")
        ret = self.app.custody.create_handover(self.admin, {
            "project_id": "p1", "kind": "return", "from_org": "org:cn-lab", "to_org": "org:uz",
            "package_code": "PKG-RET", "customs_ref": "cus:r",
            "items": [{"sample_id": a1, "declared_code": "A1", "declared_weight": "1.00"}]},
            request_key="ho-return-ok")
        self.assertEqual(ret["state"], "prepared")

    # ------------------------------------------------------------ 全链校验
    def test_verify_includes_lineage(self):
        chain = self._field_chain()
        mother = self._mother(chain["find"])
        self.app.samples.split(self.li, mother["entity_id"], [
            {"code": "A1", "material": "human_remains", "amount": "2.00"}], request_key="sp")
        report = self.app.verify()
        self.assertEqual(report["lineage"]["status"], "balanced")
        self.assertGreaterEqual(report["audit_entries"], 1)


if __name__ == "__main__":
    unittest.main()
