"""Async API client for the APsystems EMA portal.

Implements the login handshake (see crypto.py for the RSA/AES details) and
the three data endpoints used by this integration, using aiohttp so it plays
nicely with Home Assistant's shared client session and event loop.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from http.cookies import SimpleCookie
from typing import Any

import aiohttp
import yarl

from .const import (
    DEFAULT_BASE_URL,
    PATH_CONTROL_INFO,
    PATH_DASHBOARD,
    PATH_DASHBOARD_SUMMARY,
    PATH_GENERATOR_DATA,
    PATH_GENERATOR_REALTIME,
    PATH_INDEX,
    PATH_LOGIN,
    PATH_POWER_ON_CURRENT_DAY_BATCH,
    PATH_STORAGE_SUMMARY,
    PATH_STRATEGY_INFO,
    PATH_SYSTEM_STRATEGY,
)
from .crypto import build_login_payload

_LOGGER = logging.getLogger(__name__)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class ApsystemsCloudCrawlerError(Exception):
    """Base error for the APSystems Cloud Crawler client."""


class ApsystemsCloudCrawlerAuthError(ApsystemsCloudCrawlerError):
    """Raised when login fails because of invalid credentials."""


class ApsystemsCloudCrawlerConnectionError(ApsystemsCloudCrawlerError):
    """Raised when the EMA portal cannot be reached."""


def _looks_like_login_page(text: str) -> bool:
    """Return True if an authenticated-endpoint response looks like the login/HTML page."""
    stripped = text.lstrip()
    if stripped.startswith("<"):
        return True
    lowered = text.lower()
    return "exceptionindex" in lowered or "loginema" in lowered


def _extract_reissued_jsessionid(set_cookie_headers: list[str]) -> str | None:
    """Return the last non-"deleteMe" JSESSIONID value from raw Set-Cookie headers.

    See the long comment in ``async_login`` for why this is necessary: the
    portal issues JSESSIONID three times per index-page response (valid,
    then an invalidating ``deleteMe``, then a re-issued valid value), and
    aiohttp's CookieJar can end up expiring the final reissued value due to
    a stale expiration scheduled for the earlier ``deleteMe`` entry.
    """
    values = re.findall(r"JSESSIONID=([^;]+)", "\n".join(set_cookie_headers))
    real_values = [v for v in values if v != "deleteMe"]
    return real_values[-1] if real_values else None


class ApsystemsCloudCrawlerClient:
    """Thin async client handling login + the three polled endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._logged_in = False

        # Everything below is derived from base_url so this client can talk
        # to any APsystems cloud deployment that mirrors the stock EMA
        # dashboard API (e.g. a region-specific or white-labelled portal),
        # not just the default apsystemsema.com host.
        self._base_url = base_url.rstrip("/")
        parsed_base = yarl.URL(self._base_url)
        self._origin = f"{parsed_base.scheme}://{parsed_base.host}"
        self._cookie_path = parsed_base.path or "/"

        self._index_url = f"{self._base_url}/{PATH_INDEX}"
        self._login_url = f"{self._base_url}/{PATH_LOGIN}"
        self._dashboard_url = f"{self._base_url}/{PATH_DASHBOARD}"

        self._endpoint_control_info = f"{self._base_url}/{PATH_CONTROL_INFO}"
        self._endpoint_storage_summary = f"{self._base_url}/{PATH_STORAGE_SUMMARY}"
        self._endpoint_power_on_current_day_batch = (
            f"{self._base_url}/{PATH_POWER_ON_CURRENT_DAY_BATCH}"
        )
        self._endpoint_dashboard_summary = f"{self._base_url}/{PATH_DASHBOARD_SUMMARY}"
        self._endpoint_strategy_info = f"{self._base_url}/{PATH_STRATEGY_INFO}"
        self._endpoint_system_strategy = f"{self._base_url}/{PATH_SYSTEM_STRATEGY}"
        self._endpoint_generator_data = f"{self._base_url}/{PATH_GENERATOR_DATA}"
        self._endpoint_generator_realtime = (
            f"{self._base_url}/{PATH_GENERATOR_REALTIME}"
        )

        self._ajax_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self._dashboard_url,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": BROWSER_USER_AGENT,
        }

    async def async_login(self) -> None:
        """Perform the full login handshake, raising on failure."""
        # Step 1: GET the index page with a browser UA to obtain a JSESSIONID.
        try:
            async with self._session.get(
                self._index_url,
                headers={"User-Agent": BROWSER_USER_AGENT, "Accept": "text/html"},
            ) as resp:
                await resp.read()
                set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                response_url = resp.url
        except aiohttp.ClientError as err:
            raise ApsystemsCloudCrawlerConnectionError(
                f"Could not reach EMA index page: {err}"
            ) from err

        # Workaround for an aiohttp CookieJar quirk: the server emits JSESSIONID
        # three times in one response (a fresh value, then
        # "JSESSIONID=deleteMe; Max-Age=0" to invalidate it, then a re-issued
        # fresh value). aiohttp's CookieJar schedules an expiration for the
        # cookie *name* when it sees the Max-Age=0 entry but never clears that
        # scheduled expiration when the same name is set again afterwards
        # without its own Max-Age/Expires, so the final (correct) JSESSIONID
        # gets silently expired out of the jar a few milliseconds later,
        # leaving the login POST with no session cookie attached at all
        # (manifesting as a generic "wrong credentials" exceptionIndex
        # redirect). Re-extract the last non-"deleteMe" JSESSIONID value from
        # the raw Set-Cookie headers and re-inject it into the jar so it is
        # not subject to that stale expiration entry.
        real_value = _extract_reissued_jsessionid(set_cookie_headers)
        if real_value:
            fixed = SimpleCookie()
            fixed["JSESSIONID"] = real_value
            fixed["JSESSIONID"]["path"] = self._cookie_path
            self._session.cookie_jar.update_cookies(fixed, response_url)

        # The portal expects the current *local* wall-clock time; .astimezone()
        # with no argument attaches the system's local tz without changing the
        # wall-clock value, which also satisfies linters that flag naive now().
        today = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        body, content_type = build_login_payload(self._username, self._password, today)

        try:
            async with self._session.post(
                self._login_url,
                data=body.encode("ascii"),
                headers={
                    "Content-Type": content_type,
                    "Origin": self._origin,
                    "Referer": self._index_url,
                    "User-Agent": BROWSER_USER_AGENT,
                },
                allow_redirects=True,
            ) as resp:
                final_url = str(resp.url)
                text = await resp.text()
        except aiohttp.ClientError as err:
            raise ApsystemsCloudCrawlerConnectionError(
                f"Could not reach EMA login endpoint: {err}"
            ) from err

        if "exceptionindex" in final_url.lower() or "exception" in final_url.lower():
            raise ApsystemsCloudCrawlerAuthError(
                _extract_error_text(text) or "Login failed (redirected to exception page)"
            )
        if "intohemsdashboard" not in final_url.lower():
            raise ApsystemsCloudCrawlerAuthError(
                _extract_error_text(text)
                or f"Login failed: unexpected redirect to {final_url}"
            )

        self._logged_in = True
        _LOGGER.debug("APSystems Cloud Crawler login succeeded")

    async def _post_ajax(self, url: str, data: dict[str, str] | None = None) -> str:
        """POST to an authenticated ajax endpoint, re-logging in once if the session expired."""
        for attempt in range(2):
            try:
                async with self._session.post(
                    url, data=data or {}, headers=self._ajax_headers
                ) as resp:
                    text = await resp.text()
            except aiohttp.ClientError as err:
                raise ApsystemsCloudCrawlerConnectionError(f"Could not reach {url}: {err}") from err

            if _looks_like_login_page(text) or not self._logged_in:
                if attempt == 0:
                    _LOGGER.debug("Session expired or not authenticated, re-logging in")
                    await self.async_login()
                    continue
                raise ApsystemsCloudCrawlerAuthError("Session expired and re-login failed")
            return text
        raise ApsystemsCloudCrawlerAuthError("Session expired and re-login failed")

    async def async_get_control_info(self) -> dict[str, Any]:
        """Fetch the live instantaneous dashboard values (endpoint #1)."""
        text = await self._post_ajax(self._endpoint_control_info)
        return _parse_json(text, self._endpoint_control_info)

    async def async_get_storage_summary(self) -> dict[str, Any]:
        """Fetch today's daily summary values (endpoint #2)."""
        text = await self._post_ajax(
            self._endpoint_storage_summary, data={"isMultipleStorage": "true"}
        )
        return _parse_json(text, self._endpoint_storage_summary)

    def _get_user_id_cookie(self) -> str | None:
        """Return the ``userId`` cookie value set by the server at login, if any."""
        for cookie in self._session.cookie_jar.filter_cookies(yarl.URL(self._base_url)).values():
            if cookie.key == "userId":
                return cookie.value
        return None

    async def async_get_dashboard_summary(self) -> dict[str, Any]:
        """Fetch lifetime production/consumption + status values (summary endpoint)."""
        user_id = self._get_user_id_cookie()
        data = {"userId": user_id, "operateId": user_id} if user_id else {}
        text = await self._post_ajax(self._endpoint_dashboard_summary, data=data)
        return _parse_json(text, self._endpoint_dashboard_summary)

    async def async_get_strategy_info(self) -> dict[str, Any]:
        """Fetch battery/grid strategy capability+config info (slow-polled)."""
        text = await self._post_ajax(self._endpoint_strategy_info)
        return _parse_json(text, self._endpoint_strategy_info)

    async def async_get_system_strategy(self) -> dict[str, Any]:
        """Fetch the active battery/grid strategy config (slow-polled)."""
        text = await self._post_ajax(self._endpoint_system_strategy)
        return _parse_json(text, self._endpoint_system_strategy)

    async def async_get_generator_data(self, ecu_dev_id: str) -> dict[str, Any]:
        """Fetch generator work status for ``ecu_dev_id`` (the ABID device id)."""
        text = await self._post_ajax(
            self._endpoint_generator_data, data={"ecuDevId": ecu_dev_id}
        )
        return _parse_json(text, self._endpoint_generator_data)

    async def async_get_generator_realtime(self, ecu_dev_id: str) -> dict[str, Any]:
        """Fetch generator realtime telemetry for ``ecu_dev_id`` (the ABID device id)."""
        text = await self._post_ajax(
            self._endpoint_generator_realtime, data={"ecuDevId": ecu_dev_id}
        )
        return _parse_json(text, self._endpoint_generator_realtime)

    async def async_get_power_on_current_day_batch(self, day: str) -> dict[str, Any] | None:
        """Fetch the 5-minute resolution time series for ``day`` (format yyyyMMdd).

        Returns ``None`` if the response is not usable JSON (e.g. the date is
        outside the portal's retention window), so callers can stop a
        backward backfill loop gracefully instead of raising.

        Note on array semantics (confirmed against the live portal): the
        ``energy``/``chargeEnergy``/``dischargeEnergy``/``producedEnergy``/
        ``consumedEnergy``/``importedEnergy``/``exportedEnergy`` arrays are
        **per-5-minute-interval deltas**, not running cumulative totals --
        trailing night-time ``producedEnergy`` entries are ``0.000`` while
        the scalar ``producedTotal`` for the day stays at its accumulated
        value, and summing all entries approximately equals the matching
        ``*Total`` scalar. This matters for backfilling external statistics,
        which expect a running ``sum`` rather than a per-point delta.
        """
        text = await self._post_ajax(
            self._endpoint_power_on_current_day_batch, data={"date": day}
        )
        try:
            return _parse_json(text, self._endpoint_power_on_current_day_batch)
        except ApsystemsCloudCrawlerError:
            _LOGGER.debug("No usable data for day %s (likely outside retention)", day)
            return None


def _parse_json(text: str, source: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError) as err:
        raise ApsystemsCloudCrawlerError(
            f"Unexpected non-JSON response from {source}: {err}"
        ) from err


def _extract_error_text(html: str) -> str | None:
    """Best-effort extraction of a human readable error message from the EMA error HTML."""
    match = re.search(r"<div[^>]*class=\"[^\"]*error[^\"]*\"[^>]*>(.*?)</div>", html, re.S | re.I)
    if match:
        return re.sub("<[^>]+>", "", match.group(1)).strip()
    return None
