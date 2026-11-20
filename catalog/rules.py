"""规则版本管理与定向重算。

核心语义：
- 每个来源一份规则，按 version 递增管理，同一时刻只有一个 active 版本。
- 修订规则 = 创建新版本并激活；回退 = 重新激活旧版本。两者走同一条重算路径。
- 重算只触及“受影响记录”：通过新旧规则 diff 得到受影响的原始单位/货币键，
  只重算原始载荷命中这些键的记录；小数格式或语言偏好变化时才扩大到全来源。
"""
import json

from .db import canon
from .normalize import normalize


def get_active_rules(conn, source_id):
    row = conn.execute(
        "SELECT version, rules_json FROM rule_sets WHERE source_id=? AND status='active'",
        (source_id,),
    ).fetchone()
    if not row:
        return None, None
    return row["version"], json.loads(row["rules_json"])


def diff_rules(old, new):
    """计算新旧规则差异，返回受影响键集合与全局变化标志。"""
    old = old or {}
    new = new or {}

    def keydiff(a, b):
        a, b = a or {}, b or {}
        return sorted((set(b) - set(a)) | (set(a) - set(b)) | {k for k in set(a) & set(b) if a[k] != b[k]})

    return {
        "unit_keys": keydiff(old.get("units"), new.get("units")),
        "currency_keys": keydiff(old.get("currencies"), new.get("currencies")),
        "decimal_changed": old.get("decimal") != new.get("decimal"),
        "name_changed": old.get("name") != new.get("name"),
    }


def create_version(conn, source_id, rules, note="", activate=True):
    """创建新规则版本；activate=True 时立即激活并定向重算。返回 (version, stats|None)。"""
    v = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM rule_sets WHERE source_id=?", (source_id,)
    ).fetchone()["v"] + 1
    conn.execute(
        "INSERT INTO rule_sets(source_id, version, status, rules_json, note) VALUES (?,?, 'inactive', ?, ?)",
        (source_id, v, canon(rules), note),
    )
    if activate:
        return v, activate_version(conn, source_id, v, trigger="recalc")
    conn.commit()
    return v, None


def activate_version(conn, source_id, version, trigger="recalc"):
    """激活指定版本（新版=修订生效，旧版=回退），只重算受影响记录。"""
    target = conn.execute(
        "SELECT * FROM rule_sets WHERE source_id=? AND version=?", (source_id, version)
    ).fetchone()
    if not target:
        raise ValueError(f"source {source_id} 没有规则版本 v{version}")
    cur_version, cur_rules = get_active_rules(conn, source_id)
    if cur_version == version:
        return {"noop": True, "to_version": version}

    new_rules = json.loads(target["rules_json"])
    conn.execute("UPDATE rule_sets SET status='inactive' WHERE source_id=? AND status='active'", (source_id,))
    conn.execute("UPDATE rule_sets SET status='active' WHERE id=?", (target["id"],))

    diff = diff_rules(cur_rules, new_rules)
    stats = _recalc_affected(conn, source_id, version, new_rules, diff, trigger)
    stats.update({"from_version": cur_version, "to_version": version, "diff": diff})
    conn.commit()
    return stats


def _recalc_affected(conn, source_id, version, rules, diff, trigger):
    """按 diff 定位受影响记录并重算。输出不变的记录只静默推进 rule_version。"""
    rows = conn.execute(
        "SELECT id, raw_json, status, quarantine_reason, norm_json FROM records WHERE source_id=?",
        (source_id,),
    ).fetchall()
    full_scope = diff["decimal_changed"] or diff["name_changed"]
    unit_keys = set(diff["unit_keys"])
    curr_keys = set(diff["currency_keys"])

    stats = {"planned": 0, "changed": 0, "recovered": 0, "quarantined": 0, "unchanged": 0}
    for row in rows:
        item = json.loads(row["raw_json"])
        if not full_scope:
            hit = str(item.get("unit") or "").strip() in unit_keys or \
                  str(item.get("currency") or "").strip() in curr_keys
            if not hit:
                continue  # 未受影响，绝不重算
        stats["planned"] += 1

        norm, status, reason = normalize(rules, item)
        new_norm = canon(norm) if norm else None
        if status == row["status"] and reason == row["quarantine_reason"] and new_norm == row["norm_json"]:
            conn.execute("UPDATE records SET rule_version=? WHERE id=?", (version, row["id"]))
            stats["unchanged"] += 1
            continue

        if row["status"] == "quarantined" and status == "normalized":
            stats["recovered"] += 1
        if status == "quarantined":
            stats["quarantined"] += 1
        conn.execute(
            "UPDATE records SET status=?, quarantine_reason=?, norm_json=?, rule_version=?,"
            " updated_at=datetime('now') WHERE id=?",
            (status, reason, new_norm, version, row["id"]),
        )
        conn.execute(
            "INSERT INTO normalization_history(record_id, rule_version, status, quarantine_reason,"
            " norm_json, trigger) VALUES (?,?,?,?,?,?)",
            (row["id"], version, status, reason, new_norm, trigger),
        )
        stats["changed"] += 1
    return stats
