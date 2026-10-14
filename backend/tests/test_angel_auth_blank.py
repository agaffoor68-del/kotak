"""Blank-body (HTTP 200, non-JSON) handling for Angel One SmartAPI.

SmartAPI intermittently answers a valid request with ``HTTP 200`` and an
empty ``text/html`` body — most often while its backend is under load around
market open (09:15–11:00 IST). Its edge also answers real rejections (a WAF
block, a bad ``X-PrivateKey``) with ``200`` plus an HTML page. Both look like
"non-JSON body" to a naive client, so every request path — login, refresh,
profile and secured calls — must retry the transient blank reply, never retry
a genuine rejection, and only surface the descriptive error once the retries
are exhausted.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests

from backend.angel import auth
from backend.core.vault import AngelCredentials

#: A standard base32 TOTP seed so pyotp can mint a code without a real account.
TOTP_SEED = "JBSWY3DPEHPK3PXP"
ACCOUNT_ID = "AB1234"


def _response(body: str, *, status: int = 200, content_type: str = "text/html") -> requests.Response:
    """Build a bare ``requests.Response`` the way SmartAPI's edge answers."""
    response = requests.Response()
    response.status_code = status
    response.headers["Content-Type"] = content_type
    response._content = body.encode("utf-8")
    return response


def _blank() -> requests.Response:
    """The reply from the bug report: HTTP 200, text/html, empty body."""
    return _response("")


def _json(payload: dict) -> requests.Response:
    return _response(json.dumps(payload), content_type="application/json")


def _credentials() -> AngelCredentials:
    return AngelCredentials(api_key="key", client_code=ACCOUNT_ID, pin="1234", totp_key=TOTP_SEED)


