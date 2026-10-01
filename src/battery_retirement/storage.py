"""退役评估与梯次利用服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS retirement_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retirement_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('engineer','approver','planner','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS components (
    component_id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    chemistry TEXT NOT NULL,
    nominal_capacity_kwh TEXT NOT NULL,
    current_config_revision INTEGER,
    created_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS component_configs (
    component_id TEXT NOT NULL REFERENCES components(component_id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    config_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (component_id, revision)
);

CREATE TABLE IF NOT EXISTS retirement_policies (
    policy_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (policy_id, version),
    UNIQUE (content_sha256)
);

-- 检测与质量事件一经入库即不可变；评估窗口只按 recorded_at 冻结，不会覆盖旧结论。
CREATE TABLE IF NOT EXISTS measurements (
    measurement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id TEXT NOT NULL REFERENCES components(component_id),
    record_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('capacity_retention_percent','internal_resistance_percent')),
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    measured_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    imported_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    imported_at TEXT NOT NULL,
    UNIQUE (component_id, record_id)
);

CREATE INDEX IF NOT EXISTS idx_measurements_component_time
ON measurements(component_id, recorded_at);

CREATE TABLE IF NOT EXISTS quality_events (
    event_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id TEXT NOT NULL REFERENCES components(component_id),
    event_id TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('maintenance','safety')),
    severity TEXT NOT NULL CHECK (severity IN ('minor','major','critical')),
    source TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    resolved INTEGER NOT NULL CHECK (resolved IN (0,1)),
    note TEXT,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    imported_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    imported_at TEXT NOT NULL,
    UNIQUE (component_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_events_component_time
ON quality_events(component_id, recorded_at);

CREATE TABLE IF NOT EXISTS assessments (
    assessment_id TEXT PRIMARY KEY,
    component_id TEXT NOT NULL REFERENCES components(component_id),
    serial INTEGER NOT NULL CHECK (serial > 0),
    state TEXT NOT NULL CHECK (state IN ('open','submitted','approved','rejected','superseded')),
    config_revision INTEGER NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    window_cutoff TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    result_json TEXT NOT NULL,
    conclusion TEXT CHECK (conclusion IN ('continue_service','derating','cascade','recycle','pending_evidence')),
    supersedes_assessment_id TEXT REFERENCES assessments(assessment_id),
    opened_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    opened_at TEXT NOT NULL,
    submitted_by TEXT REFERENCES retirement_users(user_id),
    submitted_at TEXT,
    approved_by TEXT REFERENCES retirement_users(user_id),
    approved_at TEXT,
    approval_note TEXT,
    effective INTEGER NOT NULL DEFAULT 0 CHECK (effective IN (0,1)),
    UNIQUE (component_id, serial),
    FOREIGN KEY (policy_id, policy_version) REFERENCES retirement_policies(policy_id, version)
);

-- 一个退役组件在任意时刻最多只有一个生效去向。
CREATE UNIQUE INDEX IF NOT EXISTS one_effective_assessment_per_component
ON assessments(component_id)
WHERE effective = 1;

CREATE INDEX IF NOT EXISTS idx_assessments_component
ON assessments(component_id, serial);

-- 评估版本冻结时看到的候选证据及其是否被窗口接纳。
CREATE TABLE IF NOT EXISTS assessment_evidence (
    assessment_id TEXT NOT NULL REFERENCES assessments(assessment_id),
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('measurement','event')),
    evidence_ref TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    admitted INTEGER NOT NULL CHECK (admitted IN (0,1)),
    PRIMARY KEY (assessment_id, evidence_kind, evidence_ref)
);

CREATE TABLE IF NOT EXISTS review_requests (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id TEXT NOT NULL REFERENCES assessments(assessment_id),
    reason TEXT NOT NULL,
    new_evidence_refs TEXT NOT NULL,
    requested_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','accepted','rejected')),
    reviewed_by TEXT REFERENCES retirement_users(user_id),
    reviewed_at TEXT,
    review_note TEXT,
    new_assessment_id TEXT REFERENCES assessments(assessment_id)
);

-- 同一评估版本同时只能有一个待处理的复核申请。
CREATE UNIQUE INDEX IF NOT EXISTS one_open_review_per_assessment
ON review_requests(assessment_id)
WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS candidate_batches (
    batch_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('forming','sealed','closed')),
    content_sha256 TEXT,
    note TEXT,
    created_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    created_at TEXT NOT NULL,
    sealed_at TEXT
);

CREATE TABLE IF NOT EXISTS candidate_batch_items (
    batch_id TEXT NOT NULL REFERENCES candidate_batches(batch_id),
    component_id TEXT NOT NULL REFERENCES components(component_id),
    source_assessment_id TEXT NOT NULL REFERENCES assessments(assessment_id),
    capacity_kwh TEXT NOT NULL,
    added_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    added_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, component_id)
);

CREATE TABLE IF NOT EXISTS reuse_projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active','failed','closed')),
    created_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    created_at TEXT NOT NULL,
    failed_at TEXT
);

CREATE TABLE IF NOT EXISTS capacity_reservations (
    reservation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES reuse_projects(project_id),
    batch_id TEXT NOT NULL REFERENCES candidate_batches(batch_id),
    component_id TEXT NOT NULL REFERENCES components(component_id),
    source_assessment_id TEXT NOT NULL REFERENCES assessments(assessment_id),
    capacity_kwh TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('held','expired','withdrawn','consumed','failed')),
    hold_days INTEGER NOT NULL CHECK (hold_days > 0),
    holds_until TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES retirement_users(user_id),
    created_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT,
    revision INTEGER NOT NULL DEFAULT 1
);

-- 同一组件的容量不得被两个项目同时占用：任一时刻至多一条有效预留；
-- 已消耗（项目成功落地）为终态占用，过期、撤回、失败释放后可再次预留。
CREATE UNIQUE INDEX IF NOT EXISTS one_active_hold_per_component
ON capacity_reservations(component_id)
WHERE state IN ('held','consumed');

CREATE INDEX IF NOT EXISTS idx_reservations_project
ON capacity_reservations(project_id, state);

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
    "retirement_schema_meta", "retirement_users", "components", "component_configs",
    "retirement_policies", "measurements", "quality_events", "assessments",
    "assessment_evidence", "review_requests", "candidate_batches", "candidate_batch_items",
    "reuse_projects", "capacity_reservations", "retirement_audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。

    HTTP 服务使用线程化服务器，连接会在工作线程间复用；写事务由
    BEGIN IMMEDIATE 串行化，配合忙等待超时即可安全跨线程使用。
    """

    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    """初始化模式，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO retirement_schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


@contextmanager
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


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM retirement_schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
