"""FastAPI service for complementary cell suppression.

Run locally:
    uvicorn app.main:app --host 0.0.0.0 --port 8000

Interactive API documentation: http://localhost:8000/docs
"""

from __future__ import annotations

import json
from typing import Annotated, List, Literal, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

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

app = FastAPI(
    title="公交站间客流补充隐藏 API",
    summary="保留行列合计的小客流单元补充隐藏服务",
    description=(
        "接收带站名的 2～3 行、2～4 列客流矩阵和阈值 k。先隐藏值为 1～k−1 "
        "的单元，再以最少格数补充隐藏其他单元，使每个先隐藏单元在所有符合"
        "公开值和行列合计的非负整数矩阵中至少有两个可行值。发布矩阵以 null "
        "表示隐藏格；返回结果同时列出每个先隐藏单元的最小/最大可行值，供内部核对。\n\n"
        "**示例说明：** " + EXAMPLE_EXPLANATION
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    openapi_tags=[
        {"name": "suppression", "description": "补充隐藏求解"},
        {"name": "examples", "description": "可调用示例"},
        {"name": "system", "description": "服务状态"},
    ],
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["system"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


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
