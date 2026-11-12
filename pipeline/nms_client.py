"""A small client for the NMS-API (NOTAM Management Service).

Secrets come from the environment only (NMS_CLIENT_ID, NMS_CLIENT_SECRET) and
are never printed, logged or written to a file; the bearer token lives in
memory for one run. The host comes from NMS_API_HOST (staging by default).

Behaviour the service asks for:
  * the token endpoint is on the bare host (no /nmsapi) and a token lasts
    1799 s — one is fetched per run and renewed once on a 401;
  * requests are spaced MIN_INTERVAL_S apart (staging allows 1 per second);
  * a 429 is answered by waiting (Retry-After when given, else a growing
    backoff), never by a fast retry loop.
"""
import os
import time

import requests

DEFAULT_HOST = "https://api-staging.cgifederal-aim.com"
MIN_INTERVAL_S = 1.2
# The service gives up on a request after 30 s (HTTP 408); allow a margin.
TIMEOUT_S = 60
BACKOFF_S = (10, 30, 90)


class NmsAuthError(RuntimeError):
    """The token could not be obtained (missing or rejected credentials)."""


class NmsHttpError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def _safe_error_text(res: requests.Response) -> str:
    """The service's own error message, without echoing anything we sent."""
    try:
        body = res.json()
    except ValueError:
        return res.reason or ""
    if not isinstance(body, dict):
        return res.reason or ""
    for key in ("message", "ErrorCode", "Error"):
        if isinstance(body.get(key), str):
            return body[key][:200]
    return res.reason or ""


class NmsClient:
    def __init__(self, host: str | None = None, sleep=time.sleep, clock=time.monotonic):
        self.host = (host or os.environ.get("NMS_API_HOST") or DEFAULT_HOST).rstrip("/")
        self.base = f"{self.host}/nmsapi/v1"
        self._session = requests.Session()
        self._token: str | None = None
        self._last_request = None
        self._sleep = sleep
        self._clock = clock
        self.request_count = 0

    # -- pacing ----------------------------------------------------------
    def _pace(self):
        if self._last_request is not None:
            wait = MIN_INTERVAL_S - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()
        self.request_count += 1

    # -- auth ------------------------------------------------------------
    def authenticate(self):
        client_id = os.environ.get("NMS_CLIENT_ID", "")
        client_secret = os.environ.get("NMS_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise NmsAuthError(
                "NMS_CLIENT_ID / NMS_CLIENT_SECRET are not set in the environment"
            )
        self._pace()
        try:
            res = self._session.post(
                f"{self.host}/v1/auth/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                timeout=TIMEOUT_S,
            )
        except requests.RequestException as e:
            # str(e) of a requests error carries the URL, never the auth pair.
            raise NmsAuthError(f"token request failed: {type(e).__name__}") from None
        if res.status_code != 200:
            raise NmsAuthError(
                f"token request rejected: HTTP {res.status_code} {_safe_error_text(res)}"
            )
        try:
            token = res.json().get("access_token")
        except ValueError:
            token = None
        if not isinstance(token, str) or not token:
            raise NmsAuthError("token response carried no access_token")
        self._token = token

    # -- requests --------------------------------------------------------
    def get(self, path: str, params: dict | None = None, headers: dict | None = None,
            stream: bool = False) -> requests.Response:
        """GET {base}{path} (or an absolute /nmsapi/... content path).

        Renews the token once on a 401, waits out 429s, raises NmsHttpError
        on any other non-200 answer.
        """
        if self._token is None:
            self.authenticate()
        url = f"{self.host}{path}" if path.startswith("/nmsapi/") else f"{self.base}{path}"
        renewed = False
        backoffs = list(BACKOFF_S)
        while True:
            self._pace()
            res = self._session.get(
                url,
                params=params,
                headers={**(headers or {}), "Authorization": f"Bearer {self._token}"},
                timeout=TIMEOUT_S,
                stream=stream,
            )
            if res.status_code == 200:
                return res
            if res.status_code == 401 and not renewed:
                renewed = True
                self.authenticate()
                continue
            if res.status_code == 429 and backoffs:
                wait = backoffs.pop(0)
                retry_after = res.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    wait = max(wait, min(int(retry_after), 600))
                print(f"rate limited (429) - waiting {wait}s")
                self._sleep(wait)
                continue
            raise NmsHttpError(res.status_code, _safe_error_text(res))

    def get_json(self, path: str, params: dict | None = None, headers: dict | None = None):
        return self.get(path, params=params, headers=headers).json()

    def notams(self, **params) -> list[dict]:
        """GET /notams in GeoJSON -> the list of features (may be empty)."""
        body = self.get_json(
            "/notams", params=params, headers={"nmsResponseFormat": "GEOJSON"}
        )
        data = body.get("data") if isinstance(body, dict) else None
        features = data.get("geojson") if isinstance(data, dict) else None
        return features if isinstance(features, list) else []


def notam_of(feature: dict) -> dict:
    """The `notam` object of a GeoJSON feature ({} when the shape differs)."""
    try:
        notam = feature["properties"]["coreNOTAMData"]["notam"]
    except (KeyError, TypeError):
        return {}
    return notam if isinstance(notam, dict) else {}


def is_israeli(notam: dict) -> bool:
    """LL* location / ICAO location, or the Tel-Aviv FIR as affected FIR."""
    for key in ("icaoLocation", "location", "affectedFir"):
        value = notam.get(key)
        if isinstance(value, str) and value.upper().startswith("LL") and len(value) == 4:
            return True
    return False
