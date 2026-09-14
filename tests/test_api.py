"""HTTP-level tests for the adjudication endpoint."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

JSON_HEADERS = {"Content-Type": "application/json"}

VALID_SEQUENCE = [
    {"timestamp": 0, "ppm": 10},
    {"timestamp": 14400, "ppm": 10},
    {"timestamp": 28800, "ppm": 10},
]


def assert_error_envelope(body: dict, index: int, category: str) -> None:
    """The 422 payload must be the unique first failure and nothing else."""
    assert set(body.keys()) == {"error"}
    error = body["error"]
    assert set(error.keys()) == {"index", "category", "message"}
    assert error["index"] == index
    assert error["category"] == category
    assert isinstance(error["message"], str) and error["message"]
    for forbidden in ("area", "equivalent", "verdict"):
        assert forbidden not in body
        assert forbidden not in error


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
                    {"timestamp": 100, "ppm": 0.0005},
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
        assert_error_envelope(response.json(), index, category)


class TestLexicalPrecision:
    """Precision is judged on the literal JSON form, trailing zeros included."""

    @pytest.mark.parametrize(
        "ppm_literal", ["12.3400", "0.0000", "1.0000", "1000.0000", "0.0005"]
    )
    def test_four_decimal_place_numbers_are_rejected(self, ppm_literal):
        body = (
            f'[{{"timestamp":0,"ppm":{ppm_literal}}},'
            f'{{"timestamp":28800,"ppm":1}}]'
        )
        response = client.post("/adjudicate", content=body, headers=JSON_HEADERS)
        assert response.status_code == 422
        assert_error_envelope(response.json(), 0, "ppm_precision_exceeded")

    @pytest.mark.parametrize("ppm_literal", ["12.340", "1.000", "0.001", "1000.000"])
    def test_three_decimal_place_numbers_are_accepted(self, ppm_literal):
        body = (
            f'[{{"timestamp":0,"ppm":{ppm_literal}}},'
            f'{{"timestamp":28800,"ppm":{ppm_literal}}}]'
        )
        response = client.post("/adjudicate", content=body, headers=JSON_HEADERS)
        assert response.status_code == 200

    def test_string_ppm_is_a_type_error_not_precision(self):
        # A string is the wrong JSON type entirely; it must not be treated
        # as a precision problem even when the literal has four decimals.
        response = client.post(
            "/adjudicate",
            json=[
                {"timestamp": 0, "ppm": "12.3400"},
                {"timestamp": 28800, "ppm": 1},
            ],
        )
        assert response.status_code == 422
        assert_error_envelope(response.json(), 0, "invalid_type")


class TestTypeErrors:
    """Malformed points must yield the same unique-first-error envelope."""

    @pytest.mark.parametrize(
        ("payload", "index"),
        [
            ([{"timestamp": 1.5, "ppm": 1}, {"timestamp": 28800, "ppm": 1}], 0),
            ([{"timestamp": True, "ppm": 1}, {"timestamp": 28800, "ppm": 1}], 0),
            ([{"timestamp": "0", "ppm": 1}, {"timestamp": 28800, "ppm": 1}], 0),
            ([{"timestamp": 0, "ppm": "abc"}, {"timestamp": 28800, "ppm": 1}], 0),
            ([{"timestamp": 0, "ppm": "12.340"}, {"timestamp": 28800, "ppm": 1}], 0),
            ([{"timestamp": 0, "ppm": "12"}, {"timestamp": 28800, "ppm": 1}], 0),
            ([{"timestamp": 0, "ppm": None}, {"timestamp": 28800, "ppm": 1}], 0),
            ([{"timestamp": 0, "ppm": True}, {"timestamp": 28800, "ppm": 1}], 0),
            ([{"timestamp": 0, "ppm": [1]}, {"timestamp": 28800, "ppm": 1}], 0),
            ([{"timestamp": 0}, {"timestamp": 28800, "ppm": 1}], 0),
            ([{"ppm": 1}, {"timestamp": 28800, "ppm": 1}], 0),
            (
                [
                    {"timestamp": 0, "ppm": 1, "unit": "ppm"},
                    {"timestamp": 28800, "ppm": 1},
                ],
                0,
            ),
            (
                [
                    {"timestamp": 0, "ppm": 1},
                    "not-a-point",
                    {"timestamp": 28800, "ppm": 1},
                ],
                1,
            ),
            # Multiple malformed points still collapse to one first error.
            (
                [
                    {"timestamp": "a", "ppm": "b"},
                    {"timestamp": "c"},
                    {"timestamp": 28800, "ppm": 1},
                ],
                0,
            ),
        ],
    )
    def test_type_errors_yield_single_envelope(self, payload, index):
        response = client.post("/adjudicate", json=payload)
        assert response.status_code == 422
        assert_error_envelope(response.json(), index, "invalid_type")

    def test_domain_error_at_lower_index_beats_type_error(self):
        response = client.post(
            "/adjudicate",
            json=[
                {"timestamp": 5, "ppm": 1},
                {"timestamp": "oops", "ppm": 1},
                {"timestamp": 28800, "ppm": 1},
            ],
        )
        assert response.status_code == 422
        assert_error_envelope(response.json(), 0, "missing_endpoint")

    def test_type_error_at_lower_index_beats_domain_error(self):
        response = client.post(
            "/adjudicate",
            json=[
                {"timestamp": 0, "ppm": "oops"},
                {"timestamp": 100, "ppm": 2000},
                {"timestamp": 28800, "ppm": 1},
            ],
        )
        assert response.status_code == 422
        assert_error_envelope(response.json(), 0, "invalid_type")


class TestMalformedBodies:
    def test_object_body_is_rejected(self):
        response = client.post("/adjudicate", json={"samples": VALID_SEQUENCE})
        assert response.status_code == 422
        assert_error_envelope(response.json(), 0, "invalid_type")

    def test_empty_body_is_rejected(self):
        response = client.post("/adjudicate")
        assert response.status_code == 422
        assert_error_envelope(response.json(), 0, "invalid_type")

    def test_unparseable_json_is_rejected(self):
        response = client.post(
            "/adjudicate", content=b'[{"timestamp":0,', headers=JSON_HEADERS
        )
        assert response.status_code == 422
        assert_error_envelope(response.json(), 0, "invalid_type")

    def test_non_finite_number_is_rejected(self):
        body = '[{"timestamp":0,"ppm":NaN},{"timestamp":28800,"ppm":1}]'
        response = client.post("/adjudicate", content=body, headers=JSON_HEADERS)
        assert response.status_code == 422
        assert_error_envelope(response.json(), 0, "invalid_type")


class TestExceedance:
    """``include_exceedance`` opt-in threshold-crossing analysis."""

    TWO_CROSSINGS_SEQUENCE = [
        {"timestamp": 0, "ppm": 20},
        {"timestamp": 10, "ppm": 30},
        {"timestamp": 20, "ppm": 20},
        {"timestamp": 28800, "ppm": 20},
    ]

    def test_default_call_omits_exceedance_completely(self):
        response = client.post("/adjudicate", json=VALID_SEQUENCE)
        assert response.status_code == 200
        assert set(response.json().keys()) == {"area", "equivalent", "verdict"}

    @pytest.mark.parametrize("flag", ["false", "0", "no", "False", "OFF"])
    def test_falsey_values_omit_exceedance(self, flag):
        response = client.post(
            f"/adjudicate?include_exceedance={flag}", json=VALID_SEQUENCE
        )
        assert response.status_code == 200
        assert "exceedance" not in response.json()

    @pytest.mark.parametrize("flag", ["true", "1", "yes", "on", "TRUE"])
    def test_truthy_values_attach_exceedance(self, flag):
        response = client.post(
            f"/adjudicate?include_exceedance={flag}", json=VALID_SEQUENCE
        )
        assert response.status_code == 200
        assert response.json()["exceedance"] == {
            "total_seconds": "0",
            "longest_segment": None,
        }

    def test_two_crossings_between_samples_are_interpolated(self):
        response = client.post(
            "/adjudicate?include_exceedance=true",
            json=self.TWO_CROSSINGS_SEQUENCE,
        )
        assert response.status_code == 200
        body = response.json()
        # Main verdict payload is unchanged; exceedance is purely additive.
        assert body["area"] == "576100"
        assert body["equivalent"] == "20.003"
        assert body["verdict"] == "PASS"
        # 20 -> 30 crosses 25 at second 5, 30 -> 20 crosses back at 15.
        assert body["exceedance"] == {
            "total_seconds": "10",
            "longest_segment": {
                "start": "5",
                "end": "15",
                "duration_seconds": "10",
            },
        }

    def test_fractional_crossings_serialize_with_three_decimal_places(self):
        response = client.post(
            "/adjudicate?include_exceedance=true",
            json=[
                {"timestamp": 0, "ppm": 0},
                {"timestamp": 10, "ppm": 30},
                {"timestamp": 20, "ppm": 0},
                {"timestamp": 28800, "ppm": 0},
            ],
        )
        assert response.status_code == 200
        segment = response.json()["exceedance"]["longest_segment"]
        assert segment == {
            "start": "8.333",
            "end": "11.667",
            "duration_seconds": "3.334",
        }

    def test_contiguous_stretch_merges_across_several_segments(self):
        response = client.post(
            "/adjudicate?include_exceedance=true",
            json=[
                {"timestamp": 0, "ppm": 0},
                {"timestamp": 5, "ppm": 50},
                {"timestamp": 10, "ppm": 50},
                {"timestamp": 15, "ppm": 50},
                {"timestamp": 20, "ppm": 0},
                {"timestamp": 28800, "ppm": 0},
            ],
        )
        assert response.status_code == 200
        assert response.json()["exceedance"] == {
            "total_seconds": "15",
            "longest_segment": {
                "start": "2.5",
                "end": "17.5",
                "duration_seconds": "15",
            },
        }

    def test_tied_longest_segments_return_the_earlier_one(self):
        # Two triangular excursions that both round to 3.334 seconds.
        response = client.post(
            "/adjudicate?include_exceedance=true",
            json=[
                {"timestamp": 0, "ppm": 0},
                {"timestamp": 10, "ppm": 30},
                {"timestamp": 20, "ppm": 0},
                {"timestamp": 100, "ppm": 0},
                {"timestamp": 110, "ppm": 30},
                {"timestamp": 120, "ppm": 0},
                {"timestamp": 28800, "ppm": 0},
            ],
        )
        assert response.status_code == 200
        assert response.json()["exceedance"] == {
            "total_seconds": "6.668",
            "longest_segment": {
                "start": "8.333",
                "end": "11.667",
                "duration_seconds": "3.334",
            },
        }

    def test_threshold_point_separates_two_stretches(self):
        response = client.post(
            "/adjudicate?include_exceedance=true",
            json=[
                {"timestamp": 0, "ppm": 0},
                {"timestamp": 10, "ppm": 30},
                {"timestamp": 20, "ppm": 25},
                {"timestamp": 30, "ppm": 30},
                {"timestamp": 45, "ppm": 0},
                {"timestamp": 28800, "ppm": 0},
            ],
        )
        assert response.status_code == 200
        # 8.333 -> 20 (11.667 s) versus 20 -> 32.5 (12.5 s): second wins.
        assert response.json()["exceedance"] == {
            "total_seconds": "24.167",
            "longest_segment": {
                "start": "20",
                "end": "32.5",
                "duration_seconds": "12.5",
            },
        }

    def test_constantly_equal_to_threshold_is_not_an_exceedance(self):
        response = client.post(
            "/adjudicate?include_exceedance=true",
            json=[{"timestamp": 0, "ppm": 25}, {"timestamp": 28800, "ppm": 25}],
        )
        assert response.status_code == 200
        body = response.json()
        assert body["verdict"] == "PASS"
        assert body["exceedance"] == {
            "total_seconds": "0",
            "longest_segment": None,
        }

    def test_only_touching_threshold_at_peak_is_not_an_exceedance(self):
        response = client.post(
            "/adjudicate?include_exceedance=true",
            json=[
                {"timestamp": 0, "ppm": 0},
                {"timestamp": 100, "ppm": 25},
                {"timestamp": 200, "ppm": 0},
                {"timestamp": 28800, "ppm": 0},
            ],
        )
        assert response.status_code == 200
        assert response.json()["exceedance"] == {
            "total_seconds": "0",
            "longest_segment": None,
        }

    @pytest.mark.parametrize("bad_value", ["maybe", "2", "", "tru", "yes!"])
    def test_unparseable_boolean_is_422_located_at_query_parameter(
        self, bad_value
    ):
        response = client.post(
            f"/adjudicate?include_exceedance={bad_value}", json=VALID_SEQUENCE
        )
        assert response.status_code == 422
        body = response.json()
        # The framework's query-parameter error envelope must not carry any
        # of the adjudication payload keys.
        assert set(body.keys()) == {"detail"}
        detail = body["detail"]
        assert isinstance(detail, list) and len(detail) == 1
        assert set(detail[0].keys()) == {"type", "loc", "msg", "input"}
        assert detail[0]["loc"] == ["query", "include_exceedance"]
        assert detail[0]["input"] == bad_value
        for forbidden in ("area", "equivalent", "verdict"):
            assert forbidden not in response.text

    def test_invalid_sample_with_flag_still_returns_first_error_only(self):
        response = client.post(
            "/adjudicate?include_exceedance=true",
            json=[
                {"timestamp": 0, "ppm": 30},
                {"timestamp": 100, "ppm": 2000},
                {"timestamp": 28800, "ppm": 30},
            ],
        )
        assert response.status_code == 422
        assert_error_envelope(response.json(), 1, "ppm_out_of_range")
        assert "exceedance" not in response.text

    def test_analysis_failure_never_leaks_area_or_verdict(self, monkeypatch):
        # Even if the new analysis blows up after adjudication already ran,
        # the response must be a plain server error, never a partial payload.
        secret = "SECRET-AREA-720000-VERDICT-PASS"

        def boom(_points):
            raise RuntimeError(secret)

        monkeypatch.setattr("app.main.analyze_exceedance", boom)
        guarded = TestClient(app, raise_server_exceptions=False)
        response = guarded.post(
            "/adjudicate?include_exceedance=true", json=VALID_SEQUENCE
        )
        assert response.status_code == 500
        for forbidden in ("area", "equivalent", "verdict", "exceedance", secret):
            assert forbidden not in response.text


class TestHealthAndDocs:
    def test_healthz(self):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_openapi_documents_array_request_body(self):
        response = client.get("/openapi.json")
        assert response.status_code == 200
        operation = response.json()["paths"]["/adjudicate"]["post"]
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        assert schema["type"] == "array"

    def test_openapi_documents_optional_boolean_query_parameter(self):
        response = client.get("/openapi.json")
        parameters = response.json()["paths"]["/adjudicate"]["post"]["parameters"]
        [parameter] = [
            p for p in parameters if p["name"] == "include_exceedance"
        ]
        assert parameter["in"] == "query"
        assert parameter["required"] is False
        assert parameter["schema"]["type"] == "boolean"
        assert parameter["schema"]["default"] is False
