#!/usr/bin/env python3
"""入口：初始化数据库、种子数据、首轮采集，然后启动 Web 服务。"""
import os

from catalog.collector import run_collection
from catalog.db import connect, init_db
from catalog.seed import seed
from catalog.web import create_app

DB_PATH = os.environ.get("CATALOG_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.db"))


def main():
    fresh = not os.path.exists(DB_PATH)
    conn = connect(DB_PATH)
    init_db(conn)
    seed(conn)
    if fresh:
        bid = run_collection(conn)
        print(f"首轮采集完成：批次 #{bid}")
    conn.close()

    app = create_app(DB_PATH)
    port = int(os.environ.get("PORT", "5000"))
    print(f"Web 服务启动：http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
