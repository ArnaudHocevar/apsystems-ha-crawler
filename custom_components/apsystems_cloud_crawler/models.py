"""Data models for parsed APSystems Cloud Crawler coordinator data."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import homeassistant.util.dt as dt_util

_LOGGER = logging.getLogger(__name__)


@dataclass
class CloudCrawlerData:
    """Parsed snapshot combining the control-info and storage-summary endpoints."""

    # From getControlInfoWithMenuStorageDataPart
    last_report_time: datetime | None
    grid_power: float | None
    load_power: float | None
    pv_power: float | None
    storage_bat_power: float | None
    storage_soc: float | None
    storage_status: int | None
    storage_capacity: float | None
    abd: list[dict[str, Any]] = field(default_factory=list)
    line_map: dict[str, Any] = field(default_factory=dict)

    # From getStorageSummaryProductionInfoAjax (today's daily counters, kWh)
    battery_discharge_today: float | None = None
    battery_charge_today: float | None = None
    solar_production_today: float | None = None
    local_consumption_today: float | None = None
    grid_export_today: float | None = None
    grid_import_today: float | None = None

    # From getDashboardSummaryInfoAjax
    pv_lifetime_energy: float | None = None
    consume_lifetime_energy: float | None = None
    running_status: int | None = None
    communication_status: int | None = None

    # From getStrategyInfoWithMenu / getSystemStrategy. Field semantics here
    # are best-effort reverse-engineered guesses (no authoritative APsystems
    # documentation found) - see parse_strategy_info/parse_system_strategy.
    strategy_mode: int | None = None
    backup_reserve_soc: float | None = None
    eps_enabled: bool | None = None
    zero_export_enabled: bool | None = None
    peak_shaving_enabled: bool | None = None
    peak_shaving_threshold: float | None = None

    # From getGeneratorDataAjax / getGeneratorRealTimeAjax (only polled when
    # the enable_generator_sensors option is on). Field semantics are
    # best-effort guesses - see parse_generator_data/parse_generator_realtime.
    generator_power: float | None = None
    generator_voltage: float | None = None
    generator_frequency: float | None = None
    generator_temperature: float | None = None
    generator_status: int | None = None

    raw_control_info: dict[str, Any] = field(default_factory=dict)
    raw_storage_summary: dict[str, Any] = field(default_factory=dict)
    raw_dashboard_summary: dict[str, Any] = field(default_factory=dict)
    raw_strategy_info: dict[str, Any] = field(default_factory=dict)
    raw_system_strategy: dict[str, Any] = field(default_factory=dict)
    raw_generator_data: dict[str, Any] = field(default_factory=dict)
    raw_generator_realtime: dict[str, Any] = field(default_factory=dict)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_control_info(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse the getControlInfoWithMenuStorageDataPart payload into kwargs for CloudCrawlerData."""
    last_report_time = None
    raw_time = raw.get("lastReportTime")
    if raw_time:
        try:
            # Intentionally naive here: the portal gives no timezone of its own,
            # so we attach HA's configured local tz explicitly below instead of
            # assuming UTC/system tz at parse time.
            naive = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
            # The portal returns a naive "yyyy-MM-dd HH:mm:ss" wall-clock
            # string with no timezone info of its own. We interpret it as
            # being in Home Assistant's configured local timezone (the best
            # available assumption, since the portal doesn't tell us its
            # own timezone) and attach that via dt_util.as_local(), which
            # treats a naive datetime as already being in
            # dt_util.DEFAULT_TIME_ZONE rather than reinterpreting/shifting
            # it. A `timestamp` device_class sensor's native_value MUST be
            # a timezone-aware datetime; passing a naive one (or a string)
            # raises ValueError deep in HA's sensor state machinery.
            last_report_time = dt_util.as_local(naive)
        except ValueError:
            _LOGGER.debug("Could not parse lastReportTime %r", raw_time)

    line_map = raw.get("lineMap") or {}

    # ABD is a JSON-encoded string nested inside the outer JSON payload
    # (double encoding) - a list of per-battery-pack dicts. The account under
    # test only ever has a single entry; if more ever appear we just keep the
    # whole parsed list around and let sensors use the first one (see
    # coordinator.py), rather than hard-breaking on multi-station accounts.
    abd_raw = raw.get("ABD")
    abd: list[dict[str, Any]] = []
    if abd_raw:
        try:
            abd = json.loads(abd_raw)
        except (json.JSONDecodeError, TypeError):
            _LOGGER.debug("Could not parse ABD payload %r", abd_raw)

    return {
        "last_report_time": last_report_time,
        "grid_power": _to_float(raw.get("gridPower")),
        "load_power": _to_float(raw.get("loadPower")),
        "pv_power": _to_float(raw.get("pvPower")),
        "storage_bat_power": _to_float(raw.get("storageBatPower")),
        "storage_soc": _to_float(raw.get("storageSoc")),
        "storage_status": _to_int(raw.get("storageStatus")),
        "storage_capacity": _to_float(raw.get("storageCapacity")),
        "abd": abd,
        "line_map": line_map,
        "raw_control_info": raw,
    }


