"""FastAPI services: complementary cell suppression and schedule package import.

Run locally:
    uvicorn app.main:app --host 0.0.0.0 --port 8000

Interactive API documentation: http://localhost:8000/docs
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import asynccontextmanager
from typing import Annotated, List, Literal, Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse, Response
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from . import schedule
from .schedule_examples import (
    IMPORT_CROSS_TABLE_ERROR_EXAMPLE,
    IMPORT_SUCCESS_EXAMPLE,
    IMPORT_TIME_ORDER_ERROR_EXAMPLE,
    IMPORT_VERSION_CONFLICT_EXAMPLE,
    TRIP_QUERY_EXAMPLE,
    build_broken_reference_package,
    build_valid_package,
)
from .solver import CellRange, NoSolutionError, SolveResult, solve

# ---------------------------------------------------------------------------
# Example data: the small flow at (0, 0) is reverse-inferable before any
# secondary suppression.  With k=2 it is the only primary cell.  Row 0 fixes
# it once the other row cells are public, and column 0 does the same.  The
# minimum safe publication therefore also hides (0,1), (1,0) and (1,1).
# ---------------------------------------------------------------------------
EXAMPLE_REQUEST = {
    "row_stations": ["人民广场", "陆家嘴"],
    "column_stations": ["静安寺", "徐家汇", "世纪大道"],
    "matrix": [[1, 2, 3], [4, 5, 6]],
    "k": 2,
}
EXAMPLE_EXPLANATION = (
    "（0,0）=1 是先隐藏单元。若只隐藏它，第一行合计 6 与其余公开值 2、3 "
    "可反推出 1；第一列合计 5 与公开值 4 也会得到 1。求解器以最少格数补充隐藏 "
    "(0,1)、(1,0)、(1,1)，使该单元在保留全部行列合计时可行值为 0～3。"
)


class SuppressionRequest(BaseModel):
    """Input matrix with station labels and the small-flow threshold k."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    row_stations: List[str] = Field(
        ...,
        min_length=2,
        max_length=3,
        validation_alias=AliasChoices(
            "row_stations", "row_names", "rowLabels", "rows"
        ),
        description="Name of each row station / origin, 2..3 entries.",
    )
    column_stations: List[str] = Field(
        ...,
        min_length=2,
        max_length=4,
        validation_alias=AliasChoices(
            "column_stations", "col_stations", "column_names", "cols"
        ),
        description="Name of each column station / destination, 2..4 entries.",
    )
    matrix: List[List[Annotated[int, Field(ge=0, le=10000)]]] = Field(
        ...,
        validation_alias=AliasChoices("matrix", "flow_matrix", "flows", "data"),
        description="Passenger-flow matrix, 2..3 rows and 2..4 columns; each value 0..10000.",
    )
    k: Annotated[int, Field(ge=2, strict=True)] = Field(
        ...,
        validation_alias=AliasChoices("k", "threshold"),
        description="Integer small-flow threshold, >= 2. Cells valued 1..k-1 are primary suppressed.",
    )

    @model_validator(mode="after")
    def validate_shape_and_labels(self) -> "SuppressionRequest":
        if len(self.matrix) != len(self.row_stations):
            raise ValueError("matrix row count must equal the number of row stations")
        if any(len(row) != len(self.column_stations) for row in self.matrix):
            raise ValueError(
                "each matrix row must have the same length as column_stations"
            )
        if any(not name.strip() for name in self.row_stations):
            raise ValueError("row station names must not be empty")
        if any(not name.strip() for name in self.column_stations):
            raise ValueError("column station names must not be empty")
        return self


class CellRef(BaseModel):
    row: int
    column: int
    row_station: str
    column_station: str


class SecondaryCell(CellRef):
    suppression_type: Literal["secondary"] = "secondary"


class PrimaryCell(CellRef):
    suppression_type: Literal["primary"] = "primary"
    min_feasible: int
    max_feasible: int
    feasible_value_count: int
    protected: bool


