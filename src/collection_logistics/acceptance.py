"""贯通风险指数、转运路线、应急资源库存、调度申请和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock, parse_utc
from .errors import InvalidState
from .service import CollectionLogisticsService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = CollectionLogisticsService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": f"2026-09-{index}", "index_value": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"center_id": "collection-east", "name": "北部标本事件保藏中心", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "500000"})
    service.create_facility("plan", {"center_id": "receiving-vault-b", "name": "沿海终端", "kind": "receiving-vault", "timezone": "Asia/Shanghai", "capacity_units": "800000"})
    service.create_route("plan", {"corridor_id": "transfer-east-1", "origin_center_id": "collection-east", "destination_center_id": "receiving-vault-b", "preservation_resource_kind": "preservation-box", "hourly_capacity": "100000", "delay_basis_points": 25, "response_minutes": 36})
    service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-001", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_dispatch("dispatch", {"dispatch_id": "nom-001", "corridor_id": "transfer-east-1", "specimen_event_id": "herbarium-room-east", "duty_date": "2026-09-25", "requested_units": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "transfer-east-1", "2026-09-25")
    deployment = service.dispatch_deployment("dispatch", "deployment-001", "nom-001", "lot-001", 2)

    # 响应时长必须按分钟计算：36 分钟后到达，而不是 36 小时后。
    departed = parse_utc(deployment["departed_at"])
    expected_arrival = parse_utc(deployment["expected_arrival"])
    assert int((expected_arrival - departed).total_seconds()) == 36 * 60, deployment
    assert deployment["expected_arrival"] == "2026-09-24T08:36:00Z", deployment["expected_arrival"]
    # 接收端本地时刻按目的地时区（UTC+8）换算，跨日本地日历正确。
    assert deployment["expected_arrival_local"].startswith("2026-09-24T16:36:00+08:00"), deployment["expected_arrival_local"]
    history = service.deployment("deployment-001", "dispatch")
    assert history["overdue"] is False and history["response_minutes"] == 36, history

    # 旧记录：显式声明小时的按小时归一化为分钟；单位缺失的必须明确标记且禁止调度。
    service.create_facility("plan", {"center_id": "vault-ny", "name": "纽约冷藏室", "kind": "receiving-vault", "timezone": "America/New_York", "capacity_units": "10000"})
    service.create_route("plan", {"corridor_id": "legacy-hour-1", "origin_center_id": "collection-east", "destination_center_id": "vault-ny", "preservation_resource_kind": "preservation-box", "hourly_capacity": "10", "delay_basis_points": 0, "response_time": 2, "response_time_unit": "hour"})
    assert service.route("legacy-hour-1")["response_minutes"] == 120
    service.create_route("plan", {"corridor_id": "legacy-unknown-1", "origin_center_id": "collection-east", "destination_center_id": "vault-ny", "preservation_resource_kind": "preservation-box", "hourly_capacity": "10", "delay_basis_points": 0, "response_time": 45, "response_time_unit": "unknown"})
    unknown_route = service.route("legacy-unknown-1")
    assert unknown_route["duration_ambiguous"] is True and unknown_route["response_minutes"] is None
    try:
        service.submit_dispatch("dispatch", {"dispatch_id": "nom-legacy", "corridor_id": "legacy-unknown-1", "specimen_event_id": "bug-cold-1", "duty_date": "2026-09-25", "requested_units": "1", "priority": 50, "idempotency_key": "legacy-key"})
    except InvalidState:
        pass
    else:  # pragma: no cover - 断言分支
        raise AssertionError("单位不明路线不应接受调度")
    # 馆员澄清 45 分钟后路线恢复可用。
    clarified = service.clarify_route_duration("plan", "legacy-unknown-1", 45)
    assert clarified["response_minutes"] == 45 and clarified["duration_note"].endswith("45 分钟")

    # 跨日 + 夏令时边界：2026-03-08 美国东部春令时拨表日，06:30Z（本地 01:30 EST）
    # 出发，45 分钟后为 07:15Z —— 本地直接跳到 03:15 EDT，墙上时钟前进 1 小时 45
    # 分，实际耗时只有 45 分钟；UTC 时间线跨日/拨表判断不漂移。
    service.clock.current = datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)
    service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-002", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "10", "unit_cost_cny": "91", "received_at": "2026-03-08T02:00:00Z"})
    service.submit_dispatch("dispatch", {"dispatch_id": "nom-dst", "corridor_id": "legacy-unknown-1", "specimen_event_id": "bug-cold-2", "duty_date": "2026-03-08", "requested_units": "1", "priority": 50, "idempotency_key": "dst-key"})
    service.allocate("dispatch", "legacy-unknown-1", "2026-03-08")
    dst = service.dispatch_deployment("dispatch", "deployment-dst", "nom-dst", "lot-002", 2)
    assert dst["expected_arrival"] == "2026-03-08T07:15:00Z", dst["expected_arrival"]
    assert dst["expected_arrival_local"] == "2026-03-08T03:15:00-04:00", dst["expected_arrival_local"]
    assert int((parse_utc(dst["expected_arrival"]) - parse_utc(dst["departed_at"])).total_seconds()) == 45 * 60

    service.create_scenario("plan", {"scenario_id": "storage-recovery", "name": "主干路恢复通行与标本事件需求回落", "risk_index_drop_percent": "9", "route_capacity_changes": {"transfer-east-1": "20"}, "demand_changes": {"collection-east:preservation-box": "-5"}})
    service.approve_scenario("risk", "storage-recovery", 1)
    scenario = service.run_scenario("plan", "storage-recovery", "2026-09-23")
    result = {"status": "ok", "index": service.risk_summary("HUMIDITY"), "plan_id": allocation["plan_id"], "deployment": deployment, "scenario_run_id": scenario["run_id"], "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行标本事件保藏中心调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
