"""Web 页面：原文、归一化差异、规则版本、隔离区失败样本、采集批次。"""
import json

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

from .collector import run_collection
from .db import connect
from .normalize import norm_diff
from .rules import activate_version, create_version


def create_app(db_path):
    app = Flask(__name__)
    app.secret_key = "catalog-dev"

    def get_db():
        if "db" not in g:
            g.db = connect(db_path)
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    # ---------- 总览 ----------
    @app.get("/")
    def dashboard():
        conn = get_db()
        srcs = []
        for s in conn.execute("SELECT * FROM sources ORDER BY id"):
            cnt = conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(status='quarantined'), 0) AS q"
                " FROM records WHERE source_id=?",
                (s["id"],),
            ).fetchone()
            active = conn.execute(
                "SELECT version FROM rule_sets WHERE source_id=? AND status='active'", (s["id"],)
            ).fetchone()
            srcs.append({"s": s, "total": cnt["c"], "quarantined": cnt["q"],
                         "rule_version": active["version"] if active else None})
        recent = conn.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 5").fetchall()
        return render_template("index.html", srcs=srcs, recent=recent)

    @app.post("/collect")
    def collect():
        conn = get_db()
        bid = run_collection(conn)
        status = conn.execute("SELECT status FROM batches WHERE id=?", (bid,)).fetchone()["status"]
        flash(f"采集批次 #{bid} 完成，状态：{status}")
        return redirect(url_for("batches"))

    @app.post("/sources/<int:sid>/toggle")
    def toggle_source(sid):
        conn = get_db()
        conn.execute("UPDATE sources SET available = 1 - available WHERE id=?", (sid,))
        conn.commit()
        flash("已切换来源可用状态（用于模拟部分来源不可用）")
        return redirect(url_for("dashboard"))

    # ---------- 记录：原文 + 归一化 ----------
    @app.get("/records")
    def records():
        conn = get_db()
        src = request.args.get("source", "")
        status = request.args.get("status", "")
        sql = ("SELECT r.*, s.code AS source_code FROM records r"
               " JOIN sources s ON s.id = r.source_id")
        conds, args = [], []
        if src:
            conds.append("s.code=?")
            args.append(src)
        if status:
            conds.append("r.status=?")
            args.append(status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY r.id"
        rows = [dict(r) for r in conn.execute(sql, args)]
        for r in rows:
            r["raw"] = json.loads(r["raw_json"])
            r["norm"] = json.loads(r["norm_json"]) if r["norm_json"] else None
        sources = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
        return render_template("records.html", rows=rows, sources=sources, src=src, status=status)

    @app.get("/records/<int:rid>")
    def record_detail(rid):
        conn = get_db()
        r = conn.execute(
            "SELECT r.*, s.code AS source_code FROM records r JOIN sources s ON s.id=r.source_id"
            " WHERE r.id=?", (rid,),
        ).fetchone()
        if not r:
            abort(404)
        hist = [dict(h) for h in conn.execute(
            "SELECT * FROM normalization_history WHERE record_id=? ORDER BY id", (rid,))]
        for i, h in enumerate(hist):
            prev = hist[i - 1]["norm_json"] if i else None
            h["diff"] = norm_diff(prev, h["norm_json"])
            h["norm"] = json.loads(h["norm_json"]) if h["norm_json"] else None
        hist.reverse()
        raw = json.loads(r["raw_json"])
        norm = json.loads(r["norm_json"]) if r["norm_json"] else None
        return render_template(
            "record.html", r=r, raw=raw,
            raw_pretty=json.dumps(raw, ensure_ascii=False, indent=2),
            norm=norm, hist=hist)

    # ---------- 规则版本 ----------
    @app.get("/rules")
    def rules():
        conn = get_db()
        srcs = []
        for s in conn.execute("SELECT * FROM sources ORDER BY id"):
            versions = conn.execute(
                "SELECT * FROM rule_sets WHERE source_id=? ORDER BY version DESC", (s["id"],)
            ).fetchall()
            active = next((v for v in versions if v["status"] == "active"), None)
            pretty = json.dumps(json.loads(active["rules_json"]), ensure_ascii=False, indent=2) if active else ""
            srcs.append({"source": s, "versions": versions, "active": active, "active_pretty": pretty})
        return render_template("rules.html", srcs=srcs)

    @app.post("/rules/<int:sid>/new")
    def rule_new(sid):
        conn = get_db()
        try:
            new_rules = json.loads(request.form["rules_json"])
        except json.JSONDecodeError as e:
            flash(f"规则 JSON 解析失败：{e}")
            return redirect(url_for("rules"))
        note = request.form.get("note", "")
        v, stats = create_version(conn, sid, new_rules, note=note, activate=True)
        d = stats["diff"]
        flash(f"来源 #{sid} 规则 v{v} 已创建并激活：受影响 {stats['planned']} 条，"
              f"变更 {stats['changed']} 条（恢复 {stats['recovered']}，新隔离 {stats['quarantined']}）；"
              f"差异键 单位={d['unit_keys']} 货币={d['currency_keys']}"
              f"{' 小数格式已变' if d['decimal_changed'] else ''}")
        return redirect(url_for("rules"))

    @app.post("/rules/<int:sid>/activate/<int:version>")
    def rule_activate(sid, version):
        conn = get_db()
        stats = activate_version(conn, sid, version, trigger="rollback")
        if stats.get("noop"):
            flash(f"v{version} 已是当前激活版本")
        else:
            flash(f"已从 v{stats['from_version']} 回退/切换到 v{version}："
                  f"受影响 {stats['planned']} 条，变更 {stats['changed']} 条")
        return redirect(url_for("rules"))

    # ---------- 隔离区（失败样本） ----------
    @app.get("/quarantine")
    def quarantine():
        conn = get_db()
        rows = [dict(r) for r in conn.execute(
            "SELECT r.*, s.code AS source_code FROM records r JOIN sources s ON s.id=r.source_id"
            " WHERE r.status='quarantined' ORDER BY r.id")]
        for r in rows:
            r["raw"] = json.loads(r["raw_json"])
        return render_template("quarantine.html", rows=rows)

    # ---------- 批次 ----------
    @app.get("/batches")
    def batches():
        conn = get_db()
        bs = []
        for b in conn.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 20"):
            parts = conn.execute(
                "SELECT bs.*, s.code FROM batch_sources bs JOIN sources s ON s.id=bs.source_id"
                " WHERE bs.batch_id=? ORDER BY s.id", (b["id"],)).fetchall()
            bs.append({"batch": b, "parts": parts})
        return render_template("batches.html", bs=bs)

    return app
