"""退役评估与梯次利用服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retirement_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('operator','approver','cascade_manager','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_versions (
    policy_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    published_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (policy_id, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS components (
    component_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    chemistry TEXT NOT NULL,
    rated_capacity_kwh TEXT NOT NULL,
    acquisition_cost_cny TEXT NOT NULL,
    baseline_resistance_milliohm TEXT,
    commissioned_at TEXT NOT NULL,
    config_sha256 TEXT NOT NULL CHECK (length(config_sha256) = 64),
    registered_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS measurement_records (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id TEXT NOT NULL REFERENCES components(component_id),
    kind TEXT NOT NULL CHECK (kind IN ('capacity','resistance','repair','safety')),
    measured_at TEXT NOT NULL,
    source_batch TEXT NOT NULL,
    source_row TEXT NOT NULL,
    value TEXT,
    severity TEXT CHECK (severity IS NULL OR severity IN ('low','medium','high','critical')),
    status TEXT CHECK (status IS NULL OR status IN ('open','closed')),
    evidence_ref TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    recorded_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE (component_id, source_batch, source_row)
);

CREATE INDEX IF NOT EXISTS idx_records_component_kind_time
ON measurement_records(component_id, kind, measured_at);

CREATE TABLE IF NOT EXISTS assessments (
    assessment_id TEXT NOT NULL,
    component_id TEXT NOT NULL REFERENCES components(component_id),
    version INTEGER NOT NULL CHECK (version > 0),
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    policy_engine_version TEXT NOT NULL,
    windows_json TEXT NOT NULL,
    frozen_snapshot_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    recommendation TEXT NOT NULL CHECK (recommendation IN
        ('continue_service','derate','cascade_utilization','recycle','pending_evidence')),
    recommendation_label TEXT NOT NULL,
    explanations_json TEXT NOT NULL,
    evidence_gaps_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    residual_value_cny TEXT,
    state TEXT NOT NULL CHECK (state IN ('submitted','approved','rejected','superseded')),
    source_review_id INTEGER,
    prepared_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    prepared_at TEXT NOT NULL,
    decided_by TEXT REFERENCES retirement_users(user_id),
    decided_at TEXT,
    decision_note TEXT,
    PRIMARY KEY (assessment_id),
    UNIQUE (component_id, version),
    FOREIGN KEY (policy_id, policy_version) REFERENCES policy_versions(policy_id, version)
);

-- 同一组件同一时刻至多一个生效结论。
CREATE UNIQUE INDEX IF NOT EXISTS one_effective_assessment_per_component
ON assessments(component_id)
WHERE state = 'approved';

CREATE TABLE IF NOT EXISTS assessment_approvals (
    approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id TEXT NOT NULL UNIQUE REFERENCES assessments(assessment_id),
    component_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved','rejected')),
    note TEXT NOT NULL,
    approved_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    approved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_requests (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id TEXT NOT NULL REFERENCES assessments(assessment_id),
    component_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_windows_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending','accepted','rejected')),
    requested_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    requested_at TEXT NOT NULL,
    reviewed_by TEXT REFERENCES retirement_users(user_id),
    reviewed_at TEXT,
    review_note TEXT,
    spawned_assessment_id TEXT REFERENCES assessments(assessment_id)
);

CREATE TABLE IF NOT EXISTS cascade_batches (
    batch_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('forming','sealed','withdrawn','failed','consumed')),
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    created_at TEXT NOT NULL,
    sealed_at TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS cascade_batch_items (
    batch_id TEXT NOT NULL REFERENCES cascade_batches(batch_id),
    component_id TEXT NOT NULL REFERENCES components(component_id),
    assessment_id TEXT NOT NULL REFERENCES assessments(assessment_id),
    available_kwh TEXT NOT NULL,
    unit_value_cny_per_kwh TEXT NOT NULL,
    item_state TEXT NOT NULL DEFAULT 'active' CHECK (item_state IN ('active','released')),
    released_reason TEXT CHECK (released_reason IS NULL OR
        released_reason IN ('batch_withdrawn','batch_failed','project_confirmed')),
    added_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    added_at TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY (batch_id, component_id)
);

-- 同一组件不能同时存在于两个进行中的候选批次；历史行以 item_state='released' 保留。
CREATE UNIQUE INDEX IF NOT EXISTS one_active_batch_per_component
ON cascade_batch_items(component_id)
WHERE item_state = 'active';

CREATE TABLE IF NOT EXISTS cascade_projects (
    project_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES cascade_batches(batch_id),
    requested_capacity_kwh TEXT NOT NULL,
    reserved_capacity_kwh TEXT NOT NULL DEFAULT '0',
    state TEXT NOT NULL CHECK (state IN ('reserved','confirmed','released','failed')),
    hold_expires_at TEXT NOT NULL,
    release_reason TEXT CHECK (release_reason IS NULL OR
        release_reason IN ('expired','withdrawn','project_failed','project_confirmed')),
    created_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    released_at TEXT
);

CREATE TABLE IF NOT EXISTS capacity_holds (
    hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES cascade_projects(project_id),
    batch_id TEXT NOT NULL REFERENCES cascade_batches(batch_id),
    component_id TEXT NOT NULL REFERENCES components(component_id),
    held_kwh TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('held','released')),
    release_reason TEXT CHECK (release_reason IS NULL OR
        release_reason IN ('expired','withdrawn','project_failed','project_confirmed')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT,
    UNIQUE (project_id, component_id)
);

-- 容量预留不得被两个项目重复占用。
CREATE UNIQUE INDEX IF NOT EXISTS one_active_hold_per_component
ON capacity_holds(component_id)
WHERE state = 'held';

CREATE TABLE IF NOT EXISTS disposition_confirmations (
    confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id TEXT NOT NULL UNIQUE REFERENCES components(component_id),
    assessment_id TEXT NOT NULL REFERENCES assessments(assessment_id),
    final_destination TEXT NOT NULL CHECK (final_destination IN
        ('continued','derated','cascade','recycled')),
    reference TEXT NOT NULL,
    confirmed_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    confirmed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retirement_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_retirement_audit_entity
ON retirement_audit_events(entity_type, entity_id, event_id);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "retirement_users", "policy_versions", "components", "measurement_records",
    "assessments", "assessment_approvals", "review_requests", "cascade_batches",
    "cascade_batch_items", "cascade_projects", "capacity_holds", "disposition_confirmations",
    "retirement_audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用外键、WAL 与严格事务设置。

    HTTP 服务以多线程方式复用同一连接，故关闭同线程限制；
    所有写操作均通过 BEGIN IMMEDIATE 串行化，配合 busy_timeout 保证安全。
    """

    connection = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=False, timeout=10
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化模式，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