class SuppressionResponse(BaseModel):
    status: Literal["ok"] = "ok"
    message: str
    k: int
    row_stations: List[str]
    column_stations: List[str]
    published_matrix: List[List[Optional[int]]]
    row_totals: List[int]
    column_totals: List[int]
    primary_suppressed_cells: List[PrimaryCell]
    secondary_suppressed_cells: List[SecondaryCell]
    primary_suppressed_count: int
    secondary_suppressed_count: int
    total_suppressed_count: int


def _response(
    request: SuppressionRequest, result: SolveResult
) -> SuppressionResponse:
    m = len(request.matrix)
    n = len(request.matrix[0])
    hidden = set(result.hidden_cells)
    ranges = {(cell.row, cell.column): cell for cell in result.primary_ranges}

    published_matrix: list[list[Optional[int]]] = []
    for i in range(m):
        row: list[Optional[int]] = []
        for j in range(n):
            row.append(None if (i, j) in hidden else request.matrix[i][j])
        published_matrix.append(row)

    primary_cells: list[PrimaryCell] = []
    for i, j in result.primary_cells:
        cell_range: CellRange = ranges[(i, j)]
        primary_cells.append(
            PrimaryCell(
                row=i,
                column=j,
                row_station=request.row_stations[i],
                column_station=request.column_stations[j],
                min_feasible=cell_range.min_feasible,
                max_feasible=cell_range.max_feasible,
                feasible_value_count=cell_range.value_count,
                protected=cell_range.protected,
            )
        )

    secondary_cells = [
        SecondaryCell(
            row=i,
            column=j,
            row_station=request.row_stations[i],
            column_station=request.column_stations[j],
        )
        for i, j in result.secondary_cells
    ]

    if primary_cells:
        message = (
            f"已保留行列合计；先隐藏 {len(primary_cells)} 个小客流单元，"
            f"并以最少 {len(secondary_cells)} 格补充隐藏。"
        )
    else:
        message = "没有 1..k-1 人的小客流单元，矩阵无需隐藏，原样发布。"

    return SuppressionResponse(
        message=message,
        k=request.k,
        row_stations=list(request.row_stations),
        column_stations=list(request.column_stations),
        published_matrix=published_matrix,
        row_totals=list(result.row_totals),
        column_totals=list(result.column_totals),
        primary_suppressed_cells=primary_cells,
        secondary_suppressed_cells=secondary_cells,
        primary_suppressed_count=len(primary_cells),
        secondary_suppressed_count=len(secondary_cells),
        total_suppressed_count=len(hidden),
    )


def _example_response() -> dict:
    request = SuppressionRequest.model_validate(EXAMPLE_REQUEST)
    result = solve(request.matrix, request.k)
    return _response(request, result).model_dump(mode="json")


EXAMPLE_RESPONSE = _example_response()

NO_SOLUTION_REQUEST = {
    "row_stations": ["甲站", "乙站"],
    "column_stations": ["丙站", "丁站"],
    "matrix": [[3, 2], [0, 0]],
    "k": 3,
}
NO_SOLUTION_EXAMPLE = {
    "detail": {
        "status": "no_solution",
        "message": (
            "无法保留这些行列合计发布：即使补充隐藏所有非初次隐藏单元，"
            "仍有先隐藏单元只能取唯一值。"
        ),
        "reason": "无法在保留公开行列合计的前提下保护所有小客流单元",
        "unprotectable_cells": [
            {
                "row": 0,
                "column": 1,
                "row_station": "甲站",
                "column_station": "丁站",
            }
        ],
    }
}

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 清理上次进程中断遗留的临时上传/临时库；已发布的 SQLite 保持不动。
    schedule.cleanup_staging_files()
    yield


