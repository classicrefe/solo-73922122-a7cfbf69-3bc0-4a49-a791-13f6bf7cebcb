"""整版班次包的校验、暂存与原子发布。

设计要点
========

* 上传的 ZIP 先落盘为临时文件，再以流式方式读取；CSV 按物理行迭代，
  单行超过 ``MAX_LINE_BYTES`` 立即报错，Python 侧只保留固定大小的批量
  缓冲，内存占用不随行数增长。
* 全部数据先写入 *临时 SQLite*（位于数据目录，便于同目录原子改名），
  主键/唯一约束、外键约束与窗口函数完成跨表校验。
* 只有「ZIP 校验和 + 包结构 + 全部行内/跨表规则」均通过后，才在
  ``flock`` 排他锁内做版本比较并 ``os.replace`` 整版替换已发布库；
  任何失败或进程中断都只丢弃临时库，已发布版本保持可读。
* 查询以单连接只读事务读取快照，发布是原子改名，因此一次查询绝不会
  混版；导入期间旧版照常服务。
"""

from __future__ import annotations

import csv
import fcntl
import glob
import hashlib
import io
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 常量与路径
# ---------------------------------------------------------------------------

DATA_DIR = os.environ.get("SCHEDULE_DATA_DIR", os.path.abspath("data"))
DB_FILENAME = "schedule.sqlite3"
LOCK_FILENAME = ".publish.lock"
TMP_PREFIX = "schedule.tmp."
UPLOAD_PREFIX = "schedule.upload."

STATIONS_FILE = "stations.csv"
TRIPS_FILE = "trips.csv"
STOP_TIMES_FILE = "stop_times.csv"
REQUIRED_FILES = frozenset({STATIONS_FILE, TRIPS_FILE, STOP_TIMES_FILE})

STATIONS_HEADER = ("station_id", "station_name")
TRIPS_HEADER = ("trip_id",)
STOP_TIMES_HEADER = (
    "trip_id",
    "station_id",
    "stop_sequence",
    "arrival_sec",
    "departure_sec",
)

MAX_STOP_ROWS = 500_000
MAX_TABLE_ROWS = 500_000
MAX_LINE_BYTES = 1024  # 每行最多 1 KiB
MAX_SECOND = 172_799
INSERT_CHUNK = 1_000


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class PackageError(Exception):
    """班次包校验失败；``file``/``line`` 指向坏行（文件级错误行号为空）。"""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        file: str | None = None,
        line: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.file = file
        self.line = line

    def detail(self) -> dict:
        body: dict = {
            "status": "invalid_package",
            "code": self.code,
            "message": self.message,
        }
        if self.file is not None:
            body["file"] = self.file
        if self.line is not None:
            body["line"] = self.line
        return body


class StaleVersionError(PackageError):
    """待导入版本不高于当前已发布版本。"""

    def __init__(self, current: int, candidate: int) -> None:
        super().__init__(
            f"当前已发布版本为 {current}，版本 {candidate} 不高于当前版本，拒绝导入。",
            code="version_not_newer",
        )
        self.current_version = current
        self.candidate_version = candidate

    def detail(self) -> dict:
        body = super().detail()
        body["status"] = "version_conflict"
        body["current_version"] = self.current_version
        body["candidate_version"] = self.candidate_version
        return body


class NoPublishedDataError(Exception):
    """尚未成功发布过任何版本。"""


class TripNotFoundError(Exception):
    def __init__(self, trip_id: str) -> None:
        message = f"车次不存在：{trip_id}"
        super().__init__(message)
        self.message = message
        self.trip_id = trip_id


@dataclass(frozen=True)
class ImportOutcome:
    version: int
    previous_version: int | None
    station_count: int
    trip_count: int
    stop_row_count: int
    package_sha256: str


@dataclass(frozen=True)
class StopRow:
    stop_sequence: int
    station_id: str
    station_name: str
    arrival_sec: int
    departure_sec: int


