"""采集流水线：多来源抓取、幂等入库、按当前规则归一化。

- 重复采集安全：以 (source, external_id) 为自然键，内容哈希不变则跳过，
  只更新 last_seen_batch；内容变化才更新原文并重算。
- 部分来源不可用：单来源异常只记录该来源的批次错误，不影响其他来源，
  批次整体状态记为 partial。
"""
import hashlib
import json
import os

from .db import canon
from .normalize import normalize
from .rules import get_active_rules


class SourceUnavailable(Exception):
    pass


def fetch_source(source):
    """从来源拉取原始条目列表。fixture 来源从 JSON 文件读取。"""
    if not source["available"]:
        raise SourceUnavailable(f"来源 {source['code']} 当前不可用（已标记下线）")
    try:
        with open(source["location"], encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise SourceUnavailable(f"来源 {source['code']} 读取失败: {e}")
    return data.get("items", [])


def run_collection(conn, source_ids=None):
    """执行一轮采集，返回批次 id。"""
    bid = conn.execute("INSERT INTO batches(status) VALUES ('running')").lastrowid
    if source_ids:
        marks = ",".join("?" * len(source_ids))
        sources = conn.execute(f"SELECT * FROM sources WHERE id IN ({marks}) ORDER BY id", source_ids).fetchall()
    else:
        sources = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()

    errors = 0
    for s in sources:
        try:
            items = fetch_source(s)
        except SourceUnavailable as e:
            errors += 1
            conn.execute(
                "INSERT INTO batch_sources(batch_id, source_id, status, error) VALUES (?,?, 'error', ?)",
                (bid, s["id"], str(e)),
            )
            continue
        st = ingest_items(conn, s, items, bid)
        conn.execute(
            "INSERT INTO batch_sources(batch_id, source_id, status, fetched, inserted, updated,"
            " unchanged, quarantined, duplicates) VALUES (?,?, 'ok', ?,?,?,?,?,?)",
            (bid, s["id"], st["fetched"], st["inserted"], st["updated"],
             st["unchanged"], st["quarantined"], st["duplicates"]),
        )

    status = "ok" if errors == 0 else ("partial" if errors < len(sources) else "failed")
    conn.execute("UPDATE batches SET status=?, finished_at=datetime('now') WHERE id=?", (status, bid))
    conn.commit()
    return bid


def ingest_items(conn, source, items, batch_id):
    """把一批原始条目写入库并归一化。幂等：重复采集不产生重复记录。"""
    version, rules = get_active_rules(conn, source["id"])
    stats = {"fetched": 0, "inserted": 0, "updated": 0, "unchanged": 0, "quarantined": 0, "duplicates": 0}
    seen_in_batch = set()

    for item in items:
        stats["fetched"] += 1
        ext_id = str(item.get("id") or "").strip()
        if not ext_id:
            stats["duplicates"] += 1
            continue
        if ext_id in seen_in_batch:
            stats["duplicates"] += 1
            continue
        seen_in_batch.add(ext_id)

        raw = canon(item)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        row = conn.execute(
            "SELECT * FROM records WHERE source_id=? AND external_id=?", (source["id"], ext_id)
        ).fetchone()

        if row and row["content_hash"] == digest:
            conn.execute("UPDATE records SET last_seen_batch=? WHERE id=?", (batch_id, row["id"]))
            stats["unchanged"] += 1
            continue

        norm, status, reason = normalize(rules, item)
        norm_json = canon(norm) if norm else None
        if status == "quarantined":
            stats["quarantined"] += 1

        if row:
            conn.execute(
                "UPDATE records SET raw_json=?, content_hash=?, status=?, quarantine_reason=?,"
                " norm_json=?, rule_version=?, last_seen_batch=?, updated_at=datetime('now') WHERE id=?",
                (raw, digest, status, reason, norm_json, version, batch_id, row["id"]),
            )
            conn.execute(
                "INSERT INTO normalization_history(record_id, rule_version, status, quarantine_reason,"
                " norm_json, trigger) VALUES (?,?,?,?,?, 'collect')",
                (row["id"], version, status, reason, norm_json),
            )
            stats["updated"] += 1
        else:
            rid = conn.execute(
                "INSERT INTO records(source_id, external_id, raw_json, content_hash, status,"
                " quarantine_reason, rule_version, norm_json, first_seen_batch, last_seen_batch)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (source["id"], ext_id, raw, digest, status, reason, version, norm_json, batch_id, batch_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO normalization_history(record_id, rule_version, status, quarantine_reason,"
                " norm_json, trigger) VALUES (?,?,?,?,?, 'collect')",
                (rid, version, status, reason, norm_json),
            )
            stats["inserted"] += 1
    return stats