app = FastAPI(
    title="公交班次包入库与站间客流补充隐藏 API",
    summary="整版班次包校验入库服务（含客流补充隐藏求解）",
    description=(
        "## 班次包入库\n\n"
        "`POST /schedule/import` 接收正整数版本号与一个 ZIP 班次包（"
        "`stations.csv`、`trips.csv`、`stop_times.csv`，均为 UTF-8 CSV）。"
        "服务把整包写入临时 SQLite 完成行内与跨表校验，全部通过且 ZIP "
        "校验和无误后，才在排他锁内比较版本并整版原子替换已发布数据；"
        "失败或进程中断保留旧版，仅更高版本可生效，导入期间旧版照常查询。\n\n"
        "`GET /schedule/trips/{trip_id}` 在单个只读快照事务中返回该次车的版本、"
        "按序停站与站名，一次查询绝不混版。可先在 `/schedule/example/package` "
        "下载合法样例 ZIP，或用 `/schedule/example/broken-package` 下载跨表错误样例。\n\n"
        "## 客流补充隐藏\n\n"
        "接收带站名的 2～3 行、2～4 列客流矩阵和阈值 k。先隐藏值为 1～k−1 "
        "的单元，再以最少格数补充隐藏其他单元，使每个先隐藏单元在所有符合"
        "公开值和行列合计的非负整数矩阵中至少有两个可行值。发布矩阵以 null "
        "表示隐藏格；返回结果同时列出每个先隐藏单元的最小/最大可行值，供内部核对。\n\n"
        "**示例说明：** " + EXAMPLE_EXPLANATION
    ),
    version="1.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "suppression", "description": "补充隐藏求解"},
        {"name": "schedule", "description": "整版班次包入库与查询"},
        {"name": "examples", "description": "可调用示例"},
        {"name": "system", "description": "服务状态"},
    ],
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["system"], summary="健康检查")
def health() -> dict:
    return {"status": "ok", "schedule_version": schedule.get_current_version()}


# ---------------------------------------------------------------------------
# 班次包入库
# ---------------------------------------------------------------------------


class ImportResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: int
    previous_version: Optional[int]
    station_count: int
    trip_count: int
    stop_row_count: int
    package_sha256: str


class StopResponse(BaseModel):
    stop_sequence: int
    station_id: str
    station_name: str
    arrival_sec: int
    departure_sec: int


class TripScheduleResponse(BaseModel):
    version: int
    trip_id: str
    stops: List[StopResponse]


class ScheduleVersionResponse(BaseModel):
    version: Optional[int]


@app.get(
    "/schedule/version",
    tags=["schedule"],
    response_model=ScheduleVersionResponse,
    summary="查询当前已发布班次版本",
)
def schedule_version() -> ScheduleVersionResponse:
    return ScheduleVersionResponse(version=schedule.get_current_version())


