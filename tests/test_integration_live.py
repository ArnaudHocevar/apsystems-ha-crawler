"""Manual, real-site end-to-end validation for the APsystems EMA integration.

This test is NOT run as part of normal CI/test runs - it is marked
``@pytest.mark.integration`` and additionally skipped unless the
``RUN_APSYSTEMS_INTEGRATION_TEST`` environment variable is set, because it
makes real network calls to the APsystems EMA portal with real credentials.

Run it manually with:

    RUN_APSYSTEMS_INTEGRATION_TEST=1 pytest tests/test_integration_live.py -v -s

The credentials below are a test/development account provided for this
project only. A real end user must supply their OWN username/password via
the integration's Config Flow; this script/test is for manual validation
during development only and must never be used as a template for storing
real user credentials in source control.
"""
from __future__ import annotations

import os

import aiohttp
import pytest

from custom_components.apsystems_ema.api import ApsystemsEmaClient

TEST_USERNAME = "Hocevar arnaud"
TEST_PASSWORD = "f$SMS6QL*oYqrP"

pytestmark = pytest.mark.integration

requires_live_env = pytest.mark.skipif(
    not os.environ.get("RUN_APSYSTEMS_INTEGRATION_TEST"),
    reason=(
        "Set RUN_APSYSTEMS_INTEGRATION_TEST=1 to run this manual, "
        "real-network integration test."
    ),
)


@requires_live_env
async def test_live_login_and_endpoints():
    async with aiohttp.ClientSession() as session:
        client = ApsystemsEmaClient(session, TEST_USERNAME, TEST_PASSWORD)

        await client.async_login()

        control_info = await client.async_get_control_info()
        assert "lastReportTime" in control_info
        print("control_info:", control_info)

        storage_summary = await client.async_get_storage_summary()
        assert "DE0" in storage_summary
        print("storage_summary:", storage_summary)

        dashboard_summary = await client.async_get_dashboard_summary()
        assert "pvLifetimeEnergy" in dashboard_summary
        print("dashboard_summary:", dashboard_summary)

        strategy_info = await client.async_get_strategy_info()
        print("strategy_info:", strategy_info)

        system_strategy = await client.async_get_system_strategy()
        print("system_strategy:", system_strategy)

        import json as _json

        abd = _json.loads(control_info.get("ABD") or "[]")
        if abd:
            ecu_dev_id = abd[0]["ABID"]
            generator_data = await client.async_get_generator_data(ecu_dev_id)
            print("generator_data:", generator_data)
            generator_realtime = await client.async_get_generator_realtime(ecu_dev_id)
            print("generator_realtime:", generator_realtime)

        from datetime import datetime

        today_str = datetime.now().strftime("%Y%m%d")
        batch = await client.async_get_power_on_current_day_batch(today_str)
        print("power_on_current_day_batch keys:", list(batch.keys()) if batch else None)
