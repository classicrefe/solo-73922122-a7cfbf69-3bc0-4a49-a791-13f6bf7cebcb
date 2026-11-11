# 公交班次包入库与站间客流补充隐藏 API

FastAPI 服务，提供两类能力：

1. **整版班次包校验入库**（新增）：上传带版本号的 ZIP 班次包，流式校验三表后整版原子发布到 SQLite，支持按车次查询版本与按序停站。
2. **站间客流补充隐藏**（原接口，继续可用）：发布客流矩阵时保留真实行列合计，先隐藏 `1..k-1` 人的小客流单元，再以**最少格数**补充隐藏其他单元。

## 快速启动（Docker Compose）

```bash
docker compose up --build -d
```

- Swagger UI（含上传、查询入口与跨表错误样例）：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>
- 合法班次包样例：<http://localhost:8000/schedule/example/package>
- 跨表错误样例：<http://localhost:8000/schedule/example/broken-package>

停止：

```bash
docker compose down
```

数据保存在命名卷 `schedule-data`（容器内 `/data`），容器重建或重启后已发布版本仍然保留。如需更改宿主机端口：

```bash
HOST_PORT=18080 docker compose up --build -d
```

镜像基于 `python:3.11-slim`，以非 root 用户运行，并带容器健康检查。

---

# 一、班次包入库

## 1. 包格式

`POST /schedule/import` 接收 `multipart/form-data`：

| 表单字段 | 说明 |
| --- | --- |
| `version` | 正整数版本号；仅当**高于当前已发布版本**时才生效 |
| `file` | ZIP 班次包 |

ZIP 内必须且只能包含以下三个 UTF-8 CSV（文件名固定，表内数据行可乱序）：

| 文件 | 列 |
| --- | --- |
| `stations.csv` | `station_id,station_name`（站点 ID、站名） |
| `trips.csv` | `trip_id`（车次 ID） |
| `stop_times.csv` | `trip_id,station_id,stop_sequence,arrival_sec,departure_sec` |

校验规则：

- 站点 ID、车次 ID 各自唯一，均非空；
- 停站引用的车次、站点必须在对应表中存在（跨表外键校验）；
- 同一车次 `stop_sequence` 唯一且为正整数；
- 到发秒为 `0..172799` 的整数，且按序满足
  `到站 ≤ 发车 ≤ 下一站到站`；
- 停站最多 **50 万行**，每个物理行最多 **1 KiB**；
- 整包读完且 ZIP 校验和（CRC32）与全部跨表校验通过后，才整版替换数据。

坏行会在响应中指出**文件名与物理行号**，例如：

```json
{
  "detail": {
    "status": "invalid_package",
    "code": "broken_reference",
    "message": "停站记录引用了站点表中不存在的站点 'S99'。",
    "file": "stop_times.csv",
    "line": 4
  }
}
```

## 2. 示例调用

```bash
# 下载合法样例包
curl -s http://localhost:8000/schedule/example/package -o pkg.zip

# 上传版本 1
curl -s -X POST http://localhost:8000/schedule/import \
  -F version=1 \
  -F "file=@pkg.zip;type=application/zip"
```

成功响应：

```json
{
  "status": "ok",
  "version": 1,
  "previous_version": null,
  "station_count": 4,
  "trip_count": 2,
  "stop_row_count": 8,
  "package_sha256": "5ac8632c…"
}
```

按车次查询版本、按序停站及站名：

```bash
curl -s http://localhost:8000/schedule/trips/T100
```

```json
{
  "version": 1,
  "trip_id": "T100",
  "stops": [
    {"stop_sequence": 1, "station_id": "S01", "station_name": "人民广场",
     "arrival_sec": 28800, "departure_sec": 28920}
  ]
}
```

## 3. 状态码与并发语义

| 场景 | 状态码 |
| --- | --- |
| 校验通过并发布 | `200` |
| 版本不高于当前版本 | `409`（`version_conflict`，旧版不变） |
| ZIP/CSV 结构、行内或跨表校验失败 | `422`（含文件与行号） |
| 查询的车次不存在 | `404` |
| 尚未发布任何版本时查询 | `503` |

可靠性保证：

- **整版原子替换**：数据先写入临时 SQLite，全部通过后在 `flock` 排他锁内做版本比较并用 `os.replace` 原子改名；任何失败或进程中断都只丢弃临时库，已发布版本保持可读。
- **版本不倒退**：版本比较与替换在同一把跨进程排他锁内完成，两个 API 进程并发导入时低版本无法覆盖高版本。
- **导入期间旧版可查、查询不混版**：查询使用只读连接并在单个快照事务中同时读取版本与停站；发布是文件原子改名，一次查询只会看到单一版本。
- **重启保留**：已发布库位于持久化卷，启动时仅清理遗留的临时上传/临时库文件。
- **内存不随行数增长**：CSV 按物理行流式读取（单行超 1 KiB 即报错），按 1000 行批量写入临时 SQLite；Python 侧不缓存全表，跨表校验由 SQL（含窗口函数）完成。

---

# 二、客流补充隐藏（原接口）

`POST /suppress`

```json
{
  "row_stations": ["人民广场", "陆家嘴"],
  "column_stations": ["静安寺", "徐家汇", "世纪大道"],
  "matrix": [[1, 2, 3], [4, 5, 6]],
  "k": 2
}
```

| 字段 | 说明 |
| --- | --- |
| `row_stations` | 行站名，2～3 个，非空 |
| `column_stations` | 列站名，2～4 个，非空 |
| `matrix` | 2～3 行、2～4 列整数矩阵，每格 `0..10000` |
| `k` | 整数阈值，`k >= 2`；值为 `1..k-1` 的单元为先隐藏单元 |

