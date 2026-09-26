from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from collection_logistics.api import JsonApplication
from collection_logistics.clock import FrozenClock
from collection_logistics.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from collection_logistics.planning import AllocationRequest, RiskPoint, allocate_capacity, latest_streak
from collection_logistics.service import CollectionLogisticsService
from collection_logistics.risk import DemandBucket, inventory_coverage, mark_to_risk, traffic_gap
from collection_logistics.storage import initialize


class PlanningTests(unittest.TestCase):
    def test_latest_down_streak_uses_first_close_as_base(self) -> None:
        streak = latest_streak([
            RiskPoint("2026-09-18", Decimal("108")),
            RiskPoint("2026-09-19", Decimal("105")),
            RiskPoint("2026-09-20", Decimal("102")),
            RiskPoint("2026-09-21", Decimal("98")),
        ])
        self.assertEqual(streak.direction, "down")
        self.assertEqual(streak.sessions, 4)
        self.assertEqual(streak.start_date, "2026-09-18")
        self.assertEqual(streak.end_close, Decimal("98"))

    def test_allocation_is_stable_and_does_not_exceed_capacity(self) -> None:
        rows = allocate_capacity(Decimal("100"), [
            AllocationRequest("later", Decimal("80"), 20, "2026-09-24T09:00:00Z"),
            AllocationRequest("first", Decimal("70"), 10, "2026-09-24T10:00:00Z"),
        ])
        self.assertEqual(rows[0]["dispatch_id"], "first")
        self.assertEqual(rows[0]["allocated_units"], "70.000")
        self.assertEqual(rows[1]["allocated_units"], "30.000")

    def test_inventory_coverage_and_traffic_gap(self) -> None:
        coverage = inventory_coverage(
            [{"center_id": "receiving-vault", "preservation_resource_kind": "tow-truck", "available_units": "250"}],
            [DemandBucket("receiving-vault", "tow-truck", Decimal("100"), Decimal("20"))],
        )
        self.assertEqual(coverage[0]["coverage_days"], "2.30")
        self.assertTrue(coverage[0]["below_three_days"])
        gap = traffic_gap(
            opening_inventory=Decimal("100"),
            confirmed_inbound=Decimal("30"),
            forecast_demand=Decimal("120"),
            protected_reserve=Decimal("40"),
        )
        self.assertEqual(gap["traffic_gap"], "30.000")

    def test_mark_to_risk_groups_deterministically(self) -> None:
        result = mark_to_risk(
            [{"position_id": "p1", "risk_index": "HUMIDITY", "quantity_units": "100", "baseline_value": "105"}],
            {"HUMIDITY": Decimal("98")},
        )
        self.assertEqual(result["unrealized_pnl_cny"], "-700.00")


class CollectionLogisticsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = CollectionLogisticsService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"center_id": "collection-east", "name": "北部标本事件保藏中心", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "500000"})
        self.service.create_facility("plan", {"center_id": "receiving-vault-b", "name": "沿海终端", "kind": "receiving-vault", "timezone": "Asia/Shanghai", "capacity_units": "800000"})
        self.service.create_route("plan", {"corridor_id": "transfer-east-1", "origin_center_id": "collection-east", "destination_center_id": "receiving-vault-b", "preservation_resource_kind": "preservation-box", "hourly_capacity": "100000", "delay_basis_points": 25, "response_minutes": 36})

    def tearDown(self) -> None:
        self.connection.close()

    def risk_record(self, day: int, close: str) -> dict[str, object]:
        return self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": f"2026-09-{day}", "index_value": close, "source_revision": f"r-{day}", "observed_at": f"2026-09-{day}T21:00:00Z"})

    def test_risk_record_revisions_preserve_history(self) -> None:
        first = self.risk_record(23, "98")
        second = self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": "2026-09-23", "index_value": "97.8", "source_revision": "r-23-corrected", "observed_at": "2026-09-23T22:00:00Z"})
        self.assertNotEqual(first["risk_record_id"], second["risk_record_id"])
        rows = self.connection.execute("SELECT * FROM risk_index_risk_records ORDER BY risk_record_id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["supersedes_risk_record_id"], rows[0]["risk_record_id"])

    def test_dispatch_request_replay_and_payload_conflict(self) -> None:
        payload = {"dispatch_id": "nom-1", "corridor_id": "transfer-east-1", "specimen_event_id": "herbarium-room", "duty_date": "2026-09-25", "requested_units": "80000", "priority": 10, "idempotency_key": "key-1"}
        first = self.service.submit_dispatch("dispatch", payload)
        self.assertEqual(first, self.service.submit_dispatch("dispatch", payload))
        changed = dict(payload, requested_units="81000")
        with self.assertRaises(Conflict):
            self.service.submit_dispatch("dispatch", changed)

    def test_outage_reduces_allocation_and_deployment_consumes_inventory(self) -> None:
        self.service.announce_restriction("risk", "transfer-east-1", "2026-09-25T00:00:00Z", "2026-09-25T23:59:59Z", "50", "检修")
        for number, requested, priority in ((1, "40000", 10), (2, "30000", 20)):
            self.service.submit_dispatch("dispatch", {"dispatch_id": f"nom-{number}", "corridor_id": "transfer-east-1", "specimen_event_id": f"specimen_event-{number}", "duty_date": "2026-09-25", "requested_units": requested, "priority": priority, "idempotency_key": f"key-{number}"})
        allocation = self.service.allocate("dispatch", "transfer-east-1", "2026-09-25")
        self.assertEqual(allocation["available_units"], "50000.000")
        self.assertEqual(allocation["allocations"][1]["allocated_units"], "10000.000")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        deployment = self.service.dispatch_deployment("dispatch", "deployment-1", "nom-1", "lot-1", 2)
        self.assertEqual(deployment["deployed_units"], "40000.000")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_units"], "20000.000")

    def test_scenario_is_approved_and_replayed_by_input(self) -> None:
        self.risk_record(23, "98")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "库房环境恢复", "risk_index_drop_percent": "9", "route_capacity_changes": {"transfer-east-1": "20"}, "demand_changes": {"collection-east:preservation-box": "-5"}})
        with self.assertRaises(Forbidden):
            self.service.approve_scenario("plan", "restart", 1)
        self.service.approve_scenario("risk", "restart", 1)
        first = self.service.run_scenario("plan", "restart", "2026-09-23")
        second = self.service.run_scenario("plan", "restart", "2026-09-23")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run_id"], second["run_id"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE traffic_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_api_exposes_browser_free_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        response = app.handle("GET", "/risk_records/summary/HUMIDITY", {"X-Actor-Id": "plan"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")

    def test_response_minutes_must_be_minutes_within_sane_range(self) -> None:
        for bad in (0, -45, 1441, 45.0, True, "45", None):
            with self.assertRaises(ValidationFailed):
                RoadCorridor = self.route_payload(bad)
                self.service.create_route("plan", RoadCorridor)
        route = self.service.create_route("plan", self.route_payload(45, corridor_id="ok-45"))
        self.assertEqual(route["response_minutes"], 45)
        self.assertFalse(route["duration_ambiguous"])
        self.assertEqual(route["response_minutes_text"], "45 分钟")
        self.assertEqual(route["response_hours"], "0.75")

    @staticmethod
    def route_payload(response_minutes: object, *, corridor_id: str = "bad-route") -> dict[str, object]:
        return {
            "corridor_id": corridor_id,
            "origin_center_id": "collection-east",
            "destination_center_id": "receiving-vault-b",
            "preservation_resource_kind": "preservation-box",
            "hourly_capacity": "10",
            "delay_basis_points": 0,
            "response_minutes": response_minutes,
        }

    def test_expected_arrival_adds_minutes_not_hours(self) -> None:
        self.service.create_route("plan", self.route_payload(45, corridor_id="cold-45"))
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-45", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "A", "quantity_units": "10", "unit_cost_cny": "1", "received_at": "2026-09-24T06:00:00Z"})
        self.service.submit_dispatch("dispatch", {"dispatch_id": "nom-45", "corridor_id": "cold-45", "specimen_event_id": "bug-live", "duty_date": "2026-09-25", "requested_units": "1", "priority": 10, "idempotency_key": "key-45"})
        self.service.allocate("dispatch", "cold-45", "2026-09-25")
        deployment = self.service.dispatch_deployment("dispatch", "dep-45", "nom-45", "lot-45", 2)
        self.assertEqual(deployment["departed_at"], "2026-09-24T08:00:00Z")
        self.assertEqual(deployment["expected_arrival"], "2026-09-24T08:45:00Z")
        self.assertEqual(deployment["expected_arrival_local"], "2026-09-24T16:45:00+08:00")
        history = self.service.deployment("dep-45", "dispatch")
        self.assertFalse(history["overdue"])

    def test_overdue_flips_after_eta_and_clears_on_early_arrival(self) -> None:
        # 提前签收：在 ETA（08:45）之前以实际到达时刻 08:40 签收，判定不超时。
        self.service.create_route("plan", self.route_payload(45, corridor_id="cold-early"))
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-early", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "A", "quantity_units": "10", "unit_cost_cny": "1", "received_at": "2026-09-24T06:00:00Z"})
        self.service.submit_dispatch("dispatch", {"dispatch_id": "nom-early", "corridor_id": "cold-early", "specimen_event_id": "bug-early", "duty_date": "2026-09-25", "requested_units": "1", "priority": 10, "idempotency_key": "key-early"})
        self.service.allocate("dispatch", "cold-early", "2026-09-25")
        self.service.dispatch_deployment("dispatch", "dep-early", "nom-early", "lot-early", 2)
        delivered = self.service.confirm_arrival("dispatch", "dep-early", "2026-09-24T08:40:00Z")
        self.assertFalse(delivered["overdue"])
        self.assertEqual(delivered["state"], "delivered")
        # 未签收单：时钟越过 ETA 后立即判超时。
        self.service.create_route("plan", self.route_payload(45, corridor_id="cold-due"))
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-due", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "A", "quantity_units": "10", "unit_cost_cny": "1", "received_at": "2026-09-24T06:00:00Z"})
        self.service.submit_dispatch("dispatch", {"dispatch_id": "nom-due", "corridor_id": "cold-due", "specimen_event_id": "bug-due", "duty_date": "2026-09-25", "requested_units": "1", "priority": 10, "idempotency_key": "key-due"})
        self.service.allocate("dispatch", "cold-due", "2026-09-25")
        self.service.dispatch_deployment("dispatch", "dep-due", "nom-due", "lot-due", 2)
        self.clock.advance(minutes=46)
        self.assertTrue(self.service.deployment("dep-due", "dispatch")["overdue"])

    def test_ambiguous_legacy_route_is_flagged_and_blocks_dispatch(self) -> None:
        payload = self.route_payload(45, corridor_id="legacy-x")
        payload.pop("response_minutes")
        payload.update({"response_time": 45, "response_time_unit": "unknown"})
        route = self.service.create_route("plan", payload)
        self.assertTrue(route["duration_ambiguous"])
        self.assertIsNone(route["response_minutes"])
        self.assertIn("ambiguous", route["duration_note"])
        with self.assertRaises(InvalidState):
            self.service.submit_dispatch("dispatch", {"dispatch_id": "nom-x", "corridor_id": "legacy-x", "specimen_event_id": "bug-x", "duty_date": "2026-09-25", "requested_units": "1", "priority": 10, "idempotency_key": "key-x"})
        with self.assertRaises(ValidationFailed):
            self.service.clarify_route_duration("plan", "legacy-x", 0)
        clarified = self.service.clarify_route_duration("plan", "legacy-x", 45)
        self.assertEqual(clarified["response_minutes"], 45)
        self.assertFalse(clarified["duration_ambiguous"])

    def test_legacy_hours_normalize_to_minutes(self) -> None:
        payload = self.route_payload(2, corridor_id="legacy-h")
        payload.pop("response_minutes")
        payload.update({"response_time": 2, "response_time_unit": "hour"})
        route = self.service.create_route("plan", payload)
        self.assertEqual(route["response_minutes"], 120)
        self.assertEqual(route["response_time_unit"], "hour")

    def test_missing_duration_is_rejected_not_silently_ambiguous(self) -> None:
        payload = self.route_payload(45, corridor_id="no-duration")
        payload.pop("response_minutes")
        with self.assertRaises(ValidationFailed):
            self.service.create_route("plan", payload)
        with self.assertRaises(ValidationFailed):
            self.service.create_route("plan", {**self.route_payload(45, corridor_id="bad-unit"),
                                               "response_minutes": None})

    def test_api_surface_uses_single_semantics(self) -> None:
        app = JsonApplication(self.service)
        created = app.handle("POST", "/road_corridors", {"X-Actor-Id": "plan"}, json.dumps(self.route_payload(45, corridor_id="api-45")).encode())
        self.assertEqual(created.status, 201)
        self.assertEqual(created.body["response_minutes"], 45)
        shown = app.handle("GET", "/road_corridors/api-45", {"X-Actor-Id": "plan"})
        self.assertEqual(shown.body["response_minutes_text"], "45 分钟")
        bad = app.handle("POST", "/road_corridors", {"X-Actor-Id": "plan"}, json.dumps(self.route_payload(0, corridor_id="api-zero")).encode())
        self.assertEqual(bad.status, 422)
        clarified = app.handle("POST", "/road_corridors/api-45/clarify_duration", {"X-Actor-Id": "plan"}, b'{"response_minutes": 50}')
        self.assertEqual(clarified.body["response_minutes"], 50)


