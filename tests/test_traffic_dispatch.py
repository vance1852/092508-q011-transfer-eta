from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from collection_logistics.acceptance import run as run_logistics_acceptance
from collection_logistics.api import JsonApplication
from collection_logistics.clock import FrozenClock, shift_minutes, utc_text
from collection_logistics.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from collection_logistics.planning import AllocationRequest, RiskPoint, allocate_capacity, latest_streak
from collection_logistics.service import CollectionLogisticsService
from collection_logistics.risk import DemandBucket, inventory_coverage, mark_to_risk, traffic_gap


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


class ShiftMinutesTests(unittest.TestCase):
    def test_requires_timezone_aware_moment(self) -> None:
        with self.assertRaises(ValueError):
            shift_minutes(datetime(2026, 9, 24, 8, 0), 45)

    def test_crosses_day_boundary_in_utc(self) -> None:
        arrived = shift_minutes(datetime(2026, 9, 24, 23, 30, tzinfo=timezone.utc), 45)
        self.assertEqual(utc_text(arrived), "2026-09-25T00:15:00Z")

    def test_spring_forward_uses_elapsed_time_not_wall_clock(self) -> None:
        ny = ZoneInfo("America/New_York")
        departed = datetime(2026, 3, 8, 1, 30, tzinfo=ny)  # EST，02:00 拨快前
        arrived = shift_minutes(departed, 45)
        self.assertEqual(utc_text(arrived), "2026-03-08T07:15:00Z")
        local = arrived.astimezone(ny)
        self.assertEqual((local.hour, local.minute), (3, 15))  # 02:00-02:59 本地时间不存在
        self.assertEqual(local.utcoffset(), timedelta(hours=-4))

    def test_fall_back_uses_elapsed_time_not_wall_clock(self) -> None:
        ny = ZoneInfo("America/New_York")
        departed = datetime(2026, 11, 1, 1, 30, fold=0, tzinfo=ny)  # EDT，拨回前
        arrived = shift_minutes(departed, 45)
        self.assertEqual(utc_text(arrived), "2026-11-01T06:15:00Z")
        local = arrived.astimezone(ny)
        self.assertEqual((local.hour, local.minute, local.fold), (1, 15, 1))  # 落在重复小时内
        self.assertEqual(local.utcoffset(), timedelta(hours=-5))


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

    def prepare_allocated(self, corridor_id: str = "transfer-east-1") -> None:
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "10000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.submit_dispatch("dispatch", {"dispatch_id": "nom-1", "corridor_id": corridor_id, "specimen_event_id": "evt-1", "duty_date": "2026-09-25", "requested_units": "100", "priority": 10, "idempotency_key": "key-1"})
        self.service.allocate("dispatch", corridor_id, "2026-09-25")

    def test_expected_arrival_uses_registered_minutes_not_hours(self) -> None:
        self.service.create_route("plan", {"corridor_id": "transfer-cold-45", "origin_center_id": "collection-east", "destination_center_id": "receiving-vault-b", "preservation_resource_kind": "preservation-box", "hourly_capacity": "100000", "delay_basis_points": 25, "response_minutes": 45})
        self.prepare_allocated("transfer-cold-45")
        deployment = self.service.dispatch_deployment("dispatch", "dep-1", "nom-1", "lot-1", 2)
        self.assertEqual(deployment["expected_arrival"], "2026-09-24T08:45:00Z")

    def test_expected_arrival_crosses_day_boundary(self) -> None:
        self.service.create_route("plan", {"corridor_id": "transfer-cold-45", "origin_center_id": "collection-east", "destination_center_id": "receiving-vault-b", "preservation_resource_kind": "preservation-box", "hourly_capacity": "100000", "delay_basis_points": 25, "response_minutes": 45})
        self.prepare_allocated("transfer-cold-45")
        self.clock.advance(hours=15, minutes=30)  # 2026-09-24T23:30:00Z 出发
        deployment = self.service.dispatch_deployment("dispatch", "dep-1", "nom-1", "lot-1", 2)
        self.assertEqual(deployment["expected_arrival"], "2026-09-25T00:15:00Z")

    def test_response_minutes_bounds_rejected_before_write(self) -> None:
        base = {"corridor_id": "r-x", "origin_center_id": "collection-east", "destination_center_id": "receiving-vault-b", "preservation_resource_kind": "preservation-box", "hourly_capacity": "100", "delay_basis_points": 0}
        for bad in (0, -5, 1.5, "45", True, 10081):
            with self.assertRaises(ValidationFailed, msg=f"response_minutes={bad!r}"):
                self.service.create_route("plan", {**base, "response_minutes": bad})
        for good in (1, 45, 10080):
            created = self.service.create_route("plan", {**base, "corridor_id": f"r-ok-{good}", "response_minutes": good})
            self.assertEqual(created["response_minutes"], good)

    def test_api_rejects_invalid_response_minutes(self) -> None:
        app = JsonApplication(self.service)
        payload = json.dumps({"corridor_id": "r-api", "origin_center_id": "collection-east", "destination_center_id": "receiving-vault-b", "preservation_resource_kind": "preservation-box", "hourly_capacity": "100", "delay_basis_points": 0, "response_minutes": 0}).encode()
        response = app.handle("POST", "/road_corridors", {"X-Actor-Id": "plan"}, payload)
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

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