@dataclass(frozen=True)
class TripSchedule:
    version: int
    trip_id: str
    stops: tuple[StopRow, ...]


# ---------------------------------------------------------------------------
# 路径与启动维护
# ---------------------------------------------------------------------------


def db_path() -> str:
    return os.path.join(DATA_DIR, DB_FILENAME)


def _lock_path() -> str:
    return os.path.join(DATA_DIR, LOCK_FILENAME)


def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def cleanup_staging_files() -> None:
    """删除上次进程中断遗留的临时上传/临时库文件。"""
    ensure_data_dir()
    for pattern in (f"{TMP_PREFIX}*", f"{UPLOAD_PREFIX}*"):
        for path in glob.glob(os.path.join(DATA_DIR, pattern)):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# 严格整数解析
# ---------------------------------------------------------------------------


def _parse_int(
    raw: str,
    *,
    field: str,
    file_name: str,
    line: int,
    positive: bool,
    max_value: int | None = None,
) -> int:
    text = raw.strip()
    if not text or not text.isascii() or not text.isdigit():
        raise PackageError(
            f"字段 {field} 必须是整数，实际值为 {raw!r}。",
            code="bad_row",
            file=file_name,
            line=line,
        )
    value = int(text)
    if positive and value < 1:
        raise PackageError(
            f"字段 {field} 必须是正整数，实际值为 {value}。",
            code="bad_row",
            file=file_name,
            line=line,
        )
    if max_value is not None and value > max_value:
        raise PackageError(
            f"字段 {field} 超出允许范围（最大 {max_value}），实际值为 {value}。",
            code="bad_row",
            file=file_name,
            line=line,
        )
    return value


# ---------------------------------------------------------------------------
# 带 1 KiB 物理行上限的 CSV 读取
# ---------------------------------------------------------------------------


def _capped_text_lines(binary: io.BufferedIOBase, file_name: str):
    """逐物理行产出 UTF-8 文本，并限制每个物理行不超过 1 KiB。

    ``csv.reader`` 接受任意文本行迭代器，自己负责拼接带引号的跨行字段，
    因此这里按物理行切分即可；行号按物理行累计，错误可定位到起始行。
    """

    first_line = True
    while True:
        raw = binary.readline(MAX_LINE_BYTES + 1)
        if not raw:
            return
        if len(raw) > MAX_LINE_BYTES:
            raise PackageError(
                f"物理行长度超过 {MAX_LINE_BYTES} 字节（1 KiB）上限。",
                code="line_too_long",
                file=file_name,
            )
        if first_line:
            if raw.startswith(b"\xef\xbb\xbf"):  # UTF-8 BOM
                raw = raw[3:]
            first_line = False
        try:
            yield raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackageError(
                f"文件不是合法 UTF-8 编码：{exc.reason}。",
                code="encoding_error",
                file=file_name,
            ) from None


def _open_csv(zip_file: zipfile.ZipFile, info: zipfile.ZipInfo, file_name: str):
    try:
        binary = zip_file.open(info, "r")
    except RuntimeError as exc:  # 加密条目等
        raise PackageError(
            f"无法读取 {file_name}：{exc}",
            code="zip_entry_error",
            file=file_name,
        ) from None
    lines = _capped_text_lines(binary, file_name)
    return csv.reader(lines), binary


def _read_header(reader: csv.reader, expected: tuple[str, ...], file_name: str) -> None:
    try:
        header = next(reader)
    except StopIteration:
        raise PackageError(
            "文件为空，缺少表头。",
            code="missing_header",
            file=file_name,
        ) from None
    except UnicodeDecodeError as exc:
        raise PackageError(
            f"文件不是合法 UTF-8 编码：{exc.reason}。",
            code="encoding_error",
            file=file_name,
        ) from None
    if tuple(cell.strip() for cell in header) != expected:
        raise PackageError(
            f"表头应为 {','.join(expected)}，实际为 {','.join(header)!r}。",
            code="bad_header",
            file=file_name,
            line=1,
        )


