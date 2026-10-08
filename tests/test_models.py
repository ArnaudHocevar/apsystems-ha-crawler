"""Unit tests for parsing the example JSON payloads into CloudCrawlerData."""
from __future__ import annotations

import json

from custom_components.apsystems_cloud_crawler.models import (
    build_cloud_crawler_data,
    parse_control_info,
    parse_dashboard_summary,
    parse_generator,
    parse_storage_summary,
    parse_strategy,
)

CONTROL_INFO_EXAMPLE = {
    "lastReportTime": "2026-10-06 21:47:52",
    "gridPower": "0",
    "loadPower": "869",
    "lineMap": {"A": 0, "B": 0, "C": 0, "D": 0, "E": 0, "F": 1, "G": 0},
    "ABD": '[{"ABID":"B05000001420","GES":"0","SM":"10","GS":"0","IMS":"0"}]',
    "storageSoc": "66",
    "storageStatus": "1",
    "storageCapacity": "20.480",
    "storageBatPower": "-869",
    "pvPower": "0",
}

STORAGE_SUMMARY_EXAMPLE = {
    "P0": "864.000",
    "P1": "0.000",
    "P2": "0.000",
    "P3": "864.000",
    "P4": "0.000",
    "P5": "0.000",
    "CO2": "2236.978870",
    "tree": "83.017270",
    "storageCapacity": "20.480",
    "onlineNum": 1,
    "DE0": "8.509",
    "DE2": "38.956",
    "DE1": "19.021",
    "DE4": "5.647",
    "SSOC": "66.000",
    "DE3": "30.691",
    "T": "21:45:00",
    "DE5": "7.895",
    "totalNum": 1,
    "coal": "897.484000",
    "T2": "2243.710",
    "T3": "1392.699",
}

DASHBOARD_SUMMARY_EXAMPLE = {
    "pvTodayEnergy": "38.956",
    "pvLifetimeEnergy": "2243.710",
    "communicationStatus": 0,
    "runningStatus": 1,
    "consumeTodayEnergy": "30.907",
    "co2": "2236.978870",
    "tree": "83.017270",
    "coal": "897.484000",
    "consumeLifetimeEnergy": "1392.915",
}

STRATEGY_INFO_EXAMPLE = {
    "timeSharingVersion": "1",
    "gridTabMenu": {
        "zeroExportVersion": "1",
        "priceFlag": True,
        "relayControlFlag": False,
        "peakShavingVersion": "1",
        "zeroExportFlag": True,
        "threePhaseBalanceFlag": False,
        "pvSystemType": 0,
        "modeList": [],
        "peakShavingFlag": True,
    },
    "storageTabMenu": {"batCapVersion": "1", "batCapFlag": True, "epsFlag": True},
    "aiVersion": "1",
    "pvTabMenu": {"maxPowerFlag": False},
    "strategy": "2",
    "gridFlag": 1,
    "config": {
        "mode": "2",
        "PS": "0",
        "PSI": {"P": "3000", "PST": ["00002400"]},
        "EPS": "1",
        "ZE": "0",
        "backupSoc": "20",
    },
    "esMenu": True,
    "strategyList": ["2", "3", "4"],
}

SYSTEM_STRATEGY_EXAMPLE = {
    "modbus": "0",
    "aiVersion": "1",
    "strategy": "2",
    "config": {
        "mode": "2",
        "ECO": "1",
        "currentLevel": "0",
        "BUS": "20",
        "current": "100",
        "PSI": {"P": "3000"},
        "ZE": "0",
        "ZEI": {"P": "100"},
    },
    "deviceId": "B05000001420",
}

GENERATOR_DATA_EXAMPLE = {
    "work_status": "0",
    "ECU_NO": "B05000001420",
    "status": "0",
}

GENERATOR_REALTIME_EXAMPLE = {
    "PP": "0",
    "QT": [],
    "GTR": ["20", "100"],
    "GOVR": ["176", "264", "0.1"],
    "GWT": "0",
    "GM": "0",
    "GP": "0",
    "GUV": "187.0",
    "GST": "0",
    "GPT": "0",
    "GCET": "0",
    "GUVR": ["176", "264", "0.1"],
    "GT": "0",
    "GOT": "0",
    "GOV": "253.0",
    "PPR": ["0", "12000"],
    "GOFR": ["45", "64", "0.0"],
    "GUF": "47.50",
    "GCT": "0",
    "GUFR": ["45", "64", "0.0"],
    "work_status": "0",
    "GCST": "0",
    "status": "0",
    "GOF": "50.50",
}


def test_parse_control_info():
    parsed = parse_control_info(CONTROL_INFO_EXAMPLE)
    assert parsed["grid_power"] == 0.0
    assert parsed["load_power"] == 869.0
    assert parsed["pv_power"] == 0.0
    assert parsed["storage_bat_power"] == -869.0
    assert parsed["storage_soc"] == 66.0
    assert parsed["storage_status"] == 1
    assert parsed["storage_capacity"] == 20.480
    last_report_time = parsed["last_report_time"]
    assert last_report_time.tzinfo is not None
    assert last_report_time.strftime("%Y-%m-%dT%H:%M:%S") == "2026-10-06T21:47:52"
    # ABD is double-JSON-encoded: a JSON string nested inside the outer JSON.
    assert parsed["abd"] == json.loads(CONTROL_INFO_EXAMPLE["ABD"])
    assert parsed["abd"][0]["ABID"] == "B05000001420"
    assert parsed["line_map"] == CONTROL_INFO_EXAMPLE["lineMap"]