def parse_storage_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse the getStorageSummaryProductionInfoAjax payload into kwargs for CloudCrawlerData."""
    return {
        "battery_discharge_today": _to_float(raw.get("DE0")),
        "battery_charge_today": _to_float(raw.get("DE1")),
        "solar_production_today": _to_float(raw.get("DE2")),
        "local_consumption_today": _to_float(raw.get("DE3")),
        "grid_export_today": _to_float(raw.get("DE4")),
        "grid_import_today": _to_float(raw.get("DE5")),
        "raw_storage_summary": raw,
    }


def _to_bool_flag(value: Any) -> bool | None:
    """Parse a "1"/"0" style string/int flag into a bool, or None if absent."""
    if value is None:
        return None
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return None


def parse_dashboard_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse the getDashboardSummaryInfoAjax payload into kwargs for CloudCrawlerData."""
    return {
        "pv_lifetime_energy": _to_float(raw.get("pvLifetimeEnergy")),
        "consume_lifetime_energy": _to_float(raw.get("consumeLifetimeEnergy")),
        "running_status": _to_int(raw.get("runningStatus")),
        "communication_status": _to_int(raw.get("communicationStatus")),
        "raw_dashboard_summary": raw,
    }


def parse_strategy(
    strategy_info_raw: dict[str, Any], system_strategy_raw: dict[str, Any]
) -> dict[str, Any]:
    """Parse the two strategy endpoints into kwargs for CloudCrawlerData.

    NOTE: field semantics here are best-effort reverse-engineered guesses,
    not confirmed by official APsystems documentation:
      * strategy / config.mode: numeric operating-mode code.
      * config.backupSoc (getStrategyInfoWithMenu) / config.BUS
        (getSystemStrategy): battery reserve-for-backup percentage.
      * config.EPS: Emergency Power Supply (backup/UPS) enabled flag.
      * config.ZE: Zero-Export mode enabled flag.
      * config.PS / config.PSI.P: peak-shaving enabled flag + threshold (W).
    """
    info_config = strategy_info_raw.get("config") or {}
    sys_config = system_strategy_raw.get("config") or {}
    grid_tab_menu = strategy_info_raw.get("gridTabMenu") or {}

    strategy_mode = _to_int(
        system_strategy_raw.get("strategy") or strategy_info_raw.get("strategy")
    )

    backup_reserve_soc = _to_float(info_config.get("backupSoc"))
    if backup_reserve_soc is None:
        backup_reserve_soc = _to_float(sys_config.get("BUS"))

    peak_shaving_enabled = _to_bool_flag(info_config.get("PS"))
    peak_shaving_threshold = None
    if grid_tab_menu.get("peakShavingFlag"):
        psi = info_config.get("PSI") or {}
        peak_shaving_threshold = _to_float(psi.get("P"))

    return {
        "strategy_mode": strategy_mode,
        "backup_reserve_soc": backup_reserve_soc,
        "eps_enabled": _to_bool_flag(info_config.get("EPS")),
        "zero_export_enabled": _to_bool_flag(info_config.get("ZE")),
        "peak_shaving_enabled": peak_shaving_enabled,
        "peak_shaving_threshold": peak_shaving_threshold,
        "raw_strategy_info": strategy_info_raw,
        "raw_system_strategy": system_strategy_raw,
    }


def parse_generator(
    generator_data_raw: dict[str, Any], generator_realtime_raw: dict[str, Any]
) -> dict[str, Any]:
    """Parse the generator endpoints into kwargs for CloudCrawlerData.

    NOTE: field semantics here are best-effort reverse-engineered guesses,
    not confirmed by official APsystems documentation. On accounts without a
    physical generator attached, all fields read zero. Fields whose meaning
    is too ambiguous to expose as a dedicated entity (e.g. the `*R`-suffixed
    arrays, which look like [min, max] valid ranges rather than live
    readings) are intentionally left out of CloudCrawlerData and remain available via
    raw_generator_realtime for advanced/attribute use.
    """
    status = _to_int(
        generator_data_raw.get("work_status") or generator_data_raw.get("status")
    )
    return {
        "generator_power": _to_float(generator_realtime_raw.get("GP")),
        "generator_voltage": _to_float(generator_realtime_raw.get("GOV")),
        "generator_frequency": _to_float(generator_realtime_raw.get("GOF")),
        "generator_temperature": _to_float(generator_realtime_raw.get("GT")),
        "generator_status": status,
        "raw_generator_data": generator_data_raw,
        "raw_generator_realtime": generator_realtime_raw,
    }


def build_cloud_crawler_data(
    control_info_raw: dict[str, Any],
    storage_summary_raw: dict[str, Any],
    dashboard_summary_raw: dict[str, Any] | None = None,
    strategy_info_raw: dict[str, Any] | None = None,
    system_strategy_raw: dict[str, Any] | None = None,
    generator_data_raw: dict[str, Any] | None = None,
    generator_realtime_raw: dict[str, Any] | None = None,
) -> CloudCrawlerData:
    """Combine all raw payloads into a single CloudCrawlerData instance."""
    kwargs = parse_control_info(control_info_raw)
    kwargs.update(parse_storage_summary(storage_summary_raw))
    if dashboard_summary_raw is not None:
        kwargs.update(parse_dashboard_summary(dashboard_summary_raw))
    if strategy_info_raw is not None and system_strategy_raw is not None:
        kwargs.update(parse_strategy(strategy_info_raw, system_strategy_raw))
    if generator_data_raw is not None and generator_realtime_raw is not None:
        kwargs.update(parse_generator(generator_data_raw, generator_realtime_raw))
    return CloudCrawlerData(**kwargs)