def _next_record(reader: csv.reader, file_name: str) -> tuple[list[str], int]:
    start_line = reader.line_num + 1
    try:
        record = next(reader)
    except StopIteration:
        raise
    except UnicodeDecodeError as exc:
        raise PackageError(
            f"文件不是合法 UTF-8 编码：{exc.reason}。",
            code="encoding_error",
            file=file_name,
            line=start_line,
        ) from None
    except csv.Error as exc:
        raise PackageError(
            f"CSV 解析失败：{exc}",
            code="bad_row",
            file=file_name,
            line=start_line,
        ) from None
    return record, start_line


# ---------------------------------------------------------------------------
# 临时库
# ---------------------------------------------------------------------------


def _new_temp_db() -> tuple[sqlite3.Connection, str]:
    ensure_data_dir()
    fd, path = tempfile.mkstemp(prefix=TMP_PREFIX, suffix=".sqlite3", dir=DATA_DIR)
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    # 显式限制缓存、排序落盘，保证内存不随行数膨胀。
    conn.execute("PRAGMA cache_size = -2000")  # 约 2 MiB
    conn.execute("PRAGMA temp_store = FILE")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute(
        """
        CREATE TABLE stations_staging (
            station_id TEXT PRIMARY KEY,
            station_name TEXT NOT NULL,
            source_line INTEGER NOT NULL
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE trips_staging (
            trip_id TEXT PRIMARY KEY,
            source_line INTEGER NOT NULL
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE stop_times_staging (
            trip_id TEXT NOT NULL,
            station_id TEXT NOT NULL,
            stop_sequence INTEGER NOT NULL CHECK (stop_sequence > 0),
            arrival_sec INTEGER NOT NULL
                CHECK (arrival_sec BETWEEN 0 AND 172799),
            departure_sec INTEGER NOT NULL
                CHECK (departure_sec BETWEEN 0 AND 172799),
            source_line INTEGER NOT NULL,
            PRIMARY KEY (trip_id, stop_sequence)
        ) WITHOUT ROWID
        """
    )
    return conn, path


def _insert_chunk(
    conn: sqlite3.Connection,
    sql: str,
    chunk: list[tuple],
    file_name: str,
) -> None:
    """批量写入；失败则逐行重放，把约束错误定位到具体行号。"""

    conn.execute("SAVEPOINT insert_chunk")
    try:
        conn.executemany(sql, chunk)
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO insert_chunk")
        for row in chunk:
            try:
                conn.execute(sql, row)
            except sqlite3.IntegrityError as exc:
                line = row[-1]
                message = str(exc)
                if "UNIQUE" in message.upper() or "PRIMARY KEY" in message.upper():
                    code = "duplicate_id"
                    text = "唯一约束冲突（ID 或同车停站序号重复）。"
                elif "FOREIGN KEY" in message.upper():
                    code = "broken_reference"
                    text = "外键引用的站点或车次不存在。"
                else:
                    code = "bad_row"
                    text = f"数据约束失败：{message}。"
                raise PackageError(
                    text, code=code, file=file_name, line=line
                ) from None
            except sqlite3.Error as exc:
                raise PackageError(
                    f"数据约束失败：{exc}",
                    code="bad_row",
                    file=file_name,
                    line=row[-1],
                ) from None
    conn.execute("RELEASE insert_chunk")


# ---------------------------------------------------------------------------
# 三张表的流式解析
# ---------------------------------------------------------------------------


