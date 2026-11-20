"""SQLite 存储层：schema 与连接助手。"""
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'fixture',
  location TEXT NOT NULL,
  available INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS rule_sets (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  version INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'inactive',   -- active | inactive
  rules_json TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(source_id, version)
);

CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  external_id TEXT NOT NULL,
  raw_json TEXT NOT NULL,              -- 原文（采集到的原始载荷）
  content_hash TEXT NOT NULL,          -- 去重/变更检测
  status TEXT NOT NULL,                -- normalized | quarantined
  quarantine_reason TEXT,              -- 隔离原因，如 unknown_unit:袋
  rule_version INTEGER,                -- 最近一次归一化所用的规则版本
  norm_json TEXT,                      -- 归一化结果（隔离时为空）
  first_seen_batch INTEGER,
  last_seen_batch INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(source_id, external_id)
);

CREATE TABLE IF NOT EXISTS normalization_history (
  id INTEGER PRIMARY KEY,
  record_id INTEGER NOT NULL REFERENCES records(id),
  rule_version INTEGER,
  status TEXT NOT NULL,
  quarantine_reason TEXT,
  norm_json TEXT,
  trigger TEXT NOT NULL,               -- collect | recalc | rollback
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running'   -- ok | partial | failed
);

CREATE TABLE IF NOT EXISTS batch_sources (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  source_id INTEGER NOT NULL REFERENCES sources(id),
  status TEXT NOT NULL,                -- ok | error
  error TEXT,
  fetched INTEGER NOT NULL DEFAULT 0,
  inserted INTEGER NOT NULL DEFAULT 0,
  updated INTEGER NOT NULL DEFAULT 0,
  unchanged INTEGER NOT NULL DEFAULT 0,
  quarantined INTEGER NOT NULL DEFAULT 0,
  duplicates INTEGER NOT NULL DEFAULT 0
);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def canon(obj):
    """稳定序列化，保证哈希与字符串比较有确定结果。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
