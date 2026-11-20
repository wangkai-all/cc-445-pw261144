# 商品目录采集流水线

多来源商品目录采集 + 版本化归一化 + 隔离区 + 定向重算，附 Web 查看页面。

## 功能

- **多来源采集**：来源返回多语言名称、原始单位、货币符号与各异的小数格式
  （如 `1,280.50` 与 `1.899,95`）。单来源不可用只影响自身，批次记为 `partial`。
- **版本化归一化规则**：每个来源一份规则（单位映射+换算系数、货币映射、小数格式、
  名称语言优先级），按版本管理，同一时刻仅一个 active 版本。
- **原文与结果双存**：`records.raw_json` 保留原文，`norm_json` 为归一化结果，
  `normalization_history` 记录每次归一化（触发源：collect / recalc / rollback）。
- **隔离区**：单位或货币无法判断的记录进入隔离区（`status=quarantined`，
  原因如 `unknown_unit:袋`、`unknown_currency:£`）。
- **定向重算**：规则修订/回退时，对新旧规则做 diff，只重算原始单位/货币命中
  差异键的记录（小数格式或语言偏好变化才扩大到全来源）；未受影响记录的
  `rule_version` 与历史保持不变。回退 = 重新激活旧版本，走同一重算路径。
- **重复采集幂等**：以 `(source, external_id)` 为自然键 + 内容哈希，
  重复采集只更新 `last_seen_batch`，不产生重复记录、不重写历史。

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install flask
.venv/bin/python run.py          # 初始化 + 首轮采集 + http://127.0.0.1:5000
```

## 页面

| 路径 | 内容 |
|---|---|
| `/` | 来源状态/记录数/当前规则版本，来源上下线开关，立即采集 |
| `/records` | 原文（数量/单位/价格/货币）与归一化结果对照，可按来源/状态筛选 |
| `/records/<id>` | 原文 JSON、当前归一化、逐版本历史与字段差异（旧 → 新） |
| `/rules` | 各来源规则版本列表、当前规则 JSON、创建新版本、激活/回退 |
| `/quarantine` | 隔离区失败样本：原因、原始单位/货币、原文 |
| `/batches` | 采集批次：每来源抓取/新增/更新/未变/隔离/重复计数与错误 |

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

覆盖：初始采集与隔离（多语言/小数格式/单位换算）、**重复采集幂等**、
内容变更重归一化、**规则修订只重算受影响记录**、**规则回退恢复**、
**部分来源不可用**（批次 partial、错误记录、恢复后正常）。

页面手工验证路径：总览下线 charlie → 立即采集 → 批次页见 `partial`；
规则页给 alpha 新建版本（`units` 增加 `"袋": {"canonical":"bag","factor":1}`）
→ 隔离区 A-1005 恢复；再激活 v1 回退 → A-1005 回到隔离区。

## 结构

```
catalog/
  db.py         # SQLite schema（sources/rule_sets/records/history/batches）
  normalize.py  # 归一化引擎（小数解析、单位换算、货币/语言映射）
  rules.py      # 规则版本、diff、定向重算（修订与回退共用）
  collector.py  # 采集：幂等入库、逐来源容错、批次统计
  seed.py       # 三个模拟来源 + v1 规则
  web.py        # Flask 页面
sources/        # 模拟上游数据（fixture）
tests/          # 流水线验证
```