def _load_stations(
    conn: sqlite3.Connection,
    zf: zipfile.ZipFile,
    infos: dict[str, zipfile.ZipInfo],
) -> int:
    file_name = STATIONS_FILE
    reader, binary = _open_csv(zf, infos[file_name], file_name)
    count = 0
    chunk: list[tuple] = []
    try:
        _read_header(reader, STATIONS_HEADER, file_name)
        while True:
            try:
                record, line = _next_record(reader, file_name)
            except StopIteration:
                break
            if not record:
                raise PackageError(
                    "不允许空行。", code="bad_row", file=file_name, line=line
                )
            if len(record) != len(STATIONS_HEADER):
                raise PackageError(
                    f"站点表应有 {len(STATIONS_HEADER)} 列，实际 {len(record)} 列。",
                    code="bad_row",
                    file=file_name,
                    line=line,
                )
            station_id = record[0].strip()
            station_name = record[1].strip()
            if not station_id or not station_name:
                raise PackageError(
                    "站点 ID 与站名均不能为空。",
                    code="bad_row",
                    file=file_name,
                    line=line,
                )
            chunk.append((station_id, station_name, line))
            count += 1
            if count > MAX_TABLE_ROWS:
                raise PackageError(
                    f"站点表行数超过 {MAX_TABLE_ROWS} 上限。",
                    code="too_many_rows",
                    file=file_name,
                )
            if len(chunk) >= INSERT_CHUNK:
                _insert_chunk(
                    conn,
                    "INSERT INTO stations_staging VALUES (?, ?, ?)",
                    chunk,
                    file_name,
                )
                chunk.clear()
        if count == 0:
            raise PackageError(
                "站点表没有任何数据行。", code="empty_table", file=file_name
            )
        _insert_chunk(
            conn,
            "INSERT INTO stations_staging VALUES (?, ?, ?)",
            chunk,
            file_name,
        )
    finally:
        binary.close()
    return count


def _load_trips(
    conn: sqlite3.Connection,
    zf: zipfile.ZipFile,
    infos: dict[str, zipfile.ZipInfo],
) -> int:
    file_name = TRIPS_FILE
    reader, binary = _open_csv(zf, infos[file_name], file_name)
    count = 0
    chunk: list[tuple] = []
    try:
        _read_header(reader, TRIPS_HEADER, file_name)
        while True:
            try:
                record, line = _next_record(reader, file_name)
            except StopIteration:
                break
            if not record:
                raise PackageError(
                    "不允许空行。", code="bad_row", file=file_name, line=line
                )
            if len(record) != len(TRIPS_HEADER):
                raise PackageError(
                    f"车次表应有 {len(TRIPS_HEADER)} 列，实际 {len(record)} 列。",
                    code="bad_row",
                    file=file_name,
                    line=line,
                )
            trip_id = record[0].strip()
            if not trip_id:
                raise PackageError(
                    "车次 ID 不能为空。",
                    code="bad_row",
                    file=file_name,
                    line=line,
                )
            chunk.append((trip_id, line))
            count += 1
            if count > MAX_TABLE_ROWS:
                raise PackageError(
                    f"车次表行数超过 {MAX_TABLE_ROWS} 上限。",
                    code="too_many_rows",
                    file=file_name,
                )
            if len(chunk) >= INSERT_CHUNK:
                _insert_chunk(
                    conn,
                    "INSERT INTO trips_staging VALUES (?, ?)",
                    chunk,
                    file_name,
                )
                chunk.clear()
        if count == 0:
            raise PackageError(
                "车次表没有任何数据行。", code="empty_table", file=file_name
            )
        _insert_chunk(
            conn,
            "INSERT INTO trips_staging VALUES (?, ?)",
            chunk,
            file_name,
        )
    finally:
        binary.close()
    return count


