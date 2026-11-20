"""初始数据：三个模拟来源 + 各自的 v1 归一化规则。"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ALPHA_V1 = {
    "decimal": {"thousands": ",", "decimal": "."},
    "name": {"preferred_languages": ["zh", "en"]},
    "units": {
        "克": {"canonical": "g", "factor": 1},
        "千克": {"canonical": "kg", "factor": 1},
        "斤": {"canonical": "g", "factor": 500},
        "箱": {"canonical": "case", "factor": 1},
        "罐": {"canonical": "pcs", "factor": 1},
        "个": {"canonical": "pcs", "factor": 1},
    },
    "currencies": {"¥": "CNY", "元": "CNY", "CNY": "CNY"},
}

BRAVO_V1 = {
    "decimal": {"thousands": ".", "decimal": ","},
    "name": {"preferred_languages": ["de", "en", "fr"]},
    "units": {
        "kg": {"canonical": "kg", "factor": 1},
        "g": {"canonical": "g", "factor": 1},
        "Liter": {"canonical": "l", "factor": 1},
        "ml": {"canonical": "ml", "factor": 1},
        "Stück": {"canonical": "pcs", "factor": 1},
    },
    "currencies": {"€": "EUR", "EUR": "EUR"},
}

CHARLIE_V1 = {
    "decimal": {"thousands": ",", "decimal": "."},
    "name": {"preferred_languages": ["en"]},
    "units": {
        "pcs": {"canonical": "pcs", "factor": 1},
        "oz": {"canonical": "oz", "factor": 1},
        "lb": {"canonical": "lb", "factor": 1},
        "case": {"canonical": "case", "factor": 1},
    },
    "currencies": {"$": "USD", "USD": "USD"},
}

SOURCES = [
    ("alpha", "华东生鲜 Alpha", "sources/alpha.json", ALPHA_V1),
    ("bravo", "欧洲食品 Bravo", "sources/bravo.json", BRAVO_V1),
    ("charlie", "美洲百货 Charlie", "sources/charlie.json", CHARLIE_V1),
]


def seed(conn):
    """写入来源与 v1 规则（幂等：已有来源则跳过）。"""
    if conn.execute("SELECT COUNT(*) AS c FROM sources").fetchone()["c"]:
        return
    from .rules import create_version

    for code, name, location, rules in SOURCES:
        sid = conn.execute(
            "INSERT INTO sources(code, name, kind, location, available) VALUES (?,?, 'fixture', ?, 1)",
            (code, name, os.path.join(BASE_DIR, location)),
        ).lastrowid
        create_version(conn, sid, rules, note="初始规则 v1", activate=True)
    conn.commit()
