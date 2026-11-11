"""Swagger / 示例端点使用的班次包样例（内存构造 ZIP，不落盘）。"""

from __future__ import annotations

import io
import zipfile

from .schedule import (
    STATIONS_FILE,
    STOP_TIMES_FILE,
    TRIPS_FILE,
)

# ---------------------------------------------------------------------------
# 合法样例：两条车次、四个站点；序号不连续也允许，只要求按序时间单调。
# ---------------------------------------------------------------------------

VALID_STATIONS_CSV = (
    "station_id,station_name\n"
    "S01,人民广场\n"
    "S02,静安寺\n"
    "S03,徐家汇\n"
    "S04,世纪大道\n"
)

VALID_TRIPS_CSV = (
    "trip_id\n"
    "T100\n"
    "T200\n"
)

VALID_STOP_TIMES_CSV = (
    "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
    "T100,S01,1,28800,28920\n"
    "T100,S02,3,29400,29520\n"
    "T100,S03,5,30000,30060\n"
    "T100,S04,7,30600,30720\n"
    "T200,S04,1,36000,36060\n"
    "T200,S03,2,36600,36720\n"
    "T200,S02,4,37200,37320\n"
    "T200,S01,6,37800,37920\n"
)


def build_valid_package() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(STATIONS_FILE, VALID_STATIONS_CSV.encode("utf-8"))
        zf.writestr(TRIPS_FILE, VALID_TRIPS_CSV.encode("utf-8"))
        zf.writestr(STOP_TIMES_FILE, VALID_STOP_TIMES_CSV.encode("utf-8"))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 跨表错误样例：第 4 行（表头为第 1 行）引用了站点表中不存在的 S99。
# 对应 422 响应见 IMPORT_CROSS_TABLE_ERROR_EXAMPLE。
# ---------------------------------------------------------------------------

BROKEN_STATIONS_CSV = (
    "station_id,station_name\n"
    "S01,人民广场\n"
    "S02,静安寺\n"
)

BROKEN_TRIPS_CSV = (
    "trip_id\n"
    "T100\n"
)

BROKEN_STOP_TIMES_CSV = (
    "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
    "T100,S01,1,28800,28920\n"
    "T100,S01,2,29400,29520\n"
    "T100,S99,3,30000,30060\n"  # 物理第 5 行：S99 在站点表中不存在
    "T100,S02,5,30600,30720\n"
)


def build_broken_reference_package() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(STATIONS_FILE, BROKEN_STATIONS_CSV.encode("utf-8"))
        zf.writestr(TRIPS_FILE, BROKEN_TRIPS_CSV.encode("utf-8"))
        zf.writestr(STOP_TIMES_FILE, BROKEN_STOP_TIMES_CSV.encode("utf-8"))
    return buffer.getvalue()


# 时间逆序样例：上一站发车 29520 晚于下一站到站 29400。
TIME_ORDER_STOP_TIMES_CSV = (
    "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
    "T100,S01,1,28800,29520\n"  # 物理第 2 行
    "T100,S02,2,29400,29600\n"  # 物理第 3 行：29520 > 29400
)


def build_time_order_error_package() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(STATIONS_FILE, BROKEN_STATIONS_CSV.encode("utf-8"))
        zf.writestr(TRIPS_FILE, BROKEN_TRIPS_CSV.encode("utf-8"))
        zf.writestr(STOP_TIMES_FILE, TIME_ORDER_STOP_TIMES_CSV.encode("utf-8"))
    return buffer.getvalue()


IMPORT_CROSS_TABLE_ERROR_EXAMPLE = {
    "detail": {
        "status": "invalid_package",
        "code": "broken_reference",
        "message": "停站记录引用了站点表中不存在的站点 'S99'。",
        "file": STOP_TIMES_FILE,
        "line": 4,
    }
}

IMPORT_TIME_ORDER_ERROR_EXAMPLE = {
    "detail": {
        "status": "invalid_package",
        "code": "time_order_error",
        "message": "车次 'T100' 上一站发车秒 29520 晚于本站到站秒 29400，不满足按序 发车 <= 下站到站。",
        "file": STOP_TIMES_FILE,
        "line": 3,
    }
}

IMPORT_VERSION_CONFLICT_EXAMPLE = {
    "detail": {
        "status": "version_conflict",
        "code": "version_not_newer",
        "message": "当前已发布版本为 7，版本 7 不高于当前版本，拒绝导入。",
        "current_version": 7,
        "candidate_version": 7,
    }
}

IMPORT_SUCCESS_EXAMPLE = {
    "status": "ok",
    "version": 7,
    "previous_version": 6,
    "station_count": 4,
    "trip_count": 2,
    "stop_row_count": 8,
    "package_sha256": "<64 位十六进制 SHA-256>",
}

TRIP_QUERY_EXAMPLE = {
    "version": 7,
    "trip_id": "T100",
    "stops": [
        {
            "stop_sequence": 1,
            "station_id": "S01",
            "station_name": "人民广场",
            "arrival_sec": 28800,
            "departure_sec": 28920,
        },
        {
            "stop_sequence": 3,
            "station_id": "S02",
            "station_name": "静安寺",
            "arrival_sec": 29400,
            "departure_sec": 29520,
        },
        {
            "stop_sequence": 5,
            "station_id": "S03",
            "station_name": "徐家汇",
            "arrival_sec": 30000,
            "departure_sec": 30060,
        },
        {
            "stop_sequence": 7,
            "station_id": "S04",
            "station_name": "世纪大道",
            "arrival_sec": 30600,
            "departure_sec": 30720,
        },
    ],
}