def _load_stop_times(
    conn: sqlite3.Connection,
    zf: zipfile.ZipFile,
    infos: dict[str, zipfile.ZipInfo],
) -> int:
    file_name = STOP_TIMES_FILE
    reader, binary = _open_csv(zf, infos[file_name], file_name)
    count = 0
    chunk: list[tuple] = []
    try:
        _read_header(reader, STOP_TIMES_HEADER, file_name)
        while True:
            try:
                record, line = _next_record(reader, file_name)
            except StopIteration:
                break
            if not record:
                raise PackageError(
                    "不允许空行。", code="bad_row", file=file_name, line=line
                )
            if len(record) != len(STOP_TIMES_HEADER):
                raise PackageError(
                    f"停站表应有 {len(STOP_TIMES_HEADER)} 列，实际 {len(record)} 列。",
                    code="bad_row",
                    file=file_name,
                    line=line,
                )
            trip_id = record[0].strip()
            station_id = record[1].strip()
            if not trip_id or not station_id:
                raise PackageError(
                    "车次 ID 与站点 ID 均不能为空。",
                    code="bad_row",
                    file=file_name,
                    line=line,
                )
            stop_sequence = _parse_int(
                record[2],
                field="stop_sequence",
                file_name=file_name,
                line=line,
                positive=True,
            )
            arrival_sec = _parse_int(
                record[3],
                field="arrival_sec",
                file_name=file_name,
                line=line,
                positive=False,
                max_value=MAX_SECOND,
            )
            departure_sec = _parse_int(
                record[4],
                field="departure_sec",
                file_name=file_name,
                line=line,
                positive=False,
                max_value=MAX_SECOND,
            )
            if arrival_sec > departure_sec:
                raise PackageError(
                    "到站秒必须小于等于发车秒。",
                    code="time_order_error",
                    file=file_name,
                    line=line,
                )
            chunk.append(
                (
                    trip_id,
                    station_id,
                    stop_sequence,
                    arrival_sec,
                    departure_sec,
                    line,
                )
            )
            count += 1
            if count > MAX_STOP_ROWS:
                raise PackageError(
                    f"停站表行数超过 {MAX_STOP_ROWS} 上限。",
                    code="too_many_rows",
                    file=file_name,
                )
            if len(chunk) >= INSERT_CHUNK:
                _insert_chunk(
                    conn,
                    "INSERT INTO stop_times_staging VALUES (?, ?, ?, ?, ?, ?)",
                    chunk,
                    file_name,
                )
                chunk.clear()
        _insert_chunk(
            conn,
            "INSERT INTO stop_times_staging VALUES (?, ?, ?, ?, ?, ?)",
            chunk,
            file_name,
        )
    finally:
        binary.close()
    return count


# ---------------------------------------------------------------------------
# 跨表校验（SQL 流式执行，不把全表拉进 Python 内存）
# ---------------------------------------------------------------------------


