from unittest.mock import MagicMock, patch

import pytest

import fitatu_mcp.fitatu_client as fc
from fitatu_mcp.fitatu_client import DAY_URL_TEMPLATE, FitatuClient


def _resp(status, json_data=None, text=""):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = json_data
    response.text = text
    return response


def _make_client():
    client = FitatuClient("user@example.com", "pw")
    client.token = "tok"
    client.refresh_token = "rt"
    client.user_id = "12345678"
    return client


class TestAuthedGetHappyPath:
    def test_sets_authorization_and_cluster_headers(self):
        client = _make_client()
        with patch.object(fc, "requests") as mock_requests:
            mock_requests.get.return_value = _resp(200, {"ok": True})
            result = client._authed_get("https://example.test/x")

        assert result == {"ok": True}
        _, kwargs = mock_requests.get.call_args
        headers = kwargs["headers"]
        assert headers["Authorization"] == "Bearer tok"
        assert headers["API-Cluster"] == "pl-pl12345678"
        assert headers["app-os"] == "FITATU-WEB"

    def test_passes_params_through(self):
        client = _make_client()
        with patch.object(fc, "requests") as mock_requests:
            mock_requests.get.return_value = _resp(200, [])
            client._authed_get("https://example.test/x", params={"limit": 5, "page": 1})
        _, kwargs = mock_requests.get.call_args
        assert kwargs["params"] == {"limit": 5, "page": 1}


class TestAuthedGetRecovery:
    def test_401_then_refresh_success_retries_without_relogin(self):
        client = _make_client()
        client.refresh = MagicMock(return_value=True)
        client.login = MagicMock()
        with patch.object(fc, "requests") as mock_requests:
            mock_requests.get.side_effect = [_resp(401, text="unauth"), _resp(200, {"ok": True})]
            result = client._authed_get("https://example.test/x")

        assert result == {"ok": True}
        client.refresh.assert_called_once()
        client.login.assert_not_called()
        assert mock_requests.get.call_count == 2

    def test_401_then_refresh_fails_relogins_and_retries(self):
        client = _make_client()
        client.refresh = MagicMock(return_value=False)
        client.login = MagicMock()
        with patch.object(fc, "requests") as mock_requests:
            mock_requests.get.side_effect = [_resp(401, text="unauth"), _resp(200, {"ok": True})]
            result = client._authed_get("https://example.test/x")

        assert result == {"ok": True}
        client.refresh.assert_called_once()
        client.login.assert_called_once()
        assert mock_requests.get.call_count == 2

    def test_non_200_raises_runtime_error_with_status_and_body(self):
        client = _make_client()
        with patch.object(fc, "requests") as mock_requests:
            mock_requests.get.return_value = _resp(500, text="boom")
            with pytest.raises(RuntimeError, match="500"):
                client._authed_get("https://example.test/x")


class TestGetterUrlBuilders:
    def _call(self, method, *args, json_data=None, **kwargs):
        client = _make_client()
        with patch.object(fc, "requests") as mock_requests:
            mock_requests.get.return_value = _resp(200, json_data)
            result = getattr(client, method)(*args, **kwargs)
            call_args, call_kwargs = mock_requests.get.call_args
        url = call_args[0] if call_args else call_kwargs.get("url")
        return url, call_kwargs.get("params"), result

    def test_size_summary_url(self):
        url, params, _ = self._call("get_size_summary", json_data={})
        assert url == "https://pl-pl.fitatu.com/api/users/12345678/measurements/summary/size"
        assert params is None

    def test_metric_series_url_and_params(self):
        url, params, _ = self._call("get_metric_series", "chest", json_data=[])
        assert url == "https://pl-pl.fitatu.com/api/users/12345678/measurements/size/chest"
        assert params == {"limit": 500, "page": 1}

    def test_weight_summary_limit_only(self):
        url, params, _ = self._call("get_weight_summary", json_data=[], limit=1)
        assert url == "https://pl-pl.fitatu.com/api/users/12345678/measurements/summary/weight"
        assert params == {"limit": 1, "page": 1}

    def test_weight_summary_with_from_date(self):
        _, params, _ = self._call("get_weight_summary", json_data=[], limit=41, from_date="2026-04-01")
        assert params == {"limit": 41, "page": 1, "fromDate": "2026-04-01"}

    def test_weight_chart_url(self):
        url, params, _ = self._call("get_weight_chart", json_data={"weights": {}})
        assert url == "https://pl-pl.fitatu.com/api/users/12345678/measurements/chart/weight"
        assert params is None

    def test_day_measurements_url(self):
        url, _, _ = self._call("get_day_measurements", "2026-03-01", json_data={})
        assert url == "https://pl-pl.fitatu.com/api/users/12345678/measurements/2026-03-01"


class TestGetHeight:
    def test_extracts_height_and_units_from_user_settings(self):
        client = _make_client()
        payload = {"userSettings": {"height": 175.0, "heightCm": 175, "heightUnit": 1,
                                    "weightUnit": "KG", "sizeUnit": "CM"}}
        with patch.object(fc, "requests") as mock_requests:
            mock_requests.get.return_value = _resp(200, payload)
            result = client.get_height("2026-03-15")

        assert result["height_cm"] == 175
        assert result["height_unit"] == 1
        assert result["weight_unit"] == "KG"
        assert result["size_unit"] == "CM"

    def test_missing_user_settings_yields_none(self):
        client = _make_client()
        with patch.object(fc, "requests") as mock_requests:
            mock_requests.get.return_value = _resp(200, {})
            result = client.get_height("2026-03-15")
        assert result["height_cm"] is None


class TestGetDayRegression:
    def test_get_day_request_unchanged_after_refactor(self):
        client = _make_client()
        with patch.object(fc, "requests") as mock_requests:
            mock_requests.get.return_value = _resp(200, {"dietPlan": {}})
            result = client.get_day("2026-06-06")

        assert result == {"dietPlan": {}}
        call_args, call_kwargs = mock_requests.get.call_args
        url = call_args[0] if call_args else call_kwargs.get("url")
        assert url == DAY_URL_TEMPLATE.format(user_id="12345678", date="2026-06-06")
        headers = call_kwargs["headers"]
        assert headers["Authorization"] == "Bearer tok"
        assert headers["API-Cluster"] == "pl-pl12345678"
        # get_day issues no query params (behaviour-preserving)
        assert call_kwargs.get("params") is None