def _manager_with_session() -> tuple[auth.AngelSessionManager, auth.AngelSession]:
    """A manager with a fresh session already in place, so no login happens."""
    manager = auth.AngelSessionManager()
    session = auth.AngelSession(
        cred=_credentials(),
        access_token="old-token",
        refresh_token="refresh-1",
        feed_token="feed-1",
    )
    manager._sessions[ACCOUNT_ID] = session
    return manager, session


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Keep retries instant; only the retry *count* matters in these tests."""
    monkeypatch.setattr(auth, "BLANK_BODY_BACKOFF_SECONDS", 0.0)


# -- _describe_non_json / _is_transient_blank ------------------------------


def test_describe_non_json_reports_status_content_type_and_empty_body():
    message = auth._describe_non_json(_blank())
    assert "HTTP 200" in message
    assert "text/html" in message
    assert "the body was empty" in message


def test_describe_non_json_includes_a_body_snippet_when_present():
    message = auth._describe_non_json(_response("<html>Request Rejected</html>"))
    assert "the body began with" in message
    assert "Request Rejected" in message


def test_blank_classifier_accepts_the_empty_200_reply():
    assert auth._is_transient_blank(_blank()) is True


def test_blank_classifier_accepts_truncated_json():
    truncated = _response('{"status": tru', content_type="application/json")
    assert auth._is_transient_blank(truncated) is True


def test_blank_classifier_never_retries_a_waf_rejection_page():
    assert auth._is_transient_blank(_response("<html>Request Rejected</html>")) is False


def test_blank_classifier_never_retries_parseable_json():
    assert auth._is_transient_blank(_json({"status": True})) is False


def test_blank_classifier_ignores_error_statuses():
    assert auth._is_transient_blank(_response("", status=500)) is False
    assert auth._is_transient_blank(_response("<html>oops</html>", status=502)) is False


# -- _send_with_blank_retry ------------------------------------------------


def test_send_helper_retries_transient_blanks_then_returns_json():
    send = MagicMock(side_effect=[_blank(), _blank(), _json({"status": True})])

    response = auth._send_with_blank_retry(send, "probe")

    assert send.call_count == 3
    assert response.json() == {"status": True}


def test_send_helper_gives_up_after_the_configured_retry_count():
    send = MagicMock(return_value=_blank())

    response = auth._send_with_blank_retry(send, "probe")

    assert send.call_count == auth.BLANK_BODY_RETRIES + 1
    assert auth._is_transient_blank(response)


def test_send_helper_returns_a_waf_rejection_on_the_first_attempt():
    send = MagicMock(return_value=_response("<html>Request Rejected</html>"))

    response = auth._send_with_blank_retry(send, "probe")

    assert send.call_count == 1
    assert "Request Rejected" in response.text


# -- AngelSessionManager.call (secured calls) ------------------------------


def test_call_retries_blank_body_then_returns_payload(monkeypatch):
    manager, session = _manager_with_session()
    send = MagicMock(side_effect=[_blank(), _json({"status": True, "data": {"rms": "1000"}})])
    monkeypatch.setattr(auth.requests, "request", send)

    payload = manager.call(ACCOUNT_ID, session.cred, "rms", auth.RMS_PATH, http_method="GET")

    assert send.call_count == 2
    assert payload["data"]["rms"] == "1000"


def test_call_surfaces_the_descriptive_error_once_retries_are_exhausted(monkeypatch):
    manager, session = _manager_with_session()
    send = MagicMock(return_value=_blank())
    monkeypatch.setattr(auth.requests, "request", send)

    with pytest.raises(auth.AngelAuthError) as excinfo:
        manager.call(ACCOUNT_ID, session.cred, "rms", auth.RMS_PATH, http_method="GET")

    assert send.call_count == auth.BLANK_BODY_RETRIES + 1
    message = str(excinfo.value)
    assert "non-JSON body" in message
    assert "HTTP 200" in message
    assert "text/html" in message
    assert "the body was empty" in message


def test_call_never_retries_a_waf_rejection(monkeypatch):
    manager, session = _manager_with_session()
    send = MagicMock(return_value=_response("<html>Request Rejected</html>"))
    monkeypatch.setattr(auth.requests, "request", send)

    with pytest.raises(auth.AngelAuthError) as excinfo:
        manager.call(ACCOUNT_ID, session.cred, "rms", auth.RMS_PATH, http_method="GET")

    assert send.call_count == 1
    assert "Request Rejected" in str(excinfo.value)


# -- login (_establish) ----------------------------------------------------


def test_login_retries_blank_body_then_establishes_the_session(monkeypatch):
    post = MagicMock(
        side_effect=[
            _blank(),
            _json(
                {
                    "status": True,
                    "data": {"jwtToken": "jwt-1", "refreshToken": "ref-1", "feedToken": "feed-1"},
                }
            ),
        ]
    )
    profile = MagicMock(return_value=_json({"status": True, "data": {"name": "TRADER"}}))
    monkeypatch.setattr(auth.requests, "post", post)
    monkeypatch.setattr(auth.requests, "get", profile)

    session = auth.AngelSessionManager()._establish(_credentials())

    assert post.call_count == 2
    assert session.access_token == "jwt-1"
    assert session.refresh_token == "ref-1"
    assert session.feed_token == "feed-1"
    assert session.profile["name"] == "TRADER"


def test_login_surfaces_the_error_when_blanks_never_clear(monkeypatch):
    post = MagicMock(return_value=_blank())
    monkeypatch.setattr(auth.requests, "post", post)

    with pytest.raises(auth.AngelAuthError) as excinfo:
        auth.AngelSessionManager()._establish(_credentials())

    assert post.call_count == auth.BLANK_BODY_RETRIES + 1
    message = str(excinfo.value)
    assert "non-JSON body" in message
    assert "the body was empty" in message


# -- refresh ---------------------------------------------------------------


def test_refresh_retries_blank_body_then_mints_a_new_jwt(monkeypatch):
    manager, session = _manager_with_session()
    post = MagicMock(side_effect=[_blank(), _json({"status": True, "data": {"jwtToken": "jwt-2"}})])
    monkeypatch.setattr(auth.requests, "post", post)

    refreshed = manager._refresh(session)

    assert post.call_count == 2
    assert refreshed.access_token == "jwt-2"
    assert refreshed.refresh_token == "refresh-1"  # untouched


def test_refresh_blank_body_that_never_clears_forces_a_full_relogin(monkeypatch):
    manager, session = _manager_with_session()
    post = MagicMock(return_value=_blank())
    monkeypatch.setattr(auth.requests, "post", post)

    with pytest.raises(auth.AngelSessionExpired):
        manager._refresh(session)

    assert post.call_count == auth.BLANK_BODY_RETRIES + 1


# -- profile ---------------------------------------------------------------


def test_profile_retries_blank_body_then_returns_the_profile(monkeypatch):
    manager, session = _manager_with_session()
    get = MagicMock(side_effect=[_blank(), _json({"status": True, "data": {"name": "TRADER"}})])
    monkeypatch.setattr(auth.requests, "get", get)

    profile = manager._fetch_profile(session)

    assert get.call_count == 2
    assert profile["name"] == "TRADER"


def test_profile_swallows_a_persistent_blank_and_returns_nothing(monkeypatch):
    manager, session = _manager_with_session()
    monkeypatch.setattr(auth.requests, "get", MagicMock(return_value=_blank()))

    assert manager._fetch_profile(session) == {}