def test_parse_control_info_handles_missing_abd():
    raw = dict(CONTROL_INFO_EXAMPLE)
    del raw["ABD"]
    parsed = parse_control_info(raw)
    assert parsed["abd"] == []


def test_parse_control_info_last_report_time_is_timezone_aware():
    """Regression test for a real HA bug: a `timestamp` device_class sensor's
    native_value must be a timezone-aware datetime. The portal's
    'lastReportTime' field ('yyyy-MM-dd HH:mm:ss') is naive wall-clock time
    with no timezone of its own; parsing it must not simply return the
    naive datetime.strptime() result (that raised
    `ValueError: ... missing timezone information` deep inside HA's sensor
    state machinery when HA tried to stringify the state)."""
    raw = dict(CONTROL_INFO_EXAMPLE)
    raw["lastReportTime"] = "2026-10-06 23:01:43"
    parsed = parse_control_info(raw)
    last_report_time = parsed["last_report_time"]
    assert last_report_time is not None
    assert last_report_time.tzinfo is not None
    # Wall-clock components must be preserved (only tzinfo is attached, the
    # naive value is not shifted/reinterpreted as a different zone).
    assert last_report_time.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-06 23:01:43"


def test_parse_storage_summary():
    parsed = parse_storage_summary(STORAGE_SUMMARY_EXAMPLE)
    assert parsed["battery_discharge_today"] == 8.509
    assert parsed["battery_charge_today"] == 19.021
    assert parsed["solar_production_today"] == 38.956
    assert parsed["local_consumption_today"] == 30.691
    assert parsed["grid_export_today"] == 5.647
    assert parsed["grid_import_today"] == 7.895


def test_parse_dashboard_summary():
    parsed = parse_dashboard_summary(DASHBOARD_SUMMARY_EXAMPLE)
    assert parsed["pv_lifetime_energy"] == 2243.710
    assert parsed["consume_lifetime_energy"] == 1392.915
    assert parsed["running_status"] == 1
    assert parsed["communication_status"] == 0


def test_parse_strategy():
    parsed = parse_strategy(STRATEGY_INFO_EXAMPLE, SYSTEM_STRATEGY_EXAMPLE)
    assert parsed["strategy_mode"] == 2
    assert parsed["backup_reserve_soc"] == 20.0
    assert parsed["eps_enabled"] is True
    assert parsed["zero_export_enabled"] is False
    assert parsed["peak_shaving_enabled"] is False
    # peakShavingFlag capability is True, so the threshold should be surfaced
    # even though PS itself (enabled) is "0" in this example.
    assert parsed["peak_shaving_threshold"] == 3000.0


def test_parse_strategy_backup_soc_falls_back_to_bus():
    info = dict(STRATEGY_INFO_EXAMPLE)
    info["config"] = dict(info["config"])
    del info["config"]["backupSoc"]
    parsed = parse_strategy(info, SYSTEM_STRATEGY_EXAMPLE)
    assert parsed["backup_reserve_soc"] == 20.0  # from system_strategy's BUS


def test_parse_generator_all_zero_when_no_generator_attached():
    parsed = parse_generator(GENERATOR_DATA_EXAMPLE, GENERATOR_REALTIME_EXAMPLE)
    assert parsed["generator_power"] == 0.0
    assert parsed["generator_voltage"] == 253.0
    assert parsed["generator_frequency"] == 50.50
    assert parsed["generator_temperature"] == 0.0
    assert parsed["generator_status"] == 0
    assert parsed["raw_generator_realtime"] == GENERATOR_REALTIME_EXAMPLE


def test_build_cloud_crawler_data_combines_all_sources():
    data = build_cloud_crawler_data(
        CONTROL_INFO_EXAMPLE,
        STORAGE_SUMMARY_EXAMPLE,
        DASHBOARD_SUMMARY_EXAMPLE,
        STRATEGY_INFO_EXAMPLE,
        SYSTEM_STRATEGY_EXAMPLE,
        GENERATOR_DATA_EXAMPLE,
        GENERATOR_REALTIME_EXAMPLE,
    )
    assert data.grid_power == 0.0
    assert data.load_power == 869.0
    assert data.battery_discharge_today == 8.509
    assert data.pv_lifetime_energy == 2243.710
    assert data.strategy_mode == 2
    assert data.eps_enabled is True
    assert data.generator_voltage == 253.0
    assert data.raw_control_info == CONTROL_INFO_EXAMPLE


def test_build_cloud_crawler_data_without_optional_sources():
    data = build_cloud_crawler_data(CONTROL_INFO_EXAMPLE, STORAGE_SUMMARY_EXAMPLE)
    assert data.grid_power == 0.0
    assert data.pv_lifetime_energy is None
    assert data.strategy_mode is None
    assert data.generator_power is None
