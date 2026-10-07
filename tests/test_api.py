"""Unit tests for api.py helpers that don't require network access."""
from custom_components.apsystems_ema.api import (
    _extract_reissued_jsessionid,
    _looks_like_login_page,
)


def test_extract_reissued_jsessionid_picks_last_real_value():
    """Regression test for the aiohttp CookieJar expiry quirk.

    The portal sends JSESSIONID three times per index-page response: a
    fresh value, then an invalidating "deleteMe" with Max-Age=0, then a
    re-issued fresh value. We must always pick the *last* non-"deleteMe"
    value, since that's the one the server expects on subsequent requests.
    """
    headers = [
        "JSESSIONID=aaaa1111-0000-0000-0000-000000000000; Path=/ema; HttpOnly; SameSite=lax",
        "rememberMe=deleteMe; Path=/ema; Max-Age=0; "
        "Expires=Mon, 05-Oct-2026 20:13:04 GMT; SameSite=lax",
        "JSESSIONID=deleteMe; Path=/ema; Max-Age=0; "
        "Expires=Mon, 05-Oct-2026 20:13:04 GMT; SameSite=lax",
        "JSESSIONID=bbbb2222-0000-0000-0000-000000000000; Path=/ema; HttpOnly; SameSite=lax",
        "language=en_US;Path=/;SameSite=None;Secure;Max-Age=86400",
    ]
    assert (
        _extract_reissued_jsessionid(headers)
        == "bbbb2222-0000-0000-0000-000000000000"
    )


def test_extract_reissued_jsessionid_single_value():
    """A single, non-rotated JSESSIONID is returned as-is."""
    headers = ["JSESSIONID=only-one-value; Path=/ema; HttpOnly"]
    assert _extract_reissued_jsessionid(headers) == "only-one-value"


def test_extract_reissued_jsessionid_no_cookie_returns_none():
    """No JSESSIONID at all (or only the deleteMe marker) yields None."""
    assert _extract_reissued_jsessionid([]) is None
    assert _extract_reissued_jsessionid(["JSESSIONID=deleteMe; Max-Age=0"]) is None


def test_looks_like_login_page_detects_html():
    assert _looks_like_login_page("<html><body>login</body></html>")
    assert _looks_like_login_page("  <!DOCTYPE html>")


def test_looks_like_login_page_detects_exception_redirect_markers():
    assert _looks_like_login_page("redirecting to exceptionIndex.action?exception=2")
    assert _looks_like_login_page("please loginEMA again")


def test_looks_like_login_page_false_for_json():
    assert not _looks_like_login_page('{"gridPower": "0", "loadPower": "869"}')
