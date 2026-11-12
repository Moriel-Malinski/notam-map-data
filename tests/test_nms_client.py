"""NmsClient against a fake HTTP session: token, 401 renewal, 429 backoff,
pacing, and that no secret or token leaks into error messages."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import nms_client  # noqa: E402
from nms_client import NmsAuthError, NmsClient, NmsHttpError, is_israeli, notam_of  # noqa: E402

FAKE_ID = "unit-test-id"
FAKE_SECRET = "unit-test-secret"


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.reason = "reason"
        self.text = str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, posts=(), gets=()):
        self.posts = list(posts)
        self.gets = list(gets)
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kw):
        self.post_calls.append((url, kw))
        return self.posts.pop(0)

    def get(self, url, **kw):
        self.get_calls.append((url, kw))
        return self.gets.pop(0)


def token(value="tok-1"):
    return FakeResponse(200, {"access_token": value, "expires_in": "1799"})


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("NMS_CLIENT_ID", FAKE_ID)
    monkeypatch.setenv("NMS_CLIENT_SECRET", FAKE_SECRET)
    monkeypatch.delenv("NMS_API_HOST", raising=False)


def make(session):
    sleeps = []
    client = NmsClient(sleep=sleeps.append, clock=lambda: 0.0)
    client._session = session
    return client, sleeps


def test_token_url_has_no_nmsapi_and_uses_basic_auth(env):
    session = FakeSession(posts=[token()], gets=[FakeResponse(200, {})])
    client, _ = make(session)
    client.get("/ping")
    url, kw = session.post_calls[0]
    assert url == "https://api-staging.cgifederal-aim.com/v1/auth/token"
    assert kw["auth"] == (FAKE_ID, FAKE_SECRET)
    assert kw["data"] == {"grant_type": "client_credentials"}
    get_url, get_kw = session.get_calls[0]
    assert get_url == "https://api-staging.cgifederal-aim.com/nmsapi/v1/ping"
    assert get_kw["headers"]["Authorization"] == "Bearer tok-1"


def test_host_from_environment(env, monkeypatch):
    monkeypatch.setenv("NMS_API_HOST", "https://api-nms.aim.faa.gov/")
    client = NmsClient()
    assert client.base == "https://api-nms.aim.faa.gov/nmsapi/v1"


def test_missing_credentials_fail_clearly(monkeypatch):
    monkeypatch.delenv("NMS_CLIENT_ID", raising=False)
    monkeypatch.delenv("NMS_CLIENT_SECRET", raising=False)
    client, _ = make(FakeSession())
    with pytest.raises(NmsAuthError):
        client.authenticate()


def test_rejected_credentials_do_not_leak(env):
    session = FakeSession(
        posts=[FakeResponse(401, {"ErrorCode": "invalid_client",
                                  "Error": "Client credentials are invalid"})]
    )
    client, _ = make(session)
    with pytest.raises(NmsAuthError) as err:
        client.authenticate()
    assert "401" in str(err.value)
    assert FAKE_ID not in str(err.value) and FAKE_SECRET not in str(err.value)


def test_401_renews_token_once_and_retries(env):
    session = FakeSession(
        posts=[token("old"), token("new")],
        gets=[FakeResponse(401, {"message": "Access Token is Invalid or Expired"}),
              FakeResponse(200, {"ok": True})],
    )
    client, _ = make(session)
    assert client.get_json("/ping") == {"ok": True}
    assert len(session.post_calls) == 2
    assert session.get_calls[1][1]["headers"]["Authorization"] == "Bearer new"


def test_second_401_is_an_error_not_a_loop(env):
    session = FakeSession(
        posts=[token(), token()],
        gets=[FakeResponse(401, {"message": "x"}), FakeResponse(401, {"message": "x"})],
    )
    client, _ = make(session)
    with pytest.raises(NmsHttpError) as err:
        client.get("/ping")
    assert err.value.status == 401
    assert len(session.get_calls) == 2


def test_429_waits_with_backoff_then_succeeds(env):
    session = FakeSession(
        posts=[token()],
        gets=[FakeResponse(429), FakeResponse(429, headers={"Retry-After": "120"}),
              FakeResponse(200, {})],
    )
    client, sleeps = make(session)
    client.get("/ping")
    waits = [s for s in sleeps if s >= nms_client.BACKOFF_S[0]]
    assert waits == [nms_client.BACKOFF_S[0], 120]


def test_429_gives_up_after_the_backoff_list(env):
    n = len(nms_client.BACKOFF_S) + 1
    session = FakeSession(posts=[token()], gets=[FakeResponse(429) for _ in range(n)])
    client, _ = make(session)
    with pytest.raises(NmsHttpError) as err:
        client.get("/ping")
    assert err.value.status == 429
    assert len(session.get_calls) == n


def test_requests_are_paced(env):
    session = FakeSession(posts=[token()],
                          gets=[FakeResponse(200, {}), FakeResponse(200, {})])
    client, sleeps = make(session)  # frozen clock: every call must wait
    client.get("/ping")
    client.get("/ping")
    assert sleeps.count(nms_client.MIN_INTERVAL_S) == 2  # token->get, get->get


def test_notams_sends_geojson_header_and_returns_features(env):
    feature = {"properties": {"coreNOTAMData": {"notam": {"icaoLocation": "LLBG"}}}}
    session = FakeSession(
        posts=[token()],
        gets=[FakeResponse(200, {"status": "Success", "data": {"geojson": [feature]}})],
    )
    client, _ = make(session)
    assert client.notams(location="LLBG") == [feature]
    kw = session.get_calls[0][1]
    assert kw["headers"]["nmsResponseFormat"] == "GEOJSON"
    assert kw["params"] == {"location": "LLBG"}


def test_notams_tolerates_an_empty_or_odd_body(env):
    session = FakeSession(posts=[token()],
                          gets=[FakeResponse(200, {"status": "Success", "data": {}})])
    client, _ = make(session)
    assert client.notams(location="LLLL") == []


def test_is_israeli():
    assert is_israeli({"icaoLocation": "LLBG"})
    assert is_israeli({"location": "LLLL"})
    assert is_israeli({"affectedFir": "LLLL", "location": "XXX"})
    assert not is_israeli({"icaoLocation": "KATL", "location": "ATL"})
    assert not is_israeli({"location": "LL"})
    assert notam_of({"properties": None}) == {}