为便于对接，请求也接受这些字段别名：

- `row_stations` / `row_names` / `rowLabels` / `rows`
- `column_stations` / `col_stations` / `column_names` / `cols`
- `matrix` / `flow_matrix` / `flows` / `data`
- `k` / `threshold`

校验失败返回 HTTP `422`。

## 示例调用

```bash
curl -s -X POST http://localhost:8000/suppress \
  -H 'Content-Type: application/json' \
  -d '{
    "row_stations": ["人民广场", "陆家嘴"],
    "column_stations": ["静安寺", "徐家汇", "世纪大道"],
    "matrix": [[1, 2, 3], [4, 5, 6]],
    "k": 2
  }'
```

该示例中 `(0,0)=1` 若只做初次隐藏，会被第一行合计 `6` 与公开值 `2,3` 反推为 `1`，也会被第一列合计 `5` 与公开值 `4` 反推为 `1`。服务以最少 3 格补充隐藏 `(0,1)、(1,0)、(1,1)`，得到：

```json
{
  "status": "ok",
  "message": "已保留行列合计；先隐藏 1 个小客流单元，并以最少 3 格补充隐藏。",
  "k": 2,
  "row_stations": ["人民广场", "陆家嘴"],
  "column_stations": ["静安寺", "徐家汇", "世纪大道"],
  "published_matrix": [[null, null, 3], [null, null, 6]],
  "row_totals": [6, 15],
  "column_totals": [5, 7, 9],
  "primary_suppressed_cells": [
    {
      "row": 0,
      "column": 0,
      "row_station": "人民广场",
      "column_station": "静安寺",
      "suppression_type": "primary",
      "min_feasible": 0,
      "max_feasible": 3,
      "feasible_value_count": 4,
      "protected": true
    }
  ],
  "secondary_suppressed_cells": [
    {"row": 0, "column": 1, "row_station": "人民广场", "column_station": "徐家汇", "suppression_type": "secondary"},
    {"row": 1, "column": 0, "row_station": "陆家嘴", "column_station": "静安寺", "suppression_type": "secondary"},
    {"row": 1, "column": 1, "row_station": "陆家嘴", "column_station": "徐家汇", "suppression_type": "secondary"}
  ],
  "primary_suppressed_count": 1,
  "secondary_suppressed_count": 3,
  "total_suppressed_count": 4
}
```

完整文件见：

- `examples/reverse_inferable_request.json`
- `examples/reverse_inferable_response.json`

## 响应字段

| 字段 | 说明 |
| --- | --- |
| `published_matrix` | 发布矩阵；隐藏格为 `null`，公开值与输入一致 |
| `row_totals` / `column_totals` | 原始真实行列合计，始终发布 |
| `primary_suppressed_cells` | 每个先隐藏单元及其 `min_feasible`、`max_feasible`、可行值个数 |
| `secondary_suppressed_cells` | 为保护先隐藏单元而补充隐藏的单元 |
| `*_count` | 初次/补充/合计隐藏格数 |

响应不返回被隐藏单元的原始值。

## 无解

若补充隐藏所有其他单元后仍有先隐藏单元只能取唯一值，例如某行合计为 `0` 导致同列小客流被结构性地固定，则返回 HTTP `422`：

```json
{
  "detail": {
    "status": "no_solution",
    "message": "无法保留这些行列合计发布：即使补充隐藏所有非初次隐藏单元，仍有先隐藏单元只能取唯一值。",
    "reason": "无法在保留公开行列合计的前提下保护所有小客流单元",
    "unprotectable_cells": [
      {"row": 0, "column": 1, "row_station": "甲站", "column_station": "丁站"}
    ]
  }
}
```

示例见 `examples/no_solution_request.json` 与 `examples/no_solution_response.json`。

## 求解规则

1. 先隐藏集合 `P = {(i,j) : 1 <= a[i][j] <= k-1}`。
2. 对非先隐藏单元的每个子集 `S`，按 `|S|` 从小到大搜索；一旦某 `S` 能保护所有 `P` 中单元，即得到最少补充隐藏数。
3. 固定某组隐藏格后，把行、列剩余合计作为供给/需求，建立二部网络流。对每个先隐藏单元，用整数最大流判定其是否可等于某值；可行值在运输多面体的一条边上构成整数区间，因此二分求最小/最大可行值。
4. 保护条件为 `max_feasible > min_feasible`，即至少两个可行值。
5. 若无任何 `S` 成功，返回无法保留合计发布的原因。

矩阵至多 `3×4 = 12` 格，搜索和网络流规模都很小。

## 本地开发与测试

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt

# 默认数据目录为工作区下的 data/，可用环境变量覆盖
SCHEDULE_DATA_DIR=./data uvicorn app.main:app --reload
python -m unittest discover -s tests -v
```

测试覆盖：行内/跨表校验与坏行定位、ZIP 校验和、1 KiB 行边界、50 万行上限、
版本闸门、线程与跨进程并发不倒退、失败保留旧版、重启持久化、查询不混版，
以及原 `/suppress` 接口回归。

也可以直接启动容器后访问 `/docs` 的 “Try it out”。

## 目录

```text
app/
  main.py               FastAPI 路由、请求/响应模型、文档示例
  solver.py             客流初次隐藏、最少补充隐藏、整数可行性区间
  schedule.py           班次包流式解析、临时 SQLite、跨表校验、原子发布与查询
  schedule_examples.py  /docs 与示例端点使用的合法/跨表错误 ZIP 样例
tests/
  test_solver.py
  test_api.py
  test_schedule.py
  test_schedule_api.py
  ziphelper.py
examples/
  reverse_inferable_*.json
  no_solution_*.json
Dockerfile
docker-compose.yml
requirements.txt
```
