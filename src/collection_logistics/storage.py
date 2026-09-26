"""标本事件快处服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .clock import add_minutes, parse_utc, utc_text


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS traffic_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_index_risk_records (
    risk_record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    risk_index TEXT NOT NULL,
    duty_date TEXT NOT NULL,
    index_value TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_risk_record_id INTEGER REFERENCES risk_index_risk_records(risk_record_id),
    recorded_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(risk_index, duty_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_risk_records_series
ON risk_index_risk_records(risk_index, duty_date, risk_record_id);

CREATE TABLE IF NOT EXISTS response_centers (
    center_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_units TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS road_corridors (
    corridor_id TEXT PRIMARY KEY,
    origin_center_id TEXT NOT NULL REFERENCES response_centers(center_id),
    destination_center_id TEXT NOT NULL REFERENCES response_centers(center_id),
    preservation_resource_kind TEXT NOT NULL,
    hourly_capacity TEXT NOT NULL,
    delay_basis_points INTEGER NOT NULL,
    response_minutes INTEGER,
    response_time_unit TEXT NOT NULL DEFAULT 'minute'
        CHECK(response_time_unit IN ('minute','hour','ambiguous')),
    legacy_response_time INTEGER,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_center_id <> destination_center_id),
    CHECK(
        (response_minutes IS NULL AND response_time_unit = 'ambiguous')
        OR (response_minutes IS NOT NULL
            AND response_minutes BETWEEN 1 AND 1440
            AND response_time_unit IN ('minute','hour'))
    )
);

CREATE TABLE IF NOT EXISTS corridor_restrictions (
    restriction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    corridor_id TEXT NOT NULL REFERENCES road_corridors(corridor_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON corridor_restrictions(corridor_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS preservation_resource_lots (
    preservation_resource_lot_id TEXT PRIMARY KEY,
    center_id TEXT NOT NULL REFERENCES response_centers(center_id),
    preservation_resource_kind TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_units TEXT NOT NULL,
    available_units TEXT NOT NULL,
    unit_cost_cny TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON preservation_resource_lots(center_id, preservation_resource_kind, received_at);

CREATE TABLE IF NOT EXISTS preservation_resource_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    preservation_resource_lot_id TEXT NOT NULL REFERENCES preservation_resource_lots(preservation_resource_lot_id),
    delta_units TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatch_requests (
    dispatch_id TEXT PRIMARY KEY,
    corridor_id TEXT NOT NULL REFERENCES road_corridors(corridor_id),
    specimen_event_id TEXT NOT NULL,
    duty_date TEXT NOT NULL,
    requested_units TEXT NOT NULL,
    allocated_units TEXT NOT NULL DEFAULT '0',
    arrived_units TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispatch_requests_schedule
ON dispatch_requests(corridor_id, duty_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS dispatch_plans (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    corridor_id TEXT NOT NULL REFERENCES road_corridors(corridor_id),
    duty_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_units TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(corridor_id, duty_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS deployments (
    deployment_id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL UNIQUE REFERENCES dispatch_requests(dispatch_id),
    inventory_preservation_resource_lot_id TEXT NOT NULL REFERENCES preservation_resource_lots(preservation_resource_lot_id),
    deployed_units TEXT NOT NULL,
    expected_arrived_units TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    expected_arrival_at TEXT,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit' CHECK(state IN ('in_transit','delivered','disputed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS response_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS response_scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES response_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS traffic_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS traffic_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_traffic_audit_entity
ON traffic_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # ThreadingHTTPServer 会在工作线程中复用同一连接：WAL + busy_timeout +
    # 写事务一律 BEGIN IMMEDIATE 保证跨线程串行写入。
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(connection: sqlite3.Connection) -> None:
    """升级旧版数据库，且不静默篡改历史记录的单位语义。

    旧表 road_corridors.response_minutes 在登记契约上就是分钟，因此存量
    行默认标记为 minute；但落在新合理范围（1..1440）之外的值无法再被
    无歧义解释，原值保留在 legacy_response_time 并标记为 ambiguous
    （旧列为 NOT NULL，数值保留原位、不做任何单位猜测）。
    """
    corridor_columns = _columns(connection, "road_corridors")
    if corridor_columns and "response_time_unit" not in corridor_columns:
        connection.execute("ALTER TABLE road_corridors ADD COLUMN response_time_unit TEXT NOT NULL DEFAULT 'minute'")
    if corridor_columns and "legacy_response_time" not in corridor_columns:
        connection.execute("ALTER TABLE road_corridors ADD COLUMN legacy_response_time INTEGER")
        connection.execute(
            "UPDATE road_corridors SET legacy_response_time=response_minutes, response_time_unit='ambiguous' "
            "WHERE response_minutes IS NULL OR response_minutes NOT BETWEEN 1 AND 1440"
        )
    deployment_columns = _columns(connection, "deployments")
    if deployment_columns and "expected_arrival_at" not in deployment_columns:
        connection.execute("ALTER TABLE deployments ADD COLUMN expected_arrival_at TEXT")
        # 旧部署没有持久化 ETA：仅当路线时长单位明确时，按同一分钟语义
        # 从带时区的 departed_at 回填；ambiguous 路线保持 NULL，不猜测。
        rows = connection.execute(
            "SELECT d.deployment_id, d.departed_at, r.response_minutes, r.response_time_unit "
            "FROM deployments d JOIN dispatch_requests n ON n.dispatch_id=d.dispatch_id "
            "JOIN road_corridors r ON r.corridor_id=n.corridor_id "
            "WHERE d.expected_arrival_at IS NULL"
        ).fetchall()
        for row in rows:
            if row["response_time_unit"] == "ambiguous" or row["response_minutes"] is None:
                continue
            arrival = utc_text(add_minutes(parse_utc(row["departed_at"], "departed_at"), int(row["response_minutes"])))
            connection.execute(
                "UPDATE deployments SET expected_arrival_at=? WHERE deployment_id=?",
                (arrival, row["deployment_id"]),
            )


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    _migrate(connection)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
