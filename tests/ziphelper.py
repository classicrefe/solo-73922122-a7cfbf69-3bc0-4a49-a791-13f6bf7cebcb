"""测试辅助：在临时目录构造 ZIP 班次包。"""

from __future__ import annotations

import io
import os
import zipfile

STATIONS = "stations.csv"
TRIPS = "trips.csv"
STOP_TIMES = "stop_times.csv"


def make_zip(entries: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            zf.writestr(name, content)
    return buffer.getvalue()


def write_zip(path: str, entries: dict[str, str | bytes]) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(make_zip(entries))
    return path


def valid_entries() -> dict[str, str]:
    return {
        STATIONS: (
            "station_id,station_name\n"
            "S01,人民广场\n"
            "S02,静安寺\n"
            "S03,徐家汇\n"
        ),
        TRIPS: (
            "trip_id\n"
            "T100\n"
            "T200\n"
        ),
        STOP_TIMES: (
            "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
            "T100,S01,1,100,110\n"
            "T100,S02,2,200,210\n"
            "T100,S03,4,300,310\n"
            "T200,S03,1,500,510\n"
            "T200,S02,2,600,610\n"
            "T200,S01,3,700,710\n"
        ),
    }
