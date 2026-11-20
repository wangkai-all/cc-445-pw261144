"""流水线验证：重复采集、规则回退、部分来源不可用、隔离区与定向重算。"""
import copy
import json
import os
import tempfile
import unittest

from catalog.collector import ingest_items, run_collection
from catalog.db import connect, init_db
from catalog.rules import activate_version, create_version, get_active_rules
from catalog.seed import ALPHA_V1, seed


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(os.path.join(self.tmp.name, "test.db"))
        init_db(self.conn)
        seed(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # ---------- helpers ----------
    def rec(self, code, ext_id):
        return self.conn.execute(
            "SELECT r.* FROM records r JOIN sources s ON s.id=r.source_id"
            " WHERE s.code=? AND r.external_id=?", (code, ext_id)).fetchone()

    def source(self, code):
        return self.conn.execute("SELECT * FROM sources WHERE code=?", (code,)).fetchone()

    def norm(self, code, ext_id):
        r = self.rec(code, ext_id)
        return json.loads(r["norm_json"]) if r["norm_json"] else None

    def hist(self, code, ext_id):
        r = self.rec(code, ext_id)
        return self.conn.execute(
            "SELECT * FROM normalization_history WHERE record_id=? ORDER BY id", (r["id"],)).fetchall()

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]

    # ---------- 基础采集与归一化 ----------
    def test_initial_collection_normalizes_and_quarantines(self):
        bid = run_collection(self.conn)
        b = self.conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
        self.assertEqual(b["status"], "ok")
        self.assertEqual(self.count("records"), 14)

        # 多语言名 + 单位换算 + 货币映射 + 小数格式
        n = self.norm("alpha", "A-1001")
        self.assertEqual((n["name"], n["qty"], n["unit"]), ("红富士苹果", 5, "kg"))
        self.assertEqual((n["price"], n["currency"]), (1280.5, "CNY"))
        n = self.norm("alpha", "A-1002")  # 斤 -> g，factor 500
        self.assertEqual((n["qty"], n["unit"]), (5000, "g"))
        n = self.norm("bravo", "B-2002")  # 德语小数格式 1.899,95
        self.assertEqual((n["qty"], n["price"]), (2.5, 1899.95))
        n = self.norm("bravo", "B-2001")  # 名称按 de 优先
        self.assertEqual(n["name"], "Bio-Vollmilch")

        # 单位/货币无法判断 -> 隔离区
        q = {r["external_id"]: r["quarantine_reason"] for r in self.conn.execute(
            "SELECT external_id, quarantine_reason FROM records WHERE status='quarantined'")}
        self.assertEqual(q, {
            "A-1005": "unknown_unit:袋",
            "B-2004": "unknown_unit:Glas",
            "B-2005": "unknown_currency:£",
            "C-3003": "unknown_unit:dozen",
        })

    # ---------- 验证 1：重复采集 ----------
    def test_duplicate_collection_is_idempotent(self):
        run_collection(self.conn)
        n_records = self.count("records")
        n_hist = self.count("normalization_history")

        bid2 = run_collection(self.conn)  # 同样的数据再采一遍
        self.assertEqual(self.count("records"), n_records, "重复采集不得产生重复记录")
        self.assertEqual(self.count("normalization_history"), n_hist, "内容未变不得重写历史")
        parts = self.conn.execute(
            "SELECT * FROM batch_sources WHERE batch_id=?", (bid2,)).fetchall()
        for p in parts:
            self.assertEqual(p["inserted"], 0)
            self.assertEqual(p["updated"], 0)
            self.assertEqual(p["unchanged"], p["fetched"])

    def test_changed_payload_updates_and_renormalizes(self):
        run_collection(self.conn)
        src = self.source("alpha")
        changed = [{"id": "A-1001", "names": {"zh": "红富士苹果", "en": "Red Fuji Apple"},
                    "qty": "5", "unit": "千克", "price": "1,088.00", "currency": "¥"}]
        st = ingest_items(self.conn, src, changed, batch_id=999)
        self.assertEqual(st["updated"], 1)
        self.assertEqual(self.norm("alpha", "A-1001")["price"], 1088.0)
        self.assertEqual(len(self.hist("alpha", "A-1001")), 2)

    # ---------- 验证 2：规则修订只重算受影响记录，回退可恢复 ----------
    def test_rule_revision_recalculates_only_affected(self):
        run_collection(self.conn)
        alpha_id = self.source("alpha")["id"]
        h_before = {e: len(self.hist("alpha", e)) for e in
                    ["A-1001", "A-1002", "A-1003", "A-1004", "A-1005"]}

        v2 = copy.deepcopy(ALPHA_V1)
        v2["units"]["袋"] = {"canonical": "bag", "factor": 1}          # 新增：覆盖隔离记录
        v2["units"]["斤"] = {"canonical": "kg", "factor": 0.5}         # 修改：g -> kg 表示
        ver, stats = create_version(self.conn, alpha_id, v2, note="补充袋、斤改 kg", activate=True)

        self.assertEqual(ver, 2)
        # 只有原始单位为 袋/斤 的记录受影响：A-1005、A-1002
        self.assertEqual(stats["planned"], 2)
        self.assertEqual(stats["changed"], 2)
        self.assertEqual(stats["recovered"], 1)

        # A-1005 从隔离区恢复
        r = self.rec("alpha", "A-1005")
        self.assertEqual(r["status"], "normalized")
        self.assertEqual(r["rule_version"], 2)
        self.assertEqual(self.norm("alpha", "A-1005")["unit"], "bag")
        # A-1002 表示变化 5000 g -> 5 kg
        n = self.norm("alpha", "A-1002")
        self.assertEqual((n["qty"], n["unit"]), (5, "kg"))
        # 未受影响记录：rule_version 不变、历史不增、结果不动
        for ext in ["A-1001", "A-1003", "A-1004"]:
            r = self.rec("alpha", ext)
            self.assertEqual(r["rule_version"], 1, f"{ext} 不应被重算")
            self.assertEqual(len(self.hist("alpha", ext)), h_before[ext])
        # 其他来源完全不受影响
        self.assertEqual(self.rec("bravo", "B-2001")["rule_version"], 1)

    def test_rule_rollback_restores_previous_normalization(self):
        run_collection(self.conn)
        alpha_id = self.source("alpha")["id"]
        v2 = copy.deepcopy(ALPHA_V1)
        v2["units"]["袋"] = {"canonical": "bag", "factor": 1}
        v2["units"]["斤"] = {"canonical": "kg", "factor": 0.5}
        create_version(self.conn, alpha_id, v2, activate=True)
        self.assertEqual(self.rec("alpha", "A-1005")["status"], "normalized")

        stats = activate_version(self.conn, alpha_id, 1, trigger="rollback")
        self.assertEqual((stats["from_version"], stats["to_version"]), (2, 1))
        self.assertEqual(stats["planned"], 2)  # 同样只影响 袋/斤 两条

        # 回退后：A-1005 重新隔离，A-1002 恢复 g 表示
        self.assertEqual(self.rec("alpha", "A-1005")["status"], "quarantined")
        self.assertEqual(self.rec("alpha", "A-1005")["quarantine_reason"], "unknown_unit:袋")
        n = self.norm("alpha", "A-1002")
        self.assertEqual((n["qty"], n["unit"]), (5000, "g"))
        # 回退也写入历史，trigger=rollback
        triggers = [h["trigger"] for h in self.hist("alpha", "A-1005")]
        self.assertEqual(triggers, ["collect", "recalc", "rollback"])

    # ---------- 验证 3：部分来源不可用 ----------
    def test_partial_source_unavailability(self):
        run_collection(self.conn)  # 先有一轮正常数据
        self.conn.execute("UPDATE sources SET available=0 WHERE code='charlie'")

        bid = run_collection(self.conn)
        b = self.conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
        self.assertEqual(b["status"], "partial")

        parts = {p["code"]: p for p in self.conn.execute(
            "SELECT bs.*, s.code FROM batch_sources bs JOIN sources s ON s.id=bs.source_id"
            " WHERE bs.batch_id=?", (bid,))}
        self.assertEqual(parts["charlie"]["status"], "error")
        self.assertIn("不可用", parts["charlie"]["error"])
        self.assertEqual(parts["alpha"]["status"], "ok")
        self.assertEqual(parts["bravo"]["status"], "ok")
        # 不可用来源的既有数据保留，其他来源正常采集
        self.assertEqual(self.count("records"), 14)
        self.assertIsNotNone(self.rec("charlie", "C-3001"))

        # 来源恢复后采集正常
        self.conn.execute("UPDATE sources SET available=1 WHERE code='charlie'")
        bid3 = run_collection(self.conn)
        b3 = self.conn.execute("SELECT * FROM batches WHERE id=?", (bid3,)).fetchone()
        self.assertEqual(b3["status"], "ok")


if __name__ == "__main__":
    unittest.main()
