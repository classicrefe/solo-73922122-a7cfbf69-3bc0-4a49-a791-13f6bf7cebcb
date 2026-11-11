import unittest

try:
    from fastapi.testclient import TestClient

    from app.main import EXAMPLE_REQUEST, app
except ImportError:  # pragma: no cover - allows solver-only test environments
    TestClient = None
    app = None
    EXAMPLE_REQUEST = None


@unittest.skipIf(TestClient is None, "FastAPI test dependencies are not installed")
class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_health_and_docs(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/docs").status_code, 200)
        openapi = self.client.get("/openapi.json").json()
        self.assertIn("/suppress", openapi["paths"])
        self.assertIn("reverse_inferable_small_flow",
                      openapi["paths"]["/suppress"]["post"]["requestBody"]
                      ["content"]["application/json"]["examples"])

    def test_example_matches_documented_response(self):
        response = self.client.post("/suppress", json=EXAMPLE_REQUEST)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            body["published_matrix"],
            [[None, None, 3], [None, None, 6]],
        )
        self.assertEqual(body["row_totals"], [6, 15])
        self.assertEqual(body["column_totals"], [5, 7, 9])
        self.assertEqual(body["primary_suppressed_count"], 1)
        self.assertEqual(body["secondary_suppressed_count"], 3)
        primary = body["primary_suppressed_cells"][0]
        self.assertEqual((primary["min_feasible"], primary["max_feasible"]), (0, 3))
        self.assertTrue(primary["protected"])

    def test_example_endpoint_and_alias(self):
        example = self.client.get("/example")
        self.assertEqual(example.status_code, 200)
        payload = example.json()["request"]
        response = self.client.post("/api/v1/suppress", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["published_matrix"],
            [[None, None, 3], [None, None, 6]],
        )

    def test_no_solution_response(self):
        payload = {
            "row_stations": ["甲站", "乙站"],
            "column_stations": ["丙站", "丁站"],
            "matrix": [[3, 2], [0, 0]],
            "k": 3,
        }
        response = self.client.post("/suppress", json=payload)
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertEqual(detail["status"], "no_solution")
        self.assertEqual(len(detail["unprotectable_cells"]), 1)

    def test_alias_input_fields_are_accepted(self):
        payload = {
            "rows": ["A", "B"],
            "cols": ["C", "D"],
            "flows": [[1, 2], [3, 4]],
            "threshold": 2,
        }
        response = self.client.post("/protect", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["k"], 2)

    def test_invalid_dimensions_and_values(self):
        bad_shape = {
            "row_stations": ["A", "B"],
            "column_stations": ["C"],
            "matrix": [[1], [2]],
            "k": 2,
        }
        self.assertEqual(
            self.client.post("/suppress", json=bad_shape).status_code, 422
        )

        bad_value = {
            "row_stations": ["A", "B"],
            "column_stations": ["C", "D"],
            "matrix": [[1, 10001], [3, 4]],
            "k": 2,
        }
        self.assertEqual(
            self.client.post("/suppress", json=bad_value).status_code, 422
        )

        bad_k = {
            "row_stations": ["A", "B"],
            "column_stations": ["C", "D"],
            "matrix": [[1, 2], [3, 4]],
            "k": 1,
        }
        self.assertEqual(
            self.client.post("/suppress", json=bad_k).status_code, 422
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
