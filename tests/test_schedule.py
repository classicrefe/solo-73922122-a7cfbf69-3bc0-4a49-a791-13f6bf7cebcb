"""班次包导入服务的单元测试（临时数据目录，不接触工作区数据）。"""

import os
import tempfile
import threading
import unittest
import zipfile

from app import schedule
from tests import ziphelper


class ScheduleServiceTests(unittest.TestCase):
    def setUp(self):
        self._old_data_dir = schedule.DATA_DIR
        self.tmpdir = tempfile.mkdtemp(prefix="schedule-test-")
        schedule.DATA_DIR = self.tmpdir
        schedule.cleanup_staging_files()

    def tearDown(self):
        schedule.DATA_DIR = self._old_data_dir

    def _write(self, entries, name="pkg.zip"):
        return ziphelper.write_zip(os.path.join(self.tmpdir, name), entries)

    def test_happy_path_and_query_snapshot(self):
        path = self._write(ziphelper.valid_entries())
        outcome = schedule.import_package(path, 3)
        self.assertEqual(outcome.version, 3)
        self.assertIsNone(outcome.previous_version)
        self.assertEqual(outcome.station_count, 3)
        self.assertEqual(outcome.trip_count, 2)
        self.assertEqual(outcome.stop_row_count, 6)
        self.assertEqual(len(outcome.package_sha256), 64)
        self.assertEqual(schedule.get_current_version(), 3)

        result = schedule.get_trip_schedule("T100")
        self.assertEqual(result.version, 3)
        self.assertEqual([s.stop_sequence for s in result.stops], [1, 2, 4])
        self.assertEqual(
            [s.station_name for s in result.stops],
            ["人民广场", "静安寺", "徐家汇"],
        )
        self.assertEqual(
            [s.arrival_sec for s in result.stops], [100, 200, 300]
        )

    def test_restart_keeps_published_data(self):
        path = self._write(ziphelper.valid_entries())
        schedule.import_package(path, 5)
        # 模拟重启：清理临时文件不影响已发布库。
        schedule.cleanup_staging_files()
        self.assertEqual(schedule.get_current_version(), 5)
        self.assertEqual(schedule.get_trip_schedule("T200").version, 5)
        files = set(os.listdir(self.tmpdir))
        self.assertIn(schedule.DB_FILENAME, files)
        self.assertNotIn(
            "schedule.tmp.leftover",
            files,
        )
        # 遗留临时文件会被清掉。
        open(os.path.join(self.tmpdir, "schedule.tmp.abc"), "w").close()
        schedule.cleanup_staging_files()
        self.assertNotIn("schedule.tmp.abc", os.listdir(self.tmpdir))

    def test_version_gate_only_higher_version_publishes(self):
        path = self._write(ziphelper.valid_entries())
        schedule.import_package(path, 2)
        with self.assertRaises(schedule.StaleVersionError) as ctx:
            schedule.import_package(path, 2)
        self.assertEqual(ctx.exception.current_version, 2)
        self.assertEqual(schedule.get_current_version(), 2)
        with self.assertRaises(schedule.StaleVersionError):
            schedule.import_package(path, 1)
        self.assertEqual(schedule.get_current_version(), 2)

        outcome = schedule.import_package(path, 8)
        self.assertEqual(outcome.previous_version, 2)
        self.assertEqual(schedule.get_current_version(), 8)

    def test_concurrent_imports_never_regress(self):
        path = self._write(ziphelper.valid_entries())
        start = threading.Barrier(5)
        outcomes: list[Exception | schedule.ImportOutcome] = []

        def worker(version):
            start.wait()  # 五个线程同时抢锁，模拟两个 API 进程并发导入
            try:
                outcomes.append(schedule.import_package(path, version))
            except Exception as exc:  # noqa: BLE001 - 收集线程结果
                outcomes.append(exc)

        # 版本严格递增，因此按任意拿锁顺序 10/11/12/13 都应发布成功，
        # 重复的 10 必然因版本闸门被拒；最终版本绝不能倒退。
        threads = [
            threading.Thread(target=worker, args=(v,))
            for v in (10, 10, 11, 12, 13)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        published = sorted(
            o.version
            for o in outcomes
            if isinstance(o, schedule.ImportOutcome)
        )
        rejected = [o for o in outcomes if isinstance(o, Exception)]
        # 不变量（与拿锁顺序无关）：发布版本严格递增、不重复；所有拒绝都是
        # 版本闸门错误；最终版本一定是候选中的最大值，绝不倒退。
        self.assertEqual(published, sorted(set(published)))
        self.assertTrue(published)
        self.assertEqual(
            len(published) + len(rejected), 5
        )
        self.assertTrue(
            all(isinstance(o, schedule.StaleVersionError) for o in rejected)
        )
        self.assertEqual(schedule.get_current_version(), 13)
        self.assertEqual(published[-1], 13)

    def test_two_processes_concurrent_import_never_regress(self):
        """两个独立 OS 进程同时导入：flock 跨进程生效，低版本不能覆盖高版本。"""
        import subprocess
        import sys

        pkg = self._write(ziphelper.valid_entries(), name="shared.zip")
        worker = os.path.join(self.tmpdir, "worker.py")
        with open(worker, "w", encoding="utf-8") as fh:
            fh.write(
                "import os, sys\n"
                f"sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})\n"
                "from app import schedule\n"
                f"schedule.DATA_DIR = {self.tmpdir!r}\n"
                "version = int(sys.argv[1])\n"
                "try:\n"
                "    schedule.import_package(sys.argv[2], version)\n"
                "    print('PUBLISHED', version)\n"
                "except schedule.StaleVersionError:\n"
                "    print('REJECTED', version)\n"
            )
        env = dict(os.environ)

        def run_pair(versions: tuple[int, int]):
            procs = [
                subprocess.Popen(
                    [sys.executable, worker, str(version), pkg],
                    stdout=subprocess.PIPE,
                    env=env,
                )
                for version in versions
            ]
            lines = []
            for proc in procs:
                lines.append(proc.stdout.read().decode("utf-8").strip())
                proc.stdout.close()
                proc.wait()
            return lines

        # 20/21 并发：高版本 21 必发布；低版本 20 可能先拿锁而发布，也可能
        # 因 21 先发布而被拒，但最终版本必须是 21，且不能两个都被拒。
        results = run_pair((20, 21))
        self.assertIn("PUBLISHED 21", results)
        self.assertEqual(schedule.get_current_version(), 21)
        self.assertTrue(any(line.startswith("PUBLISHED") for line in results))
        for line in results:
            self.assertTrue(line.startswith(("PUBLISHED", "REJECTED")))

        # 22/23 并发：最终版本必须是 23，绝不倒退。
        results = run_pair((22, 23))
        self.assertIn("PUBLISHED 23", results)
        self.assertEqual(schedule.get_current_version(), 23)

        # 旧版本导入一定被拒，已发布数据不变。
        proc = subprocess.Popen(
            [sys.executable, worker, "1", pkg],
            stdout=subprocess.PIPE,
            env=env,
        )
        out = proc.stdout.read().decode().strip()
        proc.stdout.close()
        proc.wait()
        self.assertEqual(out, "REJECTED 1")
        self.assertEqual(schedule.get_current_version(), 23)

    def test_duplicate_station_id_reports_file_and_line(self):
        entries = ziphelper.valid_entries()
        entries[ziphelper.STATIONS] += "S02,重复站\n"
        path = self._write(entries)
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "duplicate_id")
        self.assertEqual(ctx.exception.file, ziphelper.STATIONS)
        self.assertEqual(ctx.exception.line, 5)
        self.assertIsNone(schedule.get_current_version())

    def test_duplicate_trip_id_reports_file_and_line(self):
        entries = ziphelper.valid_entries()
        entries[ziphelper.TRIPS] += "T100\n"
        path = self._write(entries)
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "duplicate_id")
        self.assertEqual(ctx.exception.file, ziphelper.TRIPS)
        self.assertEqual(ctx.exception.line, 4)

    def test_duplicate_stop_sequence_reports_line(self):
        entries = ziphelper.valid_entries()
        entries[ziphelper.STOP_TIMES] += "T100,S01,2,400,410\n"  # 序号 2 重复
        path = self._write(entries)
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "duplicate_id")
        self.assertEqual(ctx.exception.file, ziphelper.STOP_TIMES)
        self.assertEqual(ctx.exception.line, 8)

    def test_broken_station_reference_reports_line(self):
        entries = ziphelper.valid_entries()
        entries[ziphelper.STOP_TIMES] = (
            "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
            "T100,S99,1,100,110\n"
        )
        path = self._write(entries)
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "broken_reference")
        self.assertEqual(ctx.exception.file, ziphelper.STOP_TIMES)
        self.assertEqual(ctx.exception.line, 2)

    def test_broken_trip_reference_reports_line(self):
        entries = ziphelper.valid_entries()
        entries[ziphelper.STOP_TIMES] = (
            "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
            "T999,S01,1,100,110\n"
        )
        path = self._write(entries)
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "broken_reference")
        self.assertEqual(ctx.exception.file, ziphelper.STOP_TIMES)
        self.assertEqual(ctx.exception.line, 2)

    def test_arrival_after_departure_rejected(self):
        entries = ziphelper.valid_entries()
        entries[ziphelper.STOP_TIMES] = (
            "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
            "T100,S01,1,200,100\n"
        )
        path = self._write(entries)
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "time_order_error")
        self.assertEqual(ctx.exception.line, 2)

    def test_prev_departure_after_next_arrival_rejected(self):
        entries = ziphelper.valid_entries()
        entries[ziphelper.STOP_TIMES] = (
            "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
            "T100,S01,1,100,300\n"
            "T100,S02,2,200,250\n"
        )
        path = self._write(entries)
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "time_order_error")
        self.assertEqual(ctx.exception.line, 3)

    def test_unordered_rows_are_accepted_and_ordered_on_query(self):
        entries = ziphelper.valid_entries()
        # 乱序：先写序号 2 再写序号 1，且站点/车次表行也打乱。
        entries[ziphelper.STATIONS] = (
            "station_id,station_name\n"
            "S02,静安寺\n"
            "S01,人民广场\n"
        )
        entries[ziphelper.TRIPS] = "trip_id\nT100\n"
        entries[ziphelper.STOP_TIMES] = (
            "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
            "T100,S02,2,200,210\n"
            "T100,S01,1,100,110\n"
        )
        path = self._write(entries)
        schedule.import_package(path, 1)
        result = schedule.get_trip_schedule("T100")
        self.assertEqual([s.stop_sequence for s in result.stops], [1, 2])

    def test_seconds_bounds_and_non_integer(self):
        base = (
            "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
            "T100,S01,1,{a},{d}\n"
        )
        for bad in (
            base.format(a=172800, d=172800),
            base.format(a=-1, d=0),
            base.format(a="x", d=1),
            base.format(a=1.5, d=2),
            base.format(a=0, d=""),
        ):
            entries = ziphelper.valid_entries()
            entries[ziphelper.TRIPS] = "trip_id\nT100\n"
            entries[ziphelper.STOP_TIMES] = bad
            path = self._write(entries, name="bad.zip")
            with self.assertRaises(schedule.PackageError) as ctx:
                schedule.import_package(path, 1)
            self.assertEqual(ctx.exception.file, ziphelper.STOP_TIMES)
            self.assertIsNone(schedule.get_current_version())

    def test_non_positive_stop_sequence_rejected(self):
        entries = ziphelper.valid_entries()
        entries[ziphelper.STOP_TIMES] = (
            "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
            "T100,S01,0,100,110\n"
        )
        path = self._write(entries)
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "bad_row")
        self.assertEqual(ctx.exception.line, 2)

    def test_missing_file_and_extra_file(self):
        entries = ziphelper.valid_entries()
        del entries[ziphelper.TRIPS]
        path = self._write(entries)
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "missing_file")

        entries = ziphelper.valid_entries()
        entries["readme.txt"] = "hello"
        path = self._write(entries)
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "unexpected_file")

    def test_bad_header_and_wrong_column_count(self):
        entries = ziphelper.valid_entries()
        entries[ziphelper.TRIPS] = "id\nT100\n"
        path = self._write(entries)
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "bad_header")
        self.assertEqual(ctx.exception.line, 1)

        entries = ziphelper.valid_entries()
        entries[ziphelper.TRIPS] = "trip_id\nT100,extra\n"
        path = self._write(entries)
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "bad_row")
        self.assertEqual(ctx.exception.line, 2)

    def test_not_a_zip_and_corrupted_checksum(self):
        path = os.path.join(self.tmpdir, "plain.zip")
        with open(path, "wb") as fh:
            fh.write(b"not a zip at all")
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "bad_zip")

        good = ziphelper.make_zip(ziphelper.valid_entries())
        # 翻转压缩数据中的一个字节，破坏 CRC32 校验和。
        buffer = bytearray(good)
        idx = len(buffer) // 2
        buffer[idx] ^= 0xFF
        corrupted = os.path.join(self.tmpdir, "corrupt.zip")
        with open(corrupted, "wb") as fh:
            fh.write(bytes(buffer))
        try:
            with self.assertRaises(schedule.PackageError) as ctx:
                schedule.import_package(corrupted, 1)
            self.assertEqual(ctx.exception.code, "zip_checksum")
        except AssertionError:
            # 极少数情况下翻转字节仍落在文件头导致 BadZipFile，也算失败且不发布。
            with self.assertRaises(schedule.PackageError):
                schedule.import_package(corrupted, 1)
        self.assertIsNone(schedule.get_current_version())

    def test_line_over_one_kib_rejected(self):
        entries = ziphelper.valid_entries()
        long_name = "站" * 600  # 每行远超 1 KiB
        entries[ziphelper.STATIONS] = (
            "station_id,station_name\n"
            f"S99,{long_name}\n"
        )
        path = self._write(entries)
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "line_too_long")
        self.assertEqual(ctx.exception.file, ziphelper.STATIONS)

    def test_stop_row_cap_enforced(self):
        old_cap = schedule.MAX_STOP_ROWS
        schedule.MAX_STOP_ROWS = 5
        try:
            rows = [
                f"T100,S01,{i},{i * 10},{i * 10 + 5}"
                for i in range(1, 8)
            ]
            entries = ziphelper.valid_entries()
            entries[ziphelper.TRIPS] = "trip_id\nT100\n"
            entries[ziphelper.STOP_TIMES] = (
                "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
                + "\n".join(rows)
                + "\n"
            )
            path = self._write(entries)
            with self.assertRaises(schedule.PackageError) as ctx:
                schedule.import_package(path, 1)
            self.assertEqual(ctx.exception.code, "too_many_rows")
        finally:
            schedule.MAX_STOP_ROWS = old_cap

    def test_failed_import_leaves_no_temp_files_and_keeps_old_version(self):
        path = self._write(ziphelper.valid_entries())
        schedule.import_package(path, 1)
        entries = ziphelper.valid_entries()
        entries[ziphelper.STOP_TIMES] = (
            "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
            "T100,S01,1,100,90\n"
        )
        bad_path = self._write(entries, name="bad.zip")
        with self.assertRaises(schedule.PackageError):
            schedule.import_package(bad_path, 2)
        self.assertEqual(schedule.get_current_version(), 1)
        leftovers = [
            name
            for name in os.listdir(self.tmpdir)
            if name.startswith(schedule.TMP_PREFIX) or name.startswith(
                schedule.UPLOAD_PREFIX
            )
        ]
        self.assertEqual(leftovers, [])

    def test_empty_table_rejected(self):
        entries = ziphelper.valid_entries()
        entries[ziphelper.TRIPS] = "trip_id\n"
        path = self._write(entries)
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "empty_table")

    def test_empty_stop_times_is_allowed(self):
        entries = ziphelper.valid_entries()
        entries[ziphelper.STOP_TIMES] = (
            "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
        )
        path = self._write(entries)
        outcome = schedule.import_package(path, 1)
        self.assertEqual(outcome.stop_row_count, 0)
        self.assertEqual(schedule.get_trip_schedule("T100").stops, ())

    def test_station_and_trip_row_caps_enforced(self):
        old_cap = schedule.MAX_TABLE_ROWS
        schedule.MAX_TABLE_ROWS = 2
        try:
            entries = ziphelper.valid_entries()
            # 站点表 3 行数据，超过上限 2。
            path = self._write(entries)
            with self.assertRaises(schedule.PackageError) as ctx:
                schedule.import_package(path, 1)
            self.assertEqual(ctx.exception.code, "too_many_rows")
            self.assertEqual(ctx.exception.file, ziphelper.STATIONS)
        finally:
            schedule.MAX_TABLE_ROWS = old_cap

    def test_utf8_bom_accepted(self):
        entries = {
            ziphelper.STATIONS: (
                b"\xef\xbb\xbfstation_id,station_name\n"
                + "S01,人民广场\n".encode("utf-8")
            ),
            ziphelper.TRIPS: b"\xef\xbb\xbftrip_id\nT100\n",
            ziphelper.STOP_TIMES: (
                "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
                "T100,S01,1,100,110\n"
            ).encode("utf-8"),
        }
        path = self._write(entries)
        outcome = schedule.import_package(path, 1)
        self.assertEqual(outcome.station_count, 1)
        self.assertEqual(
            schedule.get_trip_schedule("T100").stops[0].station_name,
            "人民广场",
        )

    def test_non_utf8_rejected(self):
        entries = ziphelper.valid_entries()
        entries[ziphelper.STATIONS] = b"station_id,station_name\nS01,\xb2\xdc\n"
        path = self._write(entries)
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "encoding_error")

    def test_query_before_any_publish(self):
        with self.assertRaises(schedule.NoPublishedDataError):
            schedule.get_trip_schedule("T100")
        self.assertIsNone(schedule.get_current_version())

    def test_trip_without_stops_returns_empty_list(self):
        entries = ziphelper.valid_entries()
        entries[ziphelper.STOP_TIMES] = (
            "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
            "T100,S01,1,100,110\n"
        )
        path = self._write(entries)
        schedule.import_package(path, 1)
        result = schedule.get_trip_schedule("T200")
        self.assertEqual(result.stops, ())

    def test_zip_with_directory_entry_rejected(self):
        path = os.path.join(self.tmpdir, "dir.zip")
        with zipfile.ZipFile(path, "w") as dst:
            for name, content in ziphelper.valid_entries().items():
                dst.writestr(name, content)
            info = zipfile.ZipInfo("folder/")
            dst.writestr(info, b"")
        with self.assertRaises(schedule.PackageError) as ctx:
            schedule.import_package(path, 1)
        self.assertEqual(ctx.exception.code, "unexpected_file")


if __name__ == "__main__":
    unittest.main(verbosity=2)
