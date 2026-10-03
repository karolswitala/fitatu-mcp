import base64
import json
import logging
import os
from typing import Any

import requests

LOGIN_URL = "https://pl-pl.fitatu.com/api/login"
REFRESH_URL = "https://pl-pl.fitatu.com/api/token/refresh"
DAY_URL_TEMPLATE = "https://pl-pl.fitatu.com/api/diet-and-activity-plan/{user_id}/day/{date}"

_MEASUREMENTS_BASE = "https://pl-pl.fitatu.com/api/users/{user_id}"
MEASUREMENTS_SUMMARY_SIZE_URL = _MEASUREMENTS_BASE + "/measurements/summary/size"
MEASUREMENT_SERIES_URL = _MEASUREMENTS_BASE + "/measurements/size/{part}"
WEIGHT_SUMMARY_URL = _MEASUREMENTS_BASE + "/measurements/summary/weight"
WEIGHT_CHART_URL = _MEASUREMENTS_BASE + "/measurements/chart/weight"
DAY_MEASUREMENTS_URL = _MEASUREMENTS_BASE + "/measurements/{date}"
SETTINGS_NEW_URL = _MEASUREMENTS_BASE + "/settings-new/{date}"

FITATU_API_SECRET = os.getenv("FITATU_API_SECRET")
if not FITATU_API_SECRET:
    raise RuntimeError("FITATU_API_SECRET must be set")

BASE_HEADERS = {
    "accept": "application/json; version=v3",
    "api-key": "FITATU-MOBILE-APP",
    "api-secret": FITATU_API_SECRET,
    "app-os": "FITATU-WEB",
    "app-version": "4.5.4",
    "app-uuid": "64c2d1b0-c8ad-11e8-8956-0242ac120008",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "content-type": "application/json",
}


class FitatuAuthError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