def _cross_check(conn: sqlite3.Connection) -> None:
    file_name = STOP_TIMES_FILE

    row = conn.execute(
        """
        SELECT st.source_line, st.trip_id
        FROM stop_times_staging st
        WHERE NOT EXISTS (
            SELECT 1 FROM trips_staging t WHERE t.trip_id = st.trip_id
        )
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        raise PackageError(
            f"停站记录引用了车次表中不存在的车次 {row[1]!r}。",
            code="broken_reference",
            file=file_name,
            line=row[0],
        )

    row = conn.execute(
        """
        SELECT st.source_line, st.station_id
        FROM stop_times_staging st
        WHERE NOT EXISTS (
            SELECT 1 FROM stations_staging s WHERE s.station_id = st.station_id
        )
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        raise PackageError(
            f"停站记录引用了站点表中不存在的站点 {row[1]!r}。",
            code="broken_reference",
            file=file_name,
            line=row[0],
        )

    # 同车按停站序号排序后，上一站发车秒不得晚于本站到站秒。
    row = conn.execute(
        """
        SELECT source_line, trip_id, prev_departure, arrival_sec
        FROM (
            SELECT
                source_line,
                trip_id,
                arrival_sec,
                departure_sec,
                LAG(departure_sec) OVER (
                    PARTITION BY trip_id ORDER BY stop_sequence
                ) AS prev_departure
            FROM stop_times_staging
        )
        WHERE prev_departure IS NOT NULL AND prev_departure > arrival_sec
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        source_line, trip_id, prev_departure, arrival_sec = row
        raise PackageError(
            f"车次 {trip_id!r} 上一站发车秒 {prev_departure} 晚于本站到站秒 "
            f"{arrival_sec}，不满足按序 发车 <= 下站到站。",
            code="time_order_error",
            file=file_name,
            line=source_line,
        )


def _build_published_schema(
    conn: sqlite3.Connection, version: int, package_sha256: str
) -> None:
    conn.executescript(
        """
        CREATE TABLE stations (
            station_id TEXT PRIMARY KEY,
            station_name TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE trips (
            trip_id TEXT PRIMARY KEY
        ) WITHOUT ROWID;

        CREATE TABLE stop_times (
            trip_id TEXT NOT NULL REFERENCES trips(trip_id),
            station_id TEXT NOT NULL REFERENCES stations(station_id),
            stop_sequence INTEGER NOT NULL CHECK (stop_sequence > 0),
            arrival_sec INTEGER NOT NULL
                CHECK (arrival_sec BETWEEN 0 AND 172799),
            departure_sec INTEGER NOT NULL
                CHECK (departure_sec BETWEEN 0 AND 172799),
            PRIMARY KEY (trip_id, stop_sequence)
        ) WITHOUT ROWID;

        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )
    conn.execute(
        "INSERT INTO stations SELECT station_id, station_name FROM stations_staging"
    )
    conn.execute("INSERT INTO trips SELECT trip_id FROM trips_staging")
    conn.execute(
        """
        INSERT INTO stop_times
        SELECT trip_id, station_id, stop_sequence, arrival_sec, departure_sec
        FROM stop_times_staging
        """
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('version', ?)", (str(version),)
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('package_sha256', ?)",
        (package_sha256,),
    )
    # 外键开启时先删引用方（子表），再删被引用方（父表）。
    conn.execute("DROP TABLE stop_times_staging")
    conn.execute("DROP TABLE stations_staging")
    conn.execute("DROP TABLE trips_staging")


# ---------------------------------------------------------------------------
# ZIP 结构与校验和
# ---------------------------------------------------------------------------


def _validate_zip_layout(
    zf: zipfile.ZipFile,
) -> dict[str, zipfile.ZipInfo]:
    infos: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        name = info.filename
        if info.is_dir() or name.endswith("/"):
            raise PackageError(
                f"包内不允许目录条目：{name!r}。", code="unexpected_file"
            )
        if info.flag_bits & 0x1:
            raise PackageError(
                f"包内不支持加密条目：{name!r}。",
                code="zip_entry_error",
                file=name,
            )
        if (
            os.path.isabs(name)
            or name.startswith("/")
            or "\\" in name
            or ".." in name.split("/")
        ):
            raise PackageError(
                f"非法条目路径：{name!r}。", code="unsafe_path", file=name
            )
        if name in infos:
            raise PackageError(
                f"包内存在重复条目：{name!r}。", code="duplicate_file", file=name
            )
        infos[name] = info

    names = set(infos)
    missing = REQUIRED_FILES - names
    if missing:
        raise PackageError(
            f"缺少必需文件：{', '.join(sorted(missing))}。",
            code="missing_file",
        )
    extra = names - REQUIRED_FILES
    if extra:
        raise PackageError(
            f"存在多余文件：{', '.join(sorted(extra))}；包内只允许三张规定 CSV。",
            code="unexpected_file",
            file=sorted(extra)[0],
        )
    return {name: infos[name] for name in REQUIRED_FILES}


# ---------------------------------------------------------------------------
# 导入主流程
# ---------------------------------------------------------------------------


def import_package(zip_path: str, version: int) -> ImportOutcome:
    """校验并暂存整个班次包；通过后在锁内做版本闸门与原子替换。"""

    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PackageError("版本号必须是正整数。", code="bad_version")

    with open(zip_path, "rb") as fh:
        sha256 = hashlib.sha256()
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            sha256.update(block)
    package_sha256 = sha256.hexdigest()

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise PackageError(
            f"不是合法 ZIP 文件：{exc}", code="bad_zip"
        ) from None

    conn: sqlite3.Connection | None = None
    tmp_db_path: str | None = None
    try:
        with zf:
            # 先做纯结构校验（不读取任何条目内容），把非法路径、目录、
            # 多余/缺失文件挡在解压之前。
            infos = _validate_zip_layout(zf)
            # 结构可信后再读完整个包，核对每个条目的 CRC32 校验和与尺寸。
            bad_entry = zf.testzip()
            if bad_entry is not None:
                raise PackageError(
                    f"ZIP 校验和校验失败，损坏条目：{bad_entry}。",
                    code="zip_checksum",
                    file=bad_entry,
                )

            conn, tmp_db_path = _new_temp_db()
            station_count = _load_stations(conn, zf, infos)
            trip_count = _load_trips(conn, zf, infos)
            stop_row_count = _load_stop_times(conn, zf, infos)
            _cross_check(conn)
            _build_published_schema(conn, version, package_sha256)
            conn.commit()

        previous = _publish(tmp_db_path, version)
        tmp_db_path = None  # 已被改名发布，不能再删除
        return ImportOutcome(
            version=version,
            previous_version=previous,
            station_count=station_count,
            trip_count=trip_count,
            stop_row_count=stop_row_count,
            package_sha256=package_sha256,
        )
    finally:
        if conn is not None:
            conn.close()
        if tmp_db_path is not None:
            try:
                os.unlink(tmp_db_path)
            except FileNotFoundError:
                pass


def _publish(tmp_db_path: str, version: int) -> int | None:
    """flock 排他锁内比较版本并整版原子替换。"""

    ensure_data_dir()
    lock_fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        current = get_current_version()
        if current is not None and version <= current:
            raise StaleVersionError(current, version)
        os.replace(tmp_db_path, db_path())
        # 确保目录项变更落盘，进程崩溃后发布结果仍然存在。
        dir_fd = os.open(DATA_DIR, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return current
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def _connect_published_readonly() -> sqlite3.Connection:
    path = db_path()
    if not os.path.exists(path):
        raise NoPublishedDataError()
    uri = "file:" + path.replace("?", "%3F").replace("#", "%23") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.execute("PRAGMA query_only = ON")
    return conn


def get_current_version() -> int | None:
    """返回已发布版本；尚未发布时返回 None。"""

    if not os.path.exists(db_path()):
        return None
    try:
        conn = _connect_published_readonly()
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'version'"
        ).fetchone()
        return int(row[0]) if row is not None else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def get_trip_schedule(trip_id: str) -> TripSchedule:
    """在同一个只读事务里读取版本与停站，保证单次查询不混版。"""

    conn = _connect_published_readonly()
    try:
        # 显式开启只读事务，事务期间固定在同一快照上。
        conn.execute("BEGIN")
        try:
            version_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'version'"
            ).fetchone()
            if version_row is None:  # pragma: no cover - 发布库必带 meta
                raise NoPublishedDataError()
            version = int(version_row[0])

            exists = conn.execute(
                "SELECT 1 FROM trips WHERE trip_id = ? LIMIT 1", (trip_id,)
            ).fetchone()
            if exists is None:
                raise TripNotFoundError(trip_id)

            rows = conn.execute(
                """
                SELECT st.stop_sequence, st.station_id, s.station_name,
                       st.arrival_sec, st.departure_sec
                FROM stop_times st
                JOIN stations s ON s.station_id = st.station_id
                WHERE st.trip_id = ?
                ORDER BY st.stop_sequence
                """,
                (trip_id,),
            ).fetchall()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    stops = tuple(
        StopRow(
            stop_sequence=row[0],
            station_id=row[1],
            station_name=row[2],
            arrival_sec=row[3],
            departure_sec=row[4],
        )
        for row in rows
    )
    return TripSchedule(version=version, trip_id=trip_id, stops=stops)
