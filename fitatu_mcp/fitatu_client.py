import base64
import json
import logging
import os
from typing import Any

import requests

LOGIN_URL = "https://pl-pl.fitatu.com/api/login"
REFRESH_URL = "https://pl-pl.fitatu.com/api/token/refresh"
DAY_URL_TEMPLATE = "https://pl-pl.fitatu.com/api/diet-and-activity-plan/{user_id}/day/{date}"

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

    def get_day(self, day_date: str) -> dict[str, Any]:
        if not self.token or not self.user_id:
            logger.info("No active Fitatu session; performing login before get_day")
            self.login()

        headers = BASE_HEADERS.copy()
        headers["Authorization"] = f"Bearer {self.token}"
        headers["API-Cluster"] = f"pl-pl{self.user_id}"
        url = DAY_URL_TEMPLATE.format(user_id=self.user_id, date=day_date)
        logger.info("Fetching Fitatu day data day_date=%s user_id=%s", day_date, self.user_id)

        response = requests.get(url, headers=headers, timeout=20)
        logger.info("Fitatu get_day response status=%s", response.status_code)
        if response.status_code == 401:
            logger.warning("Fitatu get_day returned 401; attempting refresh/login recovery")
            if not self.refresh():
                self.login()
                headers["Authorization"] = f"Bearer {self.token}"
            else:
                headers["Authorization"] = f"Bearer {self.token}"
            response = requests.get(url, headers=headers, timeout=20)
            logger.info("Fitatu get_day retry response status=%s", response.status_code)

        if response.status_code != 200:
            raise RuntimeError(f"get_day failed with status {response.status_code}: {response.text}")

        logger.info("Fitatu day fetch succeeded day_date=%s", day_date)
        return response.json()
