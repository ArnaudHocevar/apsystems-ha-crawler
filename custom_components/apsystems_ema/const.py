"""Constants for the APsystems EMA integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "apsystems_ema"

BASE_URL = "https://apsystemsema.com/ema"
INDEX_URL = f"{BASE_URL}/index.action"
LOGIN_URL = f"{BASE_URL}/loginEMA.action"
DASHBOARD_URL = f"{BASE_URL}/security/optmainmenu/intoHemsDashboard.action?language=en_US"

ENDPOINT_CONTROL_INFO = (
    f"{BASE_URL}/ajax/getDashboardApiAjax/getControlInfoWithMenuStorageDataPart"
)
ENDPOINT_STORAGE_SUMMARY = (
    f"{BASE_URL}/ajax/getDashboardApiAjax/getStorageSummaryProductionInfoAjax"
)
ENDPOINT_POWER_ON_CURRENT_DAY_BATCH = (
    f"{BASE_URL}/ajax/getDashboardApiAjax/getSystemPowerOnCurrentDayBatch"
)
ENDPOINT_DASHBOARD_SUMMARY = (
    f"{BASE_URL}/ajax/getDashboardApiAjax/getDashboardSummaryInfoAjax"
)
ENDPOINT_STRATEGY_INFO = (
    f"{BASE_URL}/ajax/getDashboardApiAjax/getStrategyInfoWithMenu"
)
ENDPOINT_SYSTEM_STRATEGY = f"{BASE_URL}/ajax/getDashboardApiAjax/getSystemStrategy"
ENDPOINT_GENERATOR_DATA = f"{BASE_URL}/ajax/getDashboardApiAjax/getGeneratorDataAjax"
ENDPOINT_GENERATOR_REALTIME = (
    f"{BASE_URL}/ajax/getDashboardApiAjax/getGeneratorRealTimeAjax"
)

CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30

# Battery-strategy and generator telemetry are user-configured settings /
# rarely-changing data, not live measurements - poll them far less often
# than the main 60s coordinator cycle to reduce load on the portal.
SLOW_POLL_INTERVAL = timedelta(minutes=5)

CONF_ENABLE_GENERATOR_SENSORS = "enable_generator_sensors"
DEFAULT_ENABLE_GENERATOR_SENSORS = False

DEFAULT_UPDATE_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL)

# Backfill: how many days back to look for gaps in external statistics on
# startup. The EMA portal's retention for past dates is unverified; the
# backfill loop stops as soon as a day returns no usable data.
BACKFILL_MAX_DAYS = 7

# Manual backfill service (apsystems_ema.backfill): a hard safety cap on how
# many days a single service call may request, to avoid an accidental
# multi-year range hammering the portal. Per-day requests are additionally
# paced (see MANUAL_BACKFILL_PACE_SECONDS) regardless of range length.
MANUAL_BACKFILL_MAX_DAYS = 400
MANUAL_BACKFILL_PACE_SECONDS = 1.0

# Defensive sanity bound: no single 5-minute interval delta from
# getSystemPowerOnCurrentDayBatch should plausibly exceed this many kWh for
# the small residential/C&I behind-the-meter systems this integration
# targets (equivalent to a sustained ~240 kW for 5 minutes). This exists
# purely as a defense-in-depth guard against a malformed/glitched portal
# response (e.g. a stray non-delta value) silently corrupting the Energy
# dashboard with an implausible spike; any interval exceeding it is dropped
# (treated as 0 for that interval) and logged, rather than trusted.
MAX_PLAUSIBLE_INTERVAL_KWH = 20.0

MANUFACTURER = "APsystems"
MODEL = "EMA"

# Daily energy counter keys from getStorageSummaryProductionInfoAjax, and the
# matching statistic_id suffix / friendly name used for sensors + external
# statistics.
DAILY_ENERGY_SENSORS: dict[str, dict[str, str]] = {
    "DE0": {"key": "battery_discharge_today", "name": "Battery Discharge Today"},
    "DE1": {"key": "battery_charge_today", "name": "Battery Charge Today"},
    "DE2": {"key": "solar_production_today", "name": "Solar Production Today"},
    "DE3": {"key": "local_consumption_today", "name": "Local Consumption Today"},
    "DE4": {"key": "grid_export_today", "name": "Grid Export Today"},
    "DE5": {"key": "grid_import_today", "name": "Grid Import Today"},
}

# Maps a DAILY_ENERGY_SENSORS key to the corresponding scalar daily-total
# field name and the per-interval array field name in
# getSystemPowerOnCurrentDayBatch, used for statistics backfill.
DAILY_ENERGY_BATCH_FIELDS: dict[str, dict[str, str]] = {
    "DE0": {"total": "dischargeTotal", "series": "dischargeEnergy"},
    "DE1": {"total": "chargeTotal", "series": "chargeEnergy"},
    "DE2": {"total": "producedTotal", "series": "producedEnergy"},
    "DE3": {"total": "consumedTotal", "series": "consumedEnergy"},
    "DE4": {"total": "exportedTotal", "series": "exportedEnergy"},
    "DE5": {"total": "importedTotal", "series": "importedEnergy"},
}