class LegacySchemaMigrationTests(unittest.TestCase):
    def test_preexisting_minute_rows_stay_minutes_out_of_range_rows_flagged(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE traffic_users (user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, role TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE TABLE response_centers (center_id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                timezone TEXT NOT NULL, capacity_units TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE TABLE road_corridors (corridor_id TEXT PRIMARY KEY, origin_center_id TEXT NOT NULL,
                destination_center_id TEXT NOT NULL, preservation_resource_kind TEXT NOT NULL,
                hourly_capacity TEXT NOT NULL, delay_basis_points INTEGER NOT NULL,
                response_minutes INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL);
            CREATE TABLE dispatch_requests (dispatch_id TEXT PRIMARY KEY, corridor_id TEXT NOT NULL,
                specimen_event_id TEXT NOT NULL, duty_date TEXT NOT NULL, requested_units TEXT NOT NULL,
                allocated_units TEXT NOT NULL DEFAULT '0', arrived_units TEXT NOT NULL DEFAULT '0',
                priority INTEGER NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                idempotency_key TEXT NOT NULL, submitted_by TEXT NOT NULL, submitted_at TEXT NOT NULL);
            CREATE TABLE deployments (deployment_id TEXT PRIMARY KEY, dispatch_id TEXT NOT NULL,
                inventory_preservation_resource_lot_id TEXT NOT NULL, deployed_units TEXT NOT NULL,
                expected_arrived_units TEXT NOT NULL, departed_at TEXT NOT NULL, arrived_at TEXT,
                state TEXT NOT NULL DEFAULT 'in_transit', revision INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL);
            INSERT INTO traffic_users VALUES ('plan','plan','planner',1,'2026-01-01T00:00:00Z');
            INSERT INTO response_centers VALUES ('a','A','storage','UTC','1',1,'2026-01-01T00:00:00Z');
            INSERT INTO response_centers VALUES ('b','B','receiving-vault','UTC','1',1,'2026-01-01T00:00:00Z');
            INSERT INTO road_corridors VALUES ('ok','a','b','preservation-box','1',0,45,1,'active','2026-01-01T00:00:00Z');
            INSERT INTO road_corridors VALUES ('huge','a','b','preservation-box','1',0,99999,1,'active','2026-01-01T00:00:00Z');
            INSERT INTO dispatch_requests VALUES ('d-ok','ok','e','2026-01-01','1','1','0',10,'in_transit',1,'k','plan','2026-01-01T00:00:00Z');
            INSERT INTO dispatch_requests VALUES ('d-huge','huge','e','2026-01-01','1','1','0',10,'in_transit',1,'k2','plan','2026-01-01T00:00:00Z');
            INSERT INTO deployments VALUES ('dep-ok','d-ok','lot','1','1','2026-01-01T00:00:00Z',NULL,'in_transit',1,'plan','2026-01-01T00:00:00Z');
            INSERT INTO deployments VALUES ('dep-huge','d-huge','lot','1','1','2026-01-01T00:00:00Z',NULL,'in_transit',1,'plan','2026-01-01T00:00:00Z');
            """
        )
        initialize(connection)
        service = CollectionLogisticsService(connection)
        ok = service.route("ok")
        self.assertEqual(ok["response_minutes"], 45)
        self.assertEqual(ok["response_time_unit"], "minute")
        self.assertFalse(ok["duration_ambiguous"])
        huge = service.route("huge")
        self.assertTrue(huge["duration_ambiguous"])
        self.assertIsNone(huge["response_minutes"])
        self.assertEqual(huge["legacy_response_time"], 99999)
        # 明确分钟的旧部署按分钟回填 ETA（45 分钟，而非 45 小时）。
        backfilled = connection.execute(
            "SELECT expected_arrival_at FROM deployments WHERE deployment_id='dep-ok'"
        ).fetchone()
        self.assertEqual(backfilled["expected_arrival_at"], "2026-01-01T00:45:00Z")
        # 单位不明的旧部署不猜测，ETA 保持为空。
        not_backfilled = connection.execute(
            "SELECT expected_arrival_at FROM deployments WHERE deployment_id='dep-huge'"
        ).fetchone()
        self.assertIsNone(not_backfilled["expected_arrival_at"])
        connection.close()


if __name__ == "__main__":
    unittest.main()
