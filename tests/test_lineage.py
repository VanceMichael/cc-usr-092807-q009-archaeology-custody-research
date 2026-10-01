from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from civicflow.lineage import Lineage
from civicflow.security import AccessContext

NOW = "2026-10-01T08:00:00+08:00"


class LineageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "lineage.sqlite3"
        self.app = CivicFlow.open(self.db, fixed_now=NOW)
        self.lineage = Lineage.open(self.app)
        self.system = AccessContext.system("tester")

    def tearDown(self):
        self.temp.cleanup()

    def reopen(self, now: str) -> Lineage:
        self.app = CivicFlow.open(self.db, fixed_now=now)
        self.lineage = Lineage.open(self.app)
        return self.lineage

    def make_chain(self, *, sensitive: bool = False) -> dict:
        site = self.lineage.sites.create(self.system, {"name": "奥佐德墓地", "code": "OZD", "country": "乌兹别克斯坦"}, request_key="site")
        trench = self.lineage.trenches.create(self.system, {"site_id": site["entity_id"], "code": "T1", "opened_at": NOW}, request_key="trench")
        feature = self.lineage.features.create(self.system, {"trench_id": trench["entity_id"], "kind": "tomb", "code": "M12"}, request_key="feature")
        stratum = self.lineage.strata.create(self.system, {"trench_id": trench["entity_id"], "code": "L3"}, request_key="stratum")
        event = self.lineage.excavations.create(self.system, {"feature_id": feature["entity_id"], "stratum_id": stratum["entity_id"], "occurred_at": NOW, "recorder": "person:field", "field_notes": "田野记录"}, request_key="event")
        artifact = self.lineage.artifacts.create(self.system, {"event_id": event["entity_id"], "field_number": "OZD-M12-P07", "kind": "牙齿", "material": "人骨", "material_class": "human_remains", "sensitive": sensitive}, request_key="artifact")
        location = self.lineage.locations.create(self.system, {"org": "org:cn-lab", "facility": "实验室", "conditions": {"temperature_c": 22, "humidity_pct": 48}}, request_key="location")
        return {"site": site, "trench": trench, "feature": feature, "stratum": stratum, "event": event, "artifact": artifact, "location": location}

    def make_sample(self, chain: dict, *, quantity: str = "1000") -> dict:
        return self.lineage.samples.register(self.system, artifact_id=chain["artifact"]["entity_id"], label="S-1", unit="mg", quantity=quantity, location_id=chain["location"]["entity_id"], request_key="sample")["sample"]

    def make_handover(self, sample: dict, *, package_no: str = "PKG-1", weight: str = "500", quantity: str = "500", due_at: str = "2026-10-31T00:00:00+08:00", key: str = "handover") -> dict:
        return self.lineage.handovers.prepare(self.system, package_no=package_no, from_org="org:uz-iu", to_org="org:cn-lab", responsible="person:escort", items=[{"sample_id": sample["entity_id"], "quantity": quantity, "weight": weight}], expected_weight=weight, permit_ref="许可-1", due_at=due_at, request_key=key)["handover"]

    def test_field_chain_and_parent_validation(self):
        chain = self.make_chain()
        self.assertEqual(chain["artifact"]["state"], "recorded")
        with self.assertRaises(ValidationError):
            self.lineage.trenches.create(self.system, {"site_id": "sites:missing", "code": "T2", "opened_at": NOW}, request_key="bad-trench")
        with self.assertRaises(ValidationError):
            self.lineage.excavations.create(self.system, {"feature_id": chain["feature"]["entity_id"], "stratum_id": "strata:missing", "occurred_at": NOW, "recorder": "r", "field_notes": "n"}, request_key="bad-event")

    def test_field_records_immutable_corrections_append(self):
        chain = self.make_chain()
        with self.assertRaises(ValidationError):
            self.lineage.excavations.revise(self.system, chain["event"]["entity_id"], {"field_notes": "被研究结论改写"}, expected_version=1, request_key="revise")
        correction = self.lineage.excavations.correct(self.system, chain["event"]["entity_id"], {"field_notes": "更正：层位应为L3上"}, reason="现场复核", request_key="correct")
        self.assertEqual(correction["target"]["field_notes"], "田野记录")
        again = self.lineage.excavations.get(self.system, chain["event"]["entity_id"])
        self.assertEqual(again["field_notes"], "田野记录")
        self.assertEqual(len(self.lineage.excavations.corrections_of(self.system, chain["event"]["entity_id"])), 1)

    def test_split_conservation_and_parent_child(self):
        chain = self.make_chain()
        parent = self.make_sample(chain, quantity="1000")
        child = self.lineage.samples.split(self.system, parent_id=parent["entity_id"], label="S-1-A", quantity="400", location_id=chain["location"]["entity_id"], request_key="split-1")["sample"]
        self.assertEqual(child["parent_id"], parent["entity_id"])
        balance = self.lineage.samples.balance(self.system, parent["entity_id"])
        self.assertEqual(balance["held_minor"], 600000)
        self.assertEqual(balance["available_minor"], 600000)
        with self.assertRaises(ConflictError):
            self.lineage.samples.split(self.system, parent_id=parent["entity_id"], label="S-1-B", quantity="700", location_id=chain["location"]["entity_id"], request_key="split-2")
        children = self.lineage.samples.children_of(self.system, parent["entity_id"])
        self.assertEqual([row["entity_id"] for row in children], [child["entity_id"]])

    def test_split_idempotent_replay(self):
        chain = self.make_chain()
        parent = self.make_sample(chain)
        first = self.lineage.samples.split(self.system, parent_id=parent["entity_id"], label="S-1-A", quantity="100", location_id=chain["location"]["entity_id"], request_key="split-same")
        second = self.lineage.samples.split(self.system, parent_id=parent["entity_id"], label="S-1-A", quantity="100", location_id=chain["location"]["entity_id"], request_key="split-same")
        self.assertEqual(first["sample"]["entity_id"], second["sample"]["entity_id"])
        self.assertEqual(len(self.lineage.samples.children_of(self.system, parent["entity_id"])), 1)

    def test_double_consumption_blocked_by_reservation(self):
        chain = self.make_chain()
        sample = self.make_sample(chain, quantity="500")
        applicant = AccessContext(actor_id="person:a", permissions=frozenset({"submit:requests", "read:research"}))
        approver = AccessContext(actor_id="person:b", permissions=frozenset({"approve:requests", "read:research"}))
        first = self.lineage.requests.submit(applicant, project="p1", purpose="测年", sample_id=sample["entity_id"], quantity="400", method="AMS-14C", request_key="req-1")
        self.lineage.requests.approve(approver, first["entity_id"], request_key="approve-1")
        second = self.lineage.requests.submit(applicant, project="p1", purpose="测年", sample_id=sample["entity_id"], quantity="200", method="AMS-14C", request_key="req-2")
        with self.assertRaises(ConflictError):
            self.lineage.requests.approve(approver, second["entity_id"], request_key="approve-2")
        balance = self.lineage.samples.balance(self.system, sample["entity_id"])
        self.assertEqual(balance["reserved_minor"], 400000)
        self.assertEqual(balance["available_minor"], 100000)

    def test_applicant_cannot_approve_own_request(self):
        chain = self.make_chain()
        sample = self.make_sample(chain)
        person = AccessContext(actor_id="person:same", permissions=frozenset({"submit:requests", "approve:requests"}))
        request = self.lineage.requests.submit(person, project="p1", purpose="测年", sample_id=sample["entity_id"], quantity="100", method="AMS-14C", request_key="req-self")
        with self.assertRaises(PermissionDenied):
            self.lineage.requests.approve(person, request["entity_id"], request_key="approve-self")

    def test_sensitive_requires_grant_and_redacts_fields(self):
        chain = self.make_chain(sensitive=True)
        sample = self.make_sample(chain)
        outsider = AccessContext(actor_id="person:outsider", permissions=frozenset({"read:lineage", "read:samples", "submit:requests"}), scopes=frozenset({"project:other"}))
        masked = self.lineage.artifacts.get(outsider, chain["artifact"]["entity_id"], purpose="测年")
        self.assertEqual(masked["field_number"], "***")
        with self.assertRaises(PermissionDenied):
            self.lineage.requests.submit(outsider, project="other", purpose="测年", sample_id=sample["entity_id"], quantity="10", method="AMS-14C", request_key="req-denied")
        self.lineage.grants.grant(self.system, project="proj-c14", purpose="测年", material_class="human_remains", grantee_org="org:uz-iu", expires_at="2027-01-01T00:00:00+08:00", request_key="grant")
        insider = AccessContext(actor_id="person:insider", permissions=frozenset({"read:lineage", "read:samples", "submit:requests"}), scopes=frozenset({"project:proj-c14"}))
        visible = self.lineage.artifacts.get(insider, chain["artifact"]["entity_id"], purpose="测年")
        self.assertEqual(visible["field_number"], "OZD-M12-P07")
        wrong_purpose = self.lineage.artifacts.get(insider, chain["artifact"]["entity_id"], purpose="古DNA")
        self.assertEqual(wrong_purpose["field_number"], "***")
        request = self.lineage.requests.submit(insider, project="proj-c14", purpose="测年", sample_id=sample["entity_id"], quantity="10", method="AMS-14C", request_key="req-granted")
        self.assertEqual(request["state"], "submitted")

    def test_expired_grant_denies_access(self):
        chain = self.make_chain(sensitive=True)
        sample = self.make_sample(chain)
        self.lineage.grants.grant(self.system, project="proj-c14", purpose="测年", material_class="human_remains", grantee_org="org:uz-iu", expires_at="2026-10-01T07:00:00+08:00", request_key="grant-expired")
        insider = AccessContext(actor_id="person:insider", permissions=frozenset({"submit:requests"}), scopes=frozenset({"project:proj-c14"}))
        with self.assertRaises(PermissionDenied):
            self.lineage.requests.submit(insider, project="proj-c14", purpose="测年", sample_id=sample["entity_id"], quantity="10", method="AMS-14C", request_key="req-expired")

    def test_scan_duplicate_confirms_original(self):
        chain = self.make_chain()
        sample = self.make_sample(chain)
        handover = self.make_handover(sample)
        customs = AccessContext(actor_id="person:customs", permissions=frozenset({"scan:handovers"}))
        first = self.lineage.handovers.scan(customs, package_no="PKG-1", weight="500", to_org="org:cn-lab", request_key="scan-1")
        self.assertEqual(first["status"], "received")
        again = self.lineage.handovers.scan(customs, package_no="PKG-1", weight="500", to_org="org:cn-lab", request_key="scan-2")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["handover_id"], handover["entity_id"])
        scans = self.lineage.handovers.get(self.system, handover["entity_id"])["scans"]
        self.assertEqual(len(scans), 1)

    def test_weight_conflict_quarantines_immediately(self):
        chain = self.make_chain()
        sample = self.make_sample(chain)
        handover = self.make_handover(sample)
        customs = AccessContext(actor_id="person:customs", permissions=frozenset({"scan:handovers"}))
        with self.assertRaises(ConflictError):
            self.lineage.handovers.scan(customs, package_no="PKG-1", weight="480", to_org="org:cn-lab", request_key="scan-bad")
        isolated = self.lineage.handovers.get(self.system, handover["entity_id"])
        self.assertEqual(isolated["state"], "quarantined")
        frozen = self.lineage.samples.get(self.system, sample["entity_id"])
        self.assertEqual(frozen["state"], "quarantined")
        with self.assertRaises(ConflictError):
            self.lineage.samples.split(self.system, parent_id=sample["entity_id"], label="S-X", quantity="10", location_id=chain["location"]["entity_id"], request_key="split-frozen")
        quarantines = self.app.repository.search("quarantines", "handover_id", handover["entity_id"])
        self.assertEqual(len(quarantines), 1)
        self.assertEqual(quarantines[0]["expected_weight"], "500")
        self.assertEqual(quarantines[0]["scanned_weight"], "480")
        with self.assertRaises(ConflictError):
            self.lineage.handovers.scan(customs, package_no="PKG-1", weight="500", to_org="org:cn-lab", request_key="scan-after")
        released = self.lineage.handovers.release_quarantine(self.system, quarantines[0]["entity_id"], reason="复核确认为秤具误差", request_key="release-q")
        self.assertEqual(released["quarantine"]["state"], "resolved")
        self.assertEqual(self.lineage.samples.get(self.system, sample["entity_id"])["state"], "registered")

    def test_unknown_package_rejected(self):
        customs = AccessContext(actor_id="person:customs", permissions=frozenset({"scan:handovers"}))
        with self.assertRaises(NotFoundError):
            self.lineage.handovers.scan(customs, package_no="PKG-404", weight="1", to_org="org:cn-lab", request_key="scan-404")

    def test_consume_requires_approved_reservation(self):
        chain = self.make_chain()
        sample = self.make_sample(chain, quantity="300")
        applicant = AccessContext(actor_id="person:a", permissions=frozenset({"submit:requests"}))
        approver = AccessContext(actor_id="person:b", permissions=frozenset({"approve:requests"}))
        request = self.lineage.requests.submit(applicant, project="p1", purpose="测年", sample_id=sample["entity_id"], quantity="200", method="AMS-14C", request_key="req")
        with self.assertRaises(ConflictError):
            self.lineage.results.record(self.system, request_id=request["entity_id"], method="AMS-14C", lab="lab", consumed_quantity="100", result={}, request_key="result-early")
        self.lineage.requests.approve(approver, request["entity_id"], request_key="approve")
        with self.assertRaises(ConflictError):
            self.lineage.results.record(self.system, request_id=request["entity_id"], method="AMS-14C", lab="lab", consumed_quantity="250", result={}, request_key="result-over")
        recorded = self.lineage.results.record(self.system, request_id=request["entity_id"], method="AMS-14C", lab="lab", consumed_quantity="200", result={"bp": "2450±30"}, request_key="result-ok")
        self.assertEqual(recorded["result"]["state"], "recorded")
        completed = self.lineage.requests.get(self.system, request["entity_id"])
        self.assertEqual(completed["state"], "completed")
        balance = self.lineage.samples.balance(self.system, sample["entity_id"])
        self.assertEqual(balance["held_minor"], 100000)
        self.assertEqual(balance["reserved_minor"], 0)

    def test_release_unused_returns_quantity(self):
        chain = self.make_chain()
        sample = self.make_sample(chain, quantity="300")
        applicant = AccessContext(actor_id="person:a", permissions=frozenset({"submit:requests"}))
        approver = AccessContext(actor_id="person:b", permissions=frozenset({"approve:requests"}))
        request = self.lineage.requests.submit(applicant, project="p1", purpose="测年", sample_id=sample["entity_id"], quantity="200", method="AMS-14C", request_key="req")
        self.lineage.requests.approve(approver, request["entity_id"], request_key="approve")
        self.lineage.requests.release_unused(applicant, request["entity_id"], quantity="50", request_key="release")
        balance = self.lineage.samples.balance(self.system, sample["entity_id"])
        self.assertEqual(balance["reserved_minor"], 150000)
        self.assertEqual(balance["available_minor"], 150000)
        with self.assertRaises(ConflictError):
            self.lineage.requests.release_unused(applicant, request["entity_id"], quantity="200", request_key="release-over")

    def test_research_version_append_publish_and_field_record_untouched(self):
        chain = self.make_chain()
        sample = self.make_sample(chain)
        applicant = AccessContext(actor_id="person:a", permissions=frozenset({"submit:requests"}))
        approver = AccessContext(actor_id="person:b", permissions=frozenset({"approve:requests"}))
        request = self.lineage.requests.submit(applicant, project="p1", purpose="测年", sample_id=sample["entity_id"], quantity="100", method="AMS-14C", request_key="req")
        self.lineage.requests.approve(approver, request["entity_id"], request_key="approve")
        result = self.lineage.results.record(self.system, request_id=request["entity_id"], method="AMS-14C", lab="lab", consumed_quantity="100", result={"bp": "2450±30"}, request_key="result")["result"]
        with self.assertRaises(ValidationError):
            self.lineage.versions.append(self.system, subject_type="features", subject_id=chain["feature"]["entity_id"], kind="dating", conclusion="无证据结论", evidence=[], request_key="version-empty")
        version = self.lineage.versions.append(self.system, subject_type="features", subject_id=chain["feature"]["entity_id"], kind="dating", conclusion="墓葬M12为公元前8-5世纪", evidence=[{"entity_type": "results", "entity_id": result["entity_id"]}], request_key="version")
        publisher = AccessContext(actor_id="person:pub", permissions=frozenset({"publish:versions"}))
        with self.assertRaises(PermissionDenied):
            self.lineage.versions.publish(publisher, version["entity_id"], permit_id="permits:missing", request_key="publish-no")
        permit = self.lineage.permits.issue(self.system, project="p1", scope="报告", version_ids=[version["entity_id"]], request_key="permit")
        published = self.lineage.versions.publish(publisher, version["entity_id"], permit_id=permit["entity_id"], request_key="publish")
        self.assertEqual(published["state"], "published")
        newer = self.lineage.versions.append(self.system, subject_type="features", subject_id=chain["feature"]["entity_id"], kind="dating", conclusion="墓葬M12为公元前6-4世纪（修订）", evidence=[{"entity_type": "results", "entity_id": result["entity_id"]}], supersedes=version["entity_id"], request_key="version-2")
        self.assertEqual(newer["supersedes"], version["entity_id"])
        event_after = self.lineage.excavations.get(self.system, chain["event"]["entity_id"])
        self.assertEqual(event_after["version"], 1)
        self.assertEqual(event_after["field_notes"], "田野记录")

    def test_return_flow_and_overdue_job_survive_restart(self):
        chain = self.make_chain()
        sample = self.make_sample(chain)
        handover = self.make_handover(sample, due_at="2026-10-02T00:00:00+08:00")
        customs = AccessContext(actor_id="person:customs", permissions=frozenset({"scan:handovers"}))
        self.lineage.handovers.scan(customs, package_no="PKG-1", weight="500", to_org="org:cn-lab", request_key="scan")
        returned = self.lineage.handovers.record_return(self.system, handover["entity_id"], items=[{"sample_id": sample["entity_id"], "quantity": "500", "weight": "500"}], request_key="return")
        self.assertTrue(returned["complete"])
        self.assertEqual(self.lineage.samples.get(self.system, sample["entity_id"])["state"], "registered")
        second = self.make_handover(sample, package_no="PKG-2", due_at="2026-10-02T00:00:00+08:00", key="handover-2")
        later = self.reopen("2026-10-03T00:00:00+08:00")
        results = later.jobs.run_due(self.system)
        overdue = [row for row in results if row["job_type"] == "lineage.return_check"]
        self.assertEqual(len(overdue), 2)
        states = {row["detail"]["handover_id"]: row["detail"]["status"] for row in overdue}
        self.assertEqual(states[handover["entity_id"]], "returned")
        self.assertEqual(states[second["entity_id"]], "overdue")
        again = self.reopen("2026-10-04T00:00:00+08:00")
        self.assertEqual(again.jobs.run_due(self.system), [])
        notices = [dict(row) for row in self.app.database.connect().execute("SELECT * FROM outbox_messages WHERE topic='lineage.handover.overdue'")]
        self.assertEqual(len(notices), 1)

    def test_condition_check_alert_and_reschedule_survive_restart(self):
        chain = self.make_chain()
        self.lineage.jobs.schedule_condition_check(self.system, location_id=chain["location"]["entity_id"], interval_hours=24, required={"temperature_c": [18, 24], "humidity_pct": [40, 60]}, start_at="2026-10-02T08:00:00+08:00")
        later = self.reopen("2026-10-02T09:00:00+08:00")
        results = later.jobs.run_due(self.system)
        self.assertEqual(len(results), 1)
        detail = results[0]["detail"]
        self.assertEqual(detail["result"], "ok")
        self.assertEqual(detail["next_run_at"], "2026-10-03T01:00:00Z")
        location = later.locations.get(self.system, chain["location"]["entity_id"])
        later.locations.revise(self.system, location["entity_id"], {"conditions": {"temperature_c": 30, "humidity_pct": 48}}, expected_version=location["version"], request_key="hot")
        third = self.reopen("2026-10-03T09:00:00+08:00")
        results = third.jobs.run_due(self.system)
        self.assertEqual(results[0]["detail"]["result"], "alert")
        self.assertEqual(results[0]["detail"]["breaches"], ["temperature_c"])
        alerts = [dict(row) for row in self.app.database.connect().execute("SELECT * FROM outbox_messages WHERE topic='lineage.storage.alert'")]
        self.assertEqual(len(alerts), 1)
        rows = self.app.repository.list("inspections")
        self.assertEqual(len(rows), 2)

    def test_trace_conclusion_reaches_tomb_sample_handover_and_permit(self):
        chain = self.make_chain(sensitive=True)
        sample = self.make_sample(chain, quantity="500")
        self.lineage.grants.grant(self.system, project="p1", purpose="测年", material_class="human_remains", grantee_org="org:uz-iu", expires_at="2027-01-01T00:00:00+08:00", request_key="grant")
        handover = self.make_handover(sample)
        customs = AccessContext(actor_id="person:customs", permissions=frozenset({"scan:handovers"}))
        self.lineage.handovers.scan(customs, package_no="PKG-1", weight="500", to_org="org:cn-lab", request_key="scan")
        applicant = AccessContext(actor_id="person:a", permissions=frozenset({"submit:requests"}), scopes=frozenset({"project:p1"}))
        approver = AccessContext(actor_id="person:b", permissions=frozenset({"approve:requests"}))
        request = self.lineage.requests.submit(applicant, project="p1", purpose="测年", sample_id=sample["entity_id"], quantity="100", method="AMS-14C", request_key="req")
        self.lineage.requests.approve(approver, request["entity_id"], request_key="approve")
        result = self.lineage.results.record(self.system, request_id=request["entity_id"], method="AMS-14C", lab="lab", consumed_quantity="100", result={"bp": "2450±30"}, request_key="result")["result"]
        version = self.lineage.versions.append(self.system, subject_type="features", subject_id=chain["feature"]["entity_id"], kind="dating", conclusion="墓葬M12为公元前8-5世纪", evidence=[{"entity_type": "results", "entity_id": result["entity_id"]}], request_key="version")
        permit = self.lineage.permits.issue(self.system, project="p1", scope="报告", version_ids=[version["entity_id"]], request_key="permit")
        reviewer = AccessContext(actor_id="person:reviewer", permissions=frozenset({"read:research", "read:lineage", "read:samples"}), scopes=frozenset({"project:p1"}))
        trace = self.lineage.trace.trace_conclusion(reviewer, version["entity_id"], purpose="测年")
        self.assertEqual(trace["conclusion"]["entity_id"], version["entity_id"])
        self.assertEqual(trace["field_context"]["site"]["name"], "奥佐德墓地")
        self.assertEqual(trace["field_context"]["feature"]["code"], "M12")
        self.assertEqual(trace["field_context"]["stratum"]["code"], "L3")
        self.assertEqual(trace["artifact"]["field_number"], "OZD-M12-P07")
        self.assertEqual(trace["sample_chain"][0]["entity_id"], sample["entity_id"])
        self.assertEqual(trace["evidence"][0]["request"]["applicant"], "person:a")
        self.assertEqual(trace["evidence"][0]["request"]["approver"], "person:b")
        self.assertEqual([h["entity_id"] for h in trace["handovers"]], [handover["entity_id"]])
        self.assertEqual(trace["handovers"][0]["responsible"], "person:escort")
        self.assertEqual([p["entity_id"] for p in trace["permits"]], [permit["entity_id"]])
        kinds = [row["kind"] for row in trace["sample_movements"][sample["entity_id"]]]
        self.assertEqual(kinds, ["register", "reserve", "consume", "release"])

    def test_permissions_enforced(self):
        chain = self.make_chain()
        reader = AccessContext(actor_id="person:reader", permissions=frozenset({"read:lineage"}))
        with self.assertRaises(PermissionDenied):
            self.lineage.sites.create(reader, {"name": "x", "code": "x", "country": "x"}, request_key="denied")
        with self.assertRaises(PermissionDenied):
            self.lineage.samples.register(reader, artifact_id=chain["artifact"]["entity_id"], label="S", unit="mg", quantity="1", location_id=chain["location"]["entity_id"], request_key="denied-2")
        with self.assertRaises(PermissionDenied):
            self.lineage.jobs.run_due(reader)


if __name__ == "__main__":
    unittest.main()