LEGACY_ROAD_CORRIDORS_DDL = """
CREATE TABLE road_corridors (
    corridor_id TEXT PRIMARY KEY,
    origin_center_id TEXT NOT NULL REFERENCES response_centers(center_id),
    destination_center_id TEXT NOT NULL REFERENCES response_centers(center_id),
    preservation_resource_kind TEXT NOT NULL,
    hourly_capacity TEXT NOT NULL,
    delay_basis_points INTEGER NOT NULL,
    response_minutes INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_center_id <> destination_center_id)
);
"""


class LegacyResponseMinutesTests(unittest.TestCase):
    """约束加入前的旧库记录：按分钟无歧义识别，无法识别时明确报错。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(LEGACY_ROAD_CORRIDORS_DDL)  # 模拟没有 CHECK 约束的旧表
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = CollectionLogisticsService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"center_id": "collection-east", "name": "北部标本事件保藏中心", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "500000"})
        self.service.create_facility("plan", {"center_id": "receiving-vault-b", "name": "沿海终端", "kind": "receiving-vault", "timezone": "Asia/Shanghai", "capacity_units": "800000"})

    def tearDown(self) -> None:
        self.connection.close()

    def insert_legacy_route(self, corridor_id: str, response_minutes: object) -> None:
        self.connection.execute(
            "INSERT INTO road_corridors(corridor_id,origin_center_id,destination_center_id,preservation_resource_kind,"
            "hourly_capacity,delay_basis_points,response_minutes,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (corridor_id, "collection-east", "receiving-vault-b", "preservation-box", "100000", 25, response_minutes, "2026-09-20T00:00:00Z"),
        )

    def prepare_allocated(self, corridor_id: str) -> None:
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "10000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.submit_dispatch("dispatch", {"dispatch_id": "nom-1", "corridor_id": corridor_id, "specimen_event_id": "evt-1", "duty_date": "2026-09-25", "requested_units": "100", "priority": 10, "idempotency_key": "key-1"})
        self.service.allocate("dispatch", corridor_id, "2026-09-25")

    def test_legacy_minutes_record_computes_as_minutes(self) -> None:
        self.insert_legacy_route("legacy-ok", 45)
        self.assertEqual(self.service.route("legacy-ok")["response_minutes"], 45)
        self.prepare_allocated("legacy-ok")
        deployment = self.service.dispatch_deployment("dispatch", "dep-1", "nom-1", "lot-1", 2)
        self.assertEqual(deployment["expected_arrival"], "2026-09-24T08:45:00Z")

    def test_ambiguous_legacy_records_are_flagged_not_guessed(self) -> None:
        for corridor_id, stored in (("legacy-zero", 0), ("legacy-negative", -30), ("legacy-huge", 999999), ("legacy-fraction", 45.5)):
            self.insert_legacy_route(corridor_id, stored)
            with self.assertRaises(InvalidState, msg=f"{corridor_id}={stored!r}") as caught:
                self.service.route(corridor_id)
            self.assertIn(corridor_id, str(caught.exception))
            with self.assertRaises(InvalidState, msg=f"dispatch on {corridor_id}"):
                self.service.submit_dispatch("dispatch", {"dispatch_id": f"nom-{corridor_id}", "corridor_id": corridor_id, "specimen_event_id": "evt-1", "duty_date": "2026-09-25", "requested_units": "100", "priority": 10, "idempotency_key": f"key-{corridor_id}"})

    def test_corrupted_route_blocks_deployment(self) -> None:
        self.insert_legacy_route("legacy-drift", 45)
        self.prepare_allocated("legacy-drift")
        self.connection.execute("UPDATE road_corridors SET response_minutes=0 WHERE corridor_id='legacy-drift'")
        with self.assertRaises(InvalidState):
            self.service.dispatch_deployment("dispatch", "dep-1", "nom-1", "lot-1", 2)


class LogisticsAcceptanceTests(unittest.TestCase):
    def test_acceptance_deployment_eta_uses_minutes(self) -> None:
        result = run_logistics_acceptance(Path(__file__).resolve().parents[1])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["deployment"]["expected_arrival"], "2026-09-24T08:36:00Z")


if __name__ == "__main__":
    unittest.main()
