"""HTTP-level tests for the adjudication endpoint."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

VALID_SEQUENCE = [
    {"timestamp": 0, "ppm": 10},
    {"timestamp": 14400, "ppm": 10},
    {"timestamp": 28800, "ppm": 10},
]


class TestSuccess:
    def test_constant_sequence(self):
        response = client.post("/adjudicate", json=VALID_SEQUENCE)
        assert response.status_code == 200
        assert response.json() == {
            "area": "288000",
            "equivalent": "10.000",
            "verdict": "PASS",
        }

    def test_uneven_spacing_weights_long_segments(self):
        response = client.post(
            "/adjudicate",
            json=[
                {"timestamp": 0, "ppm": 0},
                {"timestamp": 100, "ppm": 0},
                {"timestamp": 200, "ppm": 100},
                {"timestamp": 28800, "ppm": 0},
            ],
        )
        assert response.status_code == 200
        assert response.json() == {
            "area": "1435000",
            "equivalent": "49.826",
            "verdict": "FAIL",
        }

    def test_threshold_boundary_passes(self):
        response = client.post(
            "/adjudicate",
            json=[{"timestamp": 0, "ppm": 25}, {"timestamp": 28800, "ppm": 25}],
        )
        assert response.status_code == 200
        assert response.json()["equivalent"] == "25.000"
        assert response.json()["verdict"] == "PASS"

    def test_round_half_up_tips_verdict(self):
        # Equivalent 25.0005 rounds up to 25.001 and therefore fails.
        response = client.post(
            "/adjudicate",
            json=[{"timestamp": 0, "ppm": 25}, {"timestamp": 28800, "ppm": 25.001}],
        )
        assert response.status_code == 200
        assert response.json()["equivalent"] == "25.001"
        assert response.json()["verdict"] == "FAIL"

    def test_ppm_accepts_decimal_strings(self):
        response = client.post(
            "/adjudicate",
            json=[
                {"timestamp": 0, "ppm": "12.345"},
                {"timestamp": 28800, "ppm": "12.345"},
            ],
        )
        assert response.status_code == 200
        assert response.json() == {
            "area": "355536.000",
            "equivalent": "12.345",
            "verdict": "PASS",
        }

    def test_two_point_sequence_is_accepted(self):
        response = client.post(
            "/adjudicate",
            json=[{"timestamp": 0, "ppm": 0}, {"timestamp": 28800, "ppm": 2}],
        )
        assert response.status_code == 200
        assert response.json() == {
            "area": "28800",
            "equivalent": "1.000",
            "verdict": "PASS",
        }


class TestDomainValidationErrors:
    @pytest.mark.parametrize(
        ("payload", "index", "category"),
        [
            ([], 0, "missing_endpoint"),
            ([{"timestamp": 0, "ppm": 1}], 0, "missing_endpoint"),
            (
                [{"timestamp": 5, "ppm": 1}, {"timestamp": 28800, "ppm": 1}],
                0,
                "missing_endpoint",
            ),
            (
                [{"timestamp": 0, "ppm": 1}, {"timestamp": 100, "ppm": 1}],
                1,
                "missing_endpoint",
            ),
            (
                [
                    {"timestamp": 0, "ppm": 1},
                    {"timestamp": 100, "ppm": 1},
                    {"timestamp": 100, "ppm": 2},
                    {"timestamp": 28800, "ppm": 1},
                ],
                2,
                "non_monotonic_timestamp",
            ),
            (
                [
                    {"timestamp": 0, "ppm": 1},
                    {"timestamp": 500, "ppm": 1},
                    {"timestamp": 400, "ppm": 1},
                    {"timestamp": 28800, "ppm": 1},
                ],
                2,
                "non_monotonic_timestamp",
            ),
            (
                [
                    {"timestamp": 0, "ppm": 1},
                    {"timestamp": 100, "ppm": -0.001},
                    {"timestamp": 28800, "ppm": 1},
                ],
                1,
                "ppm_out_of_range",
            ),
            (
                [
                    {"timestamp": 0, "ppm": 1},
                    {"timestamp": 100, "ppm": 1000.001},
                    {"timestamp": 28800, "ppm": 1},
                ],
                1,
                "ppm_out_of_range",
            ),
            (
                [
                    {"timestamp": 0, "ppm": 1},
                    {"timestamp": 100, "ppm": 0.0001},
                    {"timestamp": 28800, "ppm": 1},
                ],
                1,
                "ppm_precision_exceeded",
            ),
            # Same index: endpoint outranks monotonicity.
            (
                [
                    {"timestamp": 0, "ppm": 1},
                    {"timestamp": 20000, "ppm": 1},
                    {"timestamp": 15000, "ppm": 1},
                ],
                2,
                "missing_endpoint",
            ),
            # Same index: range outranks precision.
            (
                [
                    {"timestamp": 0, "ppm": 1},
                    {"timestamp": 100, "ppm": 1000.0001},
                    {"timestamp": 28800, "ppm": 1},
                ],
                1,
                "ppm_out_of_range",
            ),
            # Lowest index wins across categories.
            (
                [
                    {"timestamp": 5, "ppm": 1},
                    {"timestamp": 100, "ppm": 2000},
                    {"timestamp": 28800, "ppm": 1},
                ],
                0,
                "missing_endpoint",
            ),
        ],
    )
    def test_first_failure_is_reported(self, payload, index, category):
        response = client.post("/adjudicate", json=payload)
        assert response.status_code == 422
        body = response.json()
        assert set(body.keys()) == {"error"}
        error = body["error"]
        assert set(error.keys()) == {"index", "category", "message"}
        assert error["index"] == index
        assert error["category"] == category
        # The envelope must never leak adjudication results.
        for forbidden in ("area", "equivalent", "verdict"):
            assert forbidden not in body
            assert forbidden not in error


class TestMalformedPayloads:
    @pytest.mark.parametrize(
        "payload",
        [
            [{"timestamp": 1.5, "ppm": 1}, {"timestamp": 28800, "ppm": 1}],
            [{"timestamp": True, "ppm": 1}, {"timestamp": 28800, "ppm": 1}],
            [{"timestamp": "0", "ppm": 1}, {"timestamp": 28800, "ppm": 1}],
            [{"timestamp": 0, "ppm": "abc"}, {"timestamp": 28800, "ppm": 1}],
            [{"timestamp": 0, "ppm": None}, {"timestamp": 28800, "ppm": 1}],
            [{"timestamp": 0, "ppm": True}, {"timestamp": 28800, "ppm": 1}],
            [{"timestamp": 0}, {"timestamp": 28800, "ppm": 1}],
            [{"ppm": 1}, {"timestamp": 28800, "ppm": 1}],
            [{"timestamp": 0, "ppm": 1, "unit": "ppm"}, {"timestamp": 28800, "ppm": 1}],
        ],
    )
    def test_malformed_points_are_rejected(self, payload):
        response = client.post("/adjudicate", json=payload)
        assert response.status_code == 422

    def test_object_body_is_rejected(self):
        response = client.post("/adjudicate", json={"samples": VALID_SEQUENCE})
        assert response.status_code == 422

    def test_empty_body_is_rejected(self):
        response = client.post("/adjudicate")
        assert response.status_code == 422


class TestHealth:
    def test_healthz(self):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