@app.post(
    "/schedule/import",
    tags=["schedule"],
    response_model=ImportResponse,
    summary="上传整版班次包（ZIP）校验并整版入库",
    description=(
        "表单字段：`version` 为正整数；`file` 为 ZIP 班次包，包内必须且只能包含 "
        "`stations.csv`（station_id,station_name）、`trips.csv`（trip_id）、"
        "`stop_times.csv`（trip_id,station_id,stop_sequence,arrival_sec,departure_sec）"
        "三个 UTF-8 CSV 文件，文件与数据行均可乱序。\n\n"
        "校验规则：站点/车次 ID 各自唯一；停站引用的站点与车次必须存在；"
        "同车停站序号唯一且为正整数；到发秒为 0～172799 整数且到站 ≤ 发车 ≤ "
        "下一站到站；停站最多 50 万行、单行最多 1 KiB。\n\n"
        "服务读完整包并通过 ZIP 校验和与全部跨表校验后才整版替换 SQLite；"
        "仅高于当前版本才能生效，并发导入不会倒退版本。坏行会指出文件名与行号。"
    ),
    responses={
        200: {
            "description": "整版校验通过并已原子发布。",
            "content": {"application/json": {"example": IMPORT_SUCCESS_EXAMPLE}},
        },
        409: {
            "description": "版本不高于当前已发布版本，拒绝导入（旧版保持不变）。",
            "content": {
                "application/json": {
                    "examples": {
                        "version_conflict": {
                            "summary": "版本未递增",
                            "value": IMPORT_VERSION_CONFLICT_EXAMPLE,
                        }
                    }
                }
            },
        },
        422: {
            "description": "ZIP/CSV 结构、行内或跨表校验失败；detail 中含文件与行号。",
            "content": {
                "application/json": {
                    "examples": {
                        "broken_reference": {
                            "summary": "跨表错误：停站引用了不存在的站点（含文件与行号）",
                            "value": IMPORT_CROSS_TABLE_ERROR_EXAMPLE,
                        },
                        "time_order_error": {
                            "summary": "时间逆序：上一站发车晚于下一站到站",
                            "value": IMPORT_TIME_ORDER_ERROR_EXAMPLE,
                        },
                    }
                }
            },
        },
    },
)
async def import_schedule_package(
    version: Annotated[
        int,
        Form(
            ...,
            ge=1,
            description="班次包版本号，正整数；仅高于当前已发布版本时生效。",
        ),
    ],
    file: Annotated[
        UploadFile, File(..., description="整版班次 ZIP（含三张 UTF-8 CSV）。")
    ],
) -> ImportResponse:
    schedule.ensure_data_dir()
    fd, tmp_path = tempfile.mkstemp(
        prefix=schedule.UPLOAD_PREFIX, suffix=".zip", dir=schedule.DATA_DIR
    )
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        try:
            outcome = schedule.import_package(tmp_path, version)
        except schedule.StaleVersionError as exc:
            raise HTTPException(status_code=409, detail=exc.detail()) from None
        except schedule.PackageError as exc:
            raise HTTPException(status_code=422, detail=exc.detail()) from None
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass

    return ImportResponse(
        version=outcome.version,
        previous_version=outcome.previous_version,
        station_count=outcome.station_count,
        trip_count=outcome.trip_count,
        stop_row_count=outcome.stop_row_count,
        package_sha256=outcome.package_sha256,
    )


@app.get(
    "/schedule/trips/{trip_id}",
    tags=["schedule"],
    response_model=TripScheduleResponse,
    summary="按车次查询版本、按序停站及站名",
    description=(
        "在同一个只读快照事务中读取版本与全部停站，因此即使导入正在进行，"
        "一次查询也只会返回单一版本的数据，不会混版。"
    ),
    responses={
        200: {
            "description": "该车次在当前已发布版本中的完整停站序列。",
            "content": {"application/json": {"example": TRIP_QUERY_EXAMPLE}},
        },
        404: {"description": "车次在当前版本中不存在。"},
        503: {"description": "尚未成功发布任何班次版本。"},
    },
)
def get_trip(trip_id: str) -> TripScheduleResponse:
    try:
        result = schedule.get_trip_schedule(trip_id)
    except schedule.NoPublishedDataError:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "no_published_data",
                "message": "尚未成功发布任何班次版本，请先调用 /schedule/import。",
            },
        ) from None
    except schedule.TripNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "status": "trip_not_found",
                "message": exc.message,
                "trip_id": exc.trip_id,
            },
        ) from None

    return TripScheduleResponse(
        version=result.version,
        trip_id=result.trip_id,
        stops=[
            StopResponse(
                stop_sequence=stop.stop_sequence,
                station_id=stop.station_id,
                station_name=stop.station_name,
                arrival_sec=stop.arrival_sec,
                departure_sec=stop.departure_sec,
            )
            for stop in result.stops
        ],
    )


@app.get(
    "/schedule/example/package",
    tags=["examples"],
    summary="下载合法班次包样例 ZIP（可直接上传 /schedule/import）",
)
def valid_schedule_package_example() -> Response:
    return Response(
        content=build_valid_package(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="schedule-example-v1.zip"'
        },
    )


