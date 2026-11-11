"""班次入库 API 的 HTTP 层测试。"""

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app import schedule
from app.main import app
from tests import ziphelper


class ScheduleApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="schedule-api-")
        cls._old_data_dir = schedule.DATA_DIR
        schedule.DATA_DIR = cls.tmpdir
        schedule.cleanup_staging_files()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        schedule.DATA_DIR = cls._old_data_dir

    def _upload(self, version, entries=None, payload: bytes | None = None):
        data = ziphelper.make_zip(entries if entries is not None else ziphelper.valid_entries())
        if payload is not None:
            data = payload
        return self.client.post(
            "/schedule/import",
            data={"version": str(version)},
            files={"file": ("pkg.zip", data, "application/zip")},
        )

    def test_01_full_import_and_query_flow(self):
        response = self._upload(1)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["version"], 1)
        self.assertIsNone(body["previous_version"])
        self.assertEqual(body["stop_row_count"], 6)

        version = self.client.get("/schedule/version").json()
        self.assertEqual(version, {"version": 1})

        trip = self.client.get("/schedule/trips/T100")
        self.assertEqual(trip.status_code, 200)
        payload = trip.json()
        self.assertEqual(payload["version"], 1)
        self.assertEqual([s["stop_sequence"] for s in payload["stops"]], [1, 2, 4])
        self.assertEqual(payload["stops"][0]["station_name"], "人民广场")

    def test_02_docs_include_schedule_endpoints_and_error_examples(self):
        spec = self.client.get("/openapi.json").json()
        self.assertIn("/schedule/import", spec["paths"])
        self.assertIn("/schedule/trips/{trip_id}", spec["paths"])
        self.assertIn("/schedule/version", spec["paths"])
        examples = (
            spec["paths"]["/schedule/import"]["post"]["responses"]["422"]
            ["content"]["application/json"]["examples"]
        )
        self.assertIn("broken_reference", examples)
        self.assertIn("time_order_error", examples)

    def test_03_version_conflict_returns_409_and_keeps_old(self):
        response = self._upload(1)
        self.assertEqual(response.status_code, 409)
        detail = response.json()["detail"]
        self.assertEqual(detail["status"], "version_conflict")
        self.assertEqual(detail["current_version"], 1)
        self.assertEqual(self.client.get("/schedule/version").json()["version"], 1)

    def test_04_cross_table_error_locates_file_and_line(self):
        entries = ziphelper.valid_entries()
        entries[ziphelper.STOP_TIMES] = (
            "trip_id,station_id,stop_sequence,arrival_sec,departure_sec\n"
            "T100,S99,1,100,110\n"
        )
        response = self._upload(2, entries)
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "broken_reference")
        self.assertEqual(detail["file"], "stop_times.csv")
        self.assertEqual(detail["line"], 2)
        # 旧版仍可查。
        self.assertEqual(
            self.client.get("/schedule/trips/T100").json()["version"], 1
        )

    def test_05_bad_version_form_value(self):
        response = self.client.post(
            "/schedule/import",
            data={"version": "0"},
            files={"file": ("p.zip", ziphelper.make_zip(ziphelper.valid_entries()),
                            "application/zip")},
        )
        self.assertEqual(response.status_code, 422)

    def test_06_trip_not_found_404(self):
        response = self.client.get("/schedule/trips/UNKNOWN")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["status"], "trip_not_found")

    def test_07_higher_version_replaces_and_query_uses_new(self):
        entries = ziphelper.valid_entries()
        entries[ziphelper.STATIONS] += "S04,新站点\n"
        entries[ziphelper.STOP_TIMES] += "T100,S04,9,800,810\n"
        response = self._upload(9, entries)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["previous_version"], 1)
        trip = self.client.get("/schedule/trips/T100").json()
        self.assertEqual(trip["version"], 9)
        self.assertEqual(len(trip["stops"]), 4)

    def test_08_example_package_downloads_and_imports(self):
        pkg = self.client.get("/schedule/example/package")
        self.assertEqual(pkg.status_code, 200)
        self.assertEqual(pkg.headers["content-type"], "application/zip")
        response = self._upload(10, payload=pkg.content)
        self.assertEqual(response.status_code, 200, response.text)

        broken = self.client.get("/schedule/example/broken-package")
        response = self._upload(11, payload=broken.content)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"]["code"], "broken_reference"
        )

    def test_09_original_suppression_api_still_works(self):
        response = self.client.post(
            "/suppress",
            json={
                "row_stations": ["人民广场", "陆家嘴"],
                "column_stations": ["静安寺", "徐家汇", "世纪大道"],
                "matrix": [[1, 2, 3], [4, 5, 6]],
                "k": 2,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["published_matrix"],
            [[None, None, 3], [None, None, 6]],
        )
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(
            self.client.get("/health").json()["schedule_version"], 10
        )

    def test_10_upload_temp_files_are_cleaned(self):
        self._upload(12)
        leftovers = [
            name
            for name in os.listdir(self.tmpdir)
            if name.startswith(schedule.UPLOAD_PREFIX)
        ]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