class FitatuClient:
    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.token: str | None = None
        self.refresh_token: str | None = None
        self.user_id: str | None = None

    @staticmethod
    def _decode_jwt_payload(token: str | None) -> dict[str, Any] | None:
        if not token or token.count(".") < 2:
            return None

        payload_part = token.split(".")[1]
        payload_part += "=" * (-len(payload_part) % 4)
        try:
            decoded = base64.urlsafe_b64decode(payload_part)
            return json.loads(decoded.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None

    @classmethod
    def _extract_user_id_from_token(cls, token: str | None) -> str | None:
        payload = cls._decode_jwt_payload(token)
        if not payload:
            return None

        for key in ("user_id", "uid", "id", "sub"):
            value = payload.get(key)
            if value is not None and str(value).isdigit():
                return str(value)
        return None

    @staticmethod
    def _extract_user_id_from_login_response(data: dict[str, Any]) -> str | None:
        for key in ("user_id", "userId", "id"):
            value = data.get(key)
            if value is not None and str(value).isdigit():
                return str(value)

        user = data.get("user")
        if isinstance(user, dict):
            for key in ("id", "user_id", "userId"):
                value = user.get(key)
                if value is not None and str(value).isdigit():
                    return str(value)
        return None

    def login(self) -> None:
        logger.info("Fitatu login attempt started")
        payload = {"_username": self.username, "_password": self.password}
        response = requests.post(LOGIN_URL, headers=BASE_HEADERS, json=payload, timeout=20)
        logger.info("Fitatu login response status=%s", response.status_code)
        if response.status_code != 200:
            raise FitatuAuthError(f"Login failed with status {response.status_code}: {response.text}")

        data = response.json()
        token = data.get("token") or data.get("access_token")
        refresh_token = data.get("refresh_token") or data.get("refreshToken")
        if not token:
            raise FitatuAuthError("Login response does not include access token")

        self.token = token
        self.refresh_token = refresh_token
        self.user_id = self._extract_user_id_from_login_response(data) or self._extract_user_id_from_token(token)
        logger.info(
            "Fitatu login succeeded user_id=%s refresh_token_present=%s",
            self.user_id,
            bool(self.refresh_token),
        )

        if not self.user_id:
            raise FitatuAuthError("Could not determine user_id from login response or token")

    def refresh(self) -> bool:
        if not self.refresh_token:
            logger.warning("Fitatu token refresh skipped: no refresh token present")
            return False

        payload_variants = [
            {"refresh_token": self.refresh_token},
            {"refreshToken": self.refresh_token},
            {"token": self.refresh_token},
        ]

        logger.info("Fitatu token refresh attempt started")
        for payload in payload_variants:
            response = requests.post(REFRESH_URL, headers=BASE_HEADERS, json=payload, timeout=20)
            logger.info("Fitatu refresh response status=%s", response.status_code)
            if response.status_code != 200:
                continue

            data = response.json()
            new_token = data.get("token") or data.get("access_token")
            if not new_token:
                continue

            self.token = new_token
            self.refresh_token = data.get("refresh_token") or data.get("refreshToken") or self.refresh_token
            self.user_id = self._extract_user_id_from_token(new_token) or self.user_id
            logger.info("Fitatu token refresh succeeded user_id=%s", self.user_id)
            return True

        logger.warning("Fitatu token refresh failed for all payload variants")
        return False

    def _authed_get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        if not self.token or not self.user_id:
            logger.info("No active Fitatu session; performing login before request url=%s", url)
            self.login()

        headers = BASE_HEADERS.copy()
        headers["Authorization"] = f"Bearer {self.token}"
        headers["API-Cluster"] = f"pl-pl{self.user_id}"
        logger.info("Fitatu authed GET url=%s params=%s user_id=%s", url, params, self.user_id)

        response = requests.get(url, headers=headers, params=params, timeout=20)
        logger.info("Fitatu GET response status=%s url=%s", response.status_code, url)
        if response.status_code == 401:
            logger.warning("Fitatu GET returned 401; attempting refresh/login recovery url=%s", url)
            if not self.refresh():
                self.login()
            headers["Authorization"] = f"Bearer {self.token}"
            headers["API-Cluster"] = f"pl-pl{self.user_id}"
            response = requests.get(url, headers=headers, params=params, timeout=20)
            logger.info("Fitatu GET retry response status=%s url=%s", response.status_code, url)

        if response.status_code != 200:
            raise RuntimeError(f"GET {url} failed with status {response.status_code}: {response.text}")

        return response.json()

    def get_day(self, day_date: str) -> dict[str, Any]:
        if not self.token or not self.user_id:
            logger.info("No active Fitatu session; performing login before get_day")
            self.login()

        url = DAY_URL_TEMPLATE.format(user_id=self.user_id, date=day_date)
        logger.info("Fetching Fitatu day data day_date=%s user_id=%s", day_date, self.user_id)
        result = self._authed_get(url)
        logger.info("Fitatu day fetch succeeded day_date=%s", day_date)
        return result

    def get_size_summary(self) -> dict[str, Any]:
        if not self.user_id:
            self.login()
        url = MEASUREMENTS_SUMMARY_SIZE_URL.format(user_id=self.user_id)
        logger.info("Fetching Fitatu size summary user_id=%s", self.user_id)
        return self._authed_get(url)

    def get_metric_series(self, api_key: str, limit: int = 500) -> list[dict[str, Any]]:
        if not self.user_id:
            self.login()
        url = MEASUREMENT_SERIES_URL.format(user_id=self.user_id, part=api_key)
        logger.info("Fetching Fitatu metric series part=%s limit=%s user_id=%s", api_key, limit, self.user_id)
        return self._authed_get(url, params={"limit": limit, "page": 1})

    def get_weight_summary(self, limit: int = 500, from_date: str | None = None) -> list[dict[str, Any]]:
        if not self.user_id:
            self.login()
        url = WEIGHT_SUMMARY_URL.format(user_id=self.user_id)
        params: dict[str, Any] = {"limit": limit, "page": 1}
        if from_date:
            params["fromDate"] = from_date
        logger.info("Fetching Fitatu weight summary limit=%s from_date=%s user_id=%s", limit, from_date, self.user_id)
        return self._authed_get(url, params=params)

    def get_weight_chart(self) -> dict[str, Any]:
        if not self.user_id:
            self.login()
        url = WEIGHT_CHART_URL.format(user_id=self.user_id)
        logger.info("Fetching Fitatu weight chart user_id=%s", self.user_id)
        return self._authed_get(url)

    def get_day_measurements(self, day_date: str) -> dict[str, Any]:
        if not self.user_id:
            self.login()
        url = DAY_MEASUREMENTS_URL.format(user_id=self.user_id, date=day_date)
        logger.info("Fetching Fitatu day measurements day_date=%s user_id=%s", day_date, self.user_id)
        return self._authed_get(url)

    def get_height(self, day_date: str) -> dict[str, Any]:
        if not self.user_id:
            self.login()
        url = SETTINGS_NEW_URL.format(user_id=self.user_id, date=day_date)
        logger.info("Fetching Fitatu settings for height day_date=%s user_id=%s", day_date, self.user_id)
        data = self._authed_get(url)
        settings = data.get("userSettings") or {} if isinstance(data, dict) else {}
        return {
            "height_cm": settings.get("heightCm"),
            "height": settings.get("height"),
            "height_unit": settings.get("heightUnit"),
            "weight_unit": settings.get("weightUnit"),
            "size_unit": settings.get("sizeUnit"),
        }
