"""归一化引擎：按来源规则把原始商品条目转换为规范结构。

规则结构（每个来源一份，按版本管理）：
{
  "decimal": {"thousands": ",", "decimal": "."},   # 数字格式
  "name": {"preferred_languages": ["zh", "en"]},   # 显示名语言优先级
  "units": {"克": {"canonical": "g", "factor": 1}, ...},
  "currencies": {"¥": "CNY", ...}
}

单位或货币无法判断时返回 quarantined 状态，由调用方写入隔离区。
"""
from decimal import Decimal, InvalidOperation

NORM_FIELDS = ["name", "qty", "unit", "price", "currency"]


def parse_number(value, fmt):
    """按来源的小数格式解析数字。fmt = {"thousands": ",", "decimal": "."}"""
    if isinstance(value, bool):
        raise InvalidOperation(value)
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    s = str(value).strip().replace(" ", " ").replace(" ", "")
    if not s:
        raise InvalidOperation("empty")
    thousands = (fmt or {}).get("thousands", ",")
    decimal = (fmt or {}).get("decimal", ".")
    if thousands:
        s = s.replace(thousands, "")
    if decimal != ".":
        s = s.replace(decimal, ".")
    return Decimal(s)


def _to_float(d):
    return round(float(d), 6)


def normalize(rules, item):
    """返回 (norm_dict | None, status, reason)。

    status: 'normalized' | 'quarantined'
    reason: 隔离原因，如 'unknown_unit:袋' / 'unknown_currency:£' / 'bad_number:...'
    """
    if not rules:
        return None, "quarantined", "no_rules"

    # 多语言名称：按偏好顺序取显示名，全部缺失则无法归一化
    names = item.get("names") or {}
    pref = (rules.get("name") or {}).get("preferred_languages", ["zh", "en"])
    display = next((names[lang] for lang in pref if names.get(lang)), None)
    if display is None and names:
        display = next(iter(names.values()))
    if not display:
        return None, "quarantined", "missing_name"

    # 单位
    raw_unit = str(item.get("unit") or "").strip()
    unit_rule = (rules.get("units") or {}).get(raw_unit)
    if unit_rule is None:
        return None, "quarantined", f"unknown_unit:{raw_unit}"

    # 货币
    raw_cur = str(item.get("currency") or "").strip()
    currency = (rules.get("currencies") or {}).get(raw_cur)
    if currency is None:
        return None, "quarantined", f"unknown_currency:{raw_cur}"

    # 数字（数量 / 价格），按来源小数格式解析
    try:
        qty = parse_number(item.get("qty", 0), rules.get("decimal"))
        price = parse_number(item.get("price", 0), rules.get("decimal"))
    except (InvalidOperation, ValueError):
        return None, "quarantined", f"bad_number:{item.get('price')}"

    factor = Decimal(str(unit_rule.get("factor", 1)))
    norm = {
        "name": display,
        "names": names,
        "qty": _to_float(qty * factor),
        "unit": unit_rule["canonical"],
        "price": _to_float(price),
        "currency": currency,
    }
    return norm, "normalized", None


def norm_diff(old_json, new_json):
    """比较两个归一化结果（JSON 字符串），返回 [(field, old, new), ...]。"""
    import json

    old = json.loads(old_json) if old_json else {}
    new = json.loads(new_json) if new_json else {}
    return [(f, old.get(f), new.get(f)) for f in NORM_FIELDS if old.get(f) != new.get(f)]