@app.get(
    "/schedule/example/broken-package",
    tags=["examples"],
    summary="下载跨表错误样例 ZIP（stop_times.csv 第 4 行引用不存在的站点 S99）",
)
def broken_schedule_package_example() -> Response:
    return Response(
        content=build_broken_reference_package(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="schedule-broken-example.zip"'
        },
    )


@app.get(
    "/example",
    tags=["examples"],
    summary="获取可反推小客流示例请求",
    description="该示例在只做初次隐藏时可通过行列合计反推出小客流，因此需要补充隐藏。",
)
def example() -> dict:
    return {
        "description": EXAMPLE_EXPLANATION,
        "request": EXAMPLE_REQUEST,
        "expected_response": EXAMPLE_RESPONSE,
        "curl": (
            "curl -s -X POST http://localhost:8000/suppress "
            "-H 'Content-Type: application/json' "
            f"-d '{json.dumps(EXAMPLE_REQUEST, ensure_ascii=False)}'"
        ),
    }


@app.post(
    "/suppress",
    tags=["suppression"],
    response_model=SuppressionResponse,
    summary="计算最小补充隐藏并发布矩阵",
    description=(
        "输入 row_stations、column_stations、matrix、k。输出用 null 表示隐藏格的"
        "发布矩阵、原始行列合计、补充隐藏单元，以及每个先隐藏单元的最小/最大可行值。"
    ),
    responses={
        200: {
            "description": "成功；若 k 范围内没有小客流单元，矩阵原样发布。",
            "content": {"application/json": {"example": EXAMPLE_RESPONSE}},
        },
        422: {
            "description": "输入校验失败，或无法在保留这些行列合计的前提下保护所有小客流单元。",
            "content": {
                "application/json": {
                    "examples": {
                        "no_solution": {
                            "summary": "无解：小客流被合计结构性固定",
                            "value": NO_SOLUTION_EXAMPLE,
                        }
                    }
                }
            },
        },
    },
)
def suppress(
    payload: SuppressionRequest = Body(
        ...,
        openapi_examples={
            "reverse_inferable_small_flow": {
                "summary": "可反推的 1 人客流（2×3）",
                "description": EXAMPLE_EXPLANATION,
                "value": EXAMPLE_REQUEST,
            },
            "no_solution": {
                "summary": "无法保留合计发布的无解输入",
                "description": (
                    "乙站整行客流为 0，丁站列合计只能由 (0,1)=2 提供，"
                    "补充隐藏任何其他格都无法让小客流有两个可行值。"
                ),
                "value": NO_SOLUTION_REQUEST,
            },
        },
    ),
) -> SuppressionResponse:
    try:
        result = solve(payload.matrix, payload.k)
    except NoSolutionError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "no_solution",
                "message": (
                    "无法保留这些行列合计发布：即使补充隐藏所有非初次隐藏单元，"
                    "仍有先隐藏单元只能取唯一值。"
                ),
                "reason": exc.message,
                "unprotectable_cells": [
                    {
                        "row": i,
                        "column": j,
                        "row_station": payload.row_stations[i],
                        "column_station": payload.column_stations[j],
                    }
                    for i, j in exc.unprotectable_cells
                ],
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return _response(payload, result)


# Friendly aliases.  They are omitted from /docs to keep the canonical
# operation and its examples unambiguous, but remain directly callable.
@app.post("/protect", include_in_schema=False, response_model=SuppressionResponse)
def protect_alias(payload: SuppressionRequest) -> SuppressionResponse:
    return suppress(payload)


@app.post(
    "/api/v1/suppress",
    include_in_schema=False,
    response_model=SuppressionResponse,
)
def suppress_v1_alias(payload: SuppressionRequest) -> SuppressionResponse:
    return suppress(payload)


@app.post(
    "/api/suppress",
    include_in_schema=False,
    response_model=SuppressionResponse,
)
def suppress_api_alias(payload: SuppressionRequest) -> SuppressionResponse:
    return suppress(payload)
