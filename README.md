# 公交站间客流补充隐藏 API

FastAPI 服务：发布站间客流矩阵时保留真实行列合计，先隐藏 `1..k-1` 人的小客流单元，再以**最少格数**补充隐藏其他单元，使每个先隐藏单元在符合公开值及行列合计的所有非负整数矩阵中至少有两个可行值。

发布矩阵用 `null` 表示隐藏格；响应同时给出每个先隐藏单元的最小、最大可行值，供内部核对。

## 快速启动（Docker Compose）

```bash
docker compose up --build -d
```

- Swagger UI：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>
- 可反推示例：<http://localhost:8000/example>

停止：

```bash
docker compose down
```

如需更改宿主机端口：

```bash
HOST_PORT=18080 docker compose up --build -d
```

镜像基于 `python:3.11-slim`，以非 root 用户运行，并带容器健康检查。

## 请求

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

uvicorn app.main:app --reload
python -m unittest discover -s tests -v
```

也可以直接启动容器后访问 `/docs` 的 “Try it out”。

## 目录

```text
app/
  main.py       FastAPI、请求/响应模型、文档示例
  solver.py     初次隐藏、最少补充隐藏、整数可行性区间
tests/
  test_solver.py
  test_api.py
examples/
  reverse_inferable_*.json
  no_solution_*.json
Dockerfile
docker-compose.yml
requirements.txt
```
