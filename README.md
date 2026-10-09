# APSystems Cloud Crawler for Home Assistant

A [HACS](https://hacs.xyz/)-compatible custom integration that polls the
[APsystems EMA portal](https://apsystemsema.com/ema) (the cloud dashboard for
APsystems hybrid inverter / battery storage systems) and exposes solar,
battery, grid and (optionally) generator metrics as Home Assistant sensors,
including native support for the Energy dashboard.

There is no official APsystems local or cloud API; this integration talks to
the same AJAX endpoints the EMA web dashboard itself uses, via a
reverse-engineered login handshake (the portal encrypts credentials
client-side with RSA-wrapped AES before submitting the login form). No
browser/Selenium is used — it's plain async HTTP via `aiohttp`.

> **Disclaimer**: this is an unofficial, community-reverse-engineered
> integration, not affiliated with or endorsed by APsystems. The EMA portal
> is not a documented public API and could change at any time, which may
> break this integration without notice. Several field meanings (see
> "Known limitations / unconfirmed fields" below) are best-effort guesses
> based on observed values, not official documentation.

## What it does

* Logs in to the EMA portal using your account's username/password (entered
  once via the Config Flow; never hard-coded).
* Polls live instantaneous power/battery/grid values every `scan_interval`
  seconds (default 60s, minimum 30s).
* Polls today's daily energy counters (solar, battery charge/discharge, grid
  import/export, local consumption) on the same cadence.
* Polls lifetime energy counters, battery/grid strategy settings, and
  (optionally) generator telemetry on a slower 5-minute cadence, since those
  change rarely.
* Transparently re-authenticates if the portal session expires.
* On first install (and after any Home Assistant downtime), backfills
  hourly-resolution history for the six daily energy counters into Home
  Assistant's recorder as **external statistics**, so the Energy
  dashboard / history graphs don't start with a flat, empty day.

## Installation

### HACS (custom repository)

1. In HACS, go to **Integrations → ⋮ → Custom repositories**.
2. Add this repository's URL, category **Integration**.
3. Search for "APSystems Cloud Crawler" in HACS and install it.
4. Restart Home Assistant.

### Manual

1. Copy the `custom_components/apsystems_cloud_crawler` folder from this repository
   into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

Configuration is done entirely through the UI (**Settings → Devices &
Services → Add Integration → APSystems Cloud Crawler**) — there is no YAML
configuration.

* **Username** / **Password** — your EMA portal login credentials (the same
  ones you use at https://apsystemsema.com/ema). The integration performs a
  real login during setup to validate them; invalid credentials or
  connection problems are reported back as clear config-flow errors.
* **APsystems cloud portal URL** (optional, advanced) — defaults to the
  stock EMA portal (`https://apsystemsema.com/ema`). Override this only if
  your account is served by a different APsystems cloud deployment (e.g. a
  region-specific or white-labelled portal) that mirrors the same dashboard
  API; most users should leave it at the default.

Credentials are stored in the config entry (Home Assistant's standard
encrypted storage), the same as any other cloud-polling integration.

### Options

After setup, click **Configure** on the integration to adjust:

* **Scan interval** (seconds) — how often to poll live power/battery/grid
  values and the daily energy counters. Default `60`, minimum `30` (the
  portal's own dashboard polls roughly this often; lower values are rejected
  to avoid hammering the portal).
* **Enable generator sensors** — off by default. Turn on only if your system
  has a generator and you want its telemetry exposed. Generator field
  semantics are the least confirmed of everything this integration exposes
  (see limitations below), so it's opt-in.

## Entities

One device ("station") is created per config entry, matching the single
inverter/storage system associated with the account under test. If an
account ever has multiple battery packs or inverters, only the first one is
used (not expected for typical residential installs).

### Sensors — live values (default ~60s poll)

| Entity | Unit | Device class / state class | Notes |
|---|---|---|---|
| Grid Power | W | power / measurement | Positive/negative sign per portal convention |
| Load Power | W | power / measurement | Whole-house consumption |
| Solar Power | W | power / measurement | |
| Battery Power | W | power / measurement | Negative = discharging, positive = charging (as observed) |
| Battery State of Charge | % | battery / measurement | |
| Battery Status Code | — | measurement | Raw numeric code passed through as-is; meaning of values like 0/1/2 is **not confirmed** — do not assume a fixed mapping |
| Battery Capacity | kWh | energy_storage / measurement | Mostly static, installed capacity |
| Last Report Time | — | timestamp | Portal's own "last updated" timestamp, parsed from local time to HA's timezone |

### Sensors — daily energy counters (default ~60s poll, Energy-dashboard compatible)

| Entity | Unit | Device class / state class |
|---|---|---|
| Battery Discharge Today | kWh | energy / total_increasing |
| Battery Charge Today | kWh | energy / total_increasing |
| Solar Production Today | kWh | energy / total_increasing |
| Local Consumption Today | kWh | energy / total_increasing |
| Grid Export Today | kWh | energy / total_increasing |
| Grid Import Today | kWh | energy / total_increasing |

These are daily-resetting counters; Home Assistant's recorder/Energy
dashboard natively understands `total_increasing` sensors that reset to 0
when the portal starts a new accumulation period. **This integration never
adds these sensors to your Energy dashboard configuration for you** — add
whichever ones you want yourself under **Settings → Dashboards → Energy**.

> **Prefer the "Lifetime" companion sensors below for your Energy
> dashboard sources.** A sensor that never resets avoids a subtle failure
> mode of these daily ones: if Home Assistant samples a counter right
> before the portal's own day rollover lands, the Energy dashboard's
> day-boundary value can be yesterday's stale peak instead of the new
> day's near-zero baseline, skewing that day's figures. See "Daily
> rollover diagnostics" below for the full explanation, and "Sensors —
> lifetime" for what each counter's never-resetting equivalent is called.

#### Daily rollover diagnostics

Home Assistant's `total_increasing` state class is designed to handle a
counter decreasing: when a lower reading is seen, the recorder starts a
fresh accumulation baseline from that point instead of treating it as a
drop in usage. That is exactly what should happen when the portal rolls
over to a new day (flush to 0, or near it), so **this integration does
not withhold, rewrite, or otherwise filter any of these six values** —
whatever the portal reports is what gets published, every poll.

What this integration does add is diagnostic logging. The portal's own
day boundary is not guaranteed to land on Home Assistant's local midnight
(e.g. the portal may roll over on a UTC day boundary while HA runs in
CET), and it has occasionally been observed to report a transient,
implausibly low value for one of these counters on a single poll before
resuming at its previous level on the very next poll. Both cases look the
same at a glance — a counter going down — so whenever a decrease is
detected, a `WARNING` is logged with enough context to tell them apart:
the live power readings for that poll (grid/load/PV/battery power,
battery state of charge), the previous and new value of all six
counters, and the portal's own self-reported `lastReportTime` (used, not
Home Assistant's local date, so detection stays correct regardless of any
timezone offset between the portal and this HA instance). On a
best-effort basis the log is also annotated with how many 5-minute
interval data points the portal's current-day series has so far via
`getSystemPowerOnCurrentDayBatch` — a day that has genuinely just started
has few or none, while a day that is already well underway (the expected
state during a transient glitch) has many — purely as a clue for
whoever reads the log, never used to alter what gets published.

A decrease that flushes to (near) zero is logged as consistent with a
genuine rollover; a decrease that doesn't is logged more loudly as a
possible data inconsistency worth investigating. Check the logs
(`custom_components.apsystems_cloud_crawler.daily_energy_monitor`) if the
Energy dashboard ever looks skewed around day boundaries.

### Sensors — lifetime / slow-poll (default ~5 min poll)

| Entity | Unit | Device class / state class | Source |
|---|---|---|---|
| Solar Production Lifetime | kWh | energy / total_increasing | Native portal field (`pvLifetimeEnergy`) |
| Consumption Lifetime | kWh | energy / total_increasing | Native portal field (`consumeLifetimeEnergy`) |
| Battery Discharge Lifetime | kWh | energy / total_increasing | Synthesized (see below) |
| Battery Charge Lifetime | kWh | energy / total_increasing | Synthesized (see below) |
| Grid Export Lifetime | kWh | energy / total_increasing | Synthesized (see below) |
| Grid Import Lifetime | kWh | energy / total_increasing | Synthesized (see below) |
| Battery Strategy Mode | — | diagnostic, raw code (unconfirmed meaning) | |
| Backup Reserve SOC | % | diagnostic | |
| Peak Shaving Threshold | W | diagnostic | |

The portal only reports a genuine, never-resetting lifetime total for
solar production and consumption. For the other four daily counters
(battery charge/discharge, grid import/export) this integration
reconstructs an equivalent itself: each ~60s poll, it adds the counter's
positive delta since the last poll onto a running total that is restored
across Home Assistant restarts and never reset. A decrease is never
simply subtracted — it is checked against the portal's own self-reported
`lastReportTime` date (the same signal used by the diagnostics above): if
that date advanced, the drop is treated as a genuine rollover and the new
day's progress-so-far is folded in; if it didn't, the drop is held as a
suspected glitch (so a same-poll recovery isn't double-counted) and only
force-accepted as a new baseline if it persists for more than 15 minutes
straight. This logic only ever adds to its own independent sensors — it
never reads back or alters the daily counters above.

### Binary sensors

| Entity | Notes |
|---|---|
| System Running | From the portal's `runningStatus` field |
| System Communication OK | From the portal's `communicationStatus` field |
| EPS Enabled | Emergency power supply / backup setting |
| Zero Export Enabled | Zero-export grid setting |

### Sensors — generator (opt-in via Options, slow poll, only if enabled)

| Entity | Unit | Device class / state class |
|---|---|---|
| Generator Power | W | power / measurement |
| Generator Voltage | V | voltage / measurement |
| Generator Frequency | Hz | frequency / measurement |
| Generator Temperature | °C | temperature / measurement |
| Generator Status Code | — | diagnostic, raw code (unconfirmed meaning) |

## External statistics backfill (Energy dashboard history)

In addition to the native daily-energy sensors above, this integration
maintains a set of **external statistics** under statistic IDs like
`apsystems_cloud_crawler:entry_<sanitized_entry_id>_battery_discharge_today`. These
are a separate, integration-owned backing store used to backfill hourly
historical curves for the Energy dashboard from before the integration was
running. The portal reports energy in 5-minute intervals, but Home
Assistant's external-statistics API only accepts hour-aligned timestamps,
so the 5-minute deltas are summed into whole-UTC-hour buckets before being
written (Home Assistant still displays the resulting hourly bars in your
local timezone). They are intentionally namespaced so this integration can
never collide with or overwrite the statistic IDs Home Assistant derives
from the native sensor entities, or any other integration's statistics —
*we* never auto-register anything as an Energy source; you choose what to
add yourself.

**Important — which one to add as your Energy dashboard source:** the
native sensor and its corresponding `apsystems_cloud_crawler:...` external statistic
are two *separate* entries in the Energy dashboard's source picker (there
is no way for Home Assistant to merge history into a sensor entity's own
statistic from outside its own state updates — a custom integration simply
isn't allowed to write into a `sensor.xxx` statistic_id). **Pick exactly
one of the two, never both** (adding both double-counts every day they
overlap):

* **Add the native sensor** (e.g. "Solar Production Today") if you want
  live, continuously-accurate tracking from the moment you add it onward.
  It will *not* show any history from before it was added.
* **Add the `apsystems_cloud_crawler:...` external statistic instead** — in the
  Energy dashboard's "Add source" picker it's listed with the same
  friendly name plus a `(History)` suffix, e.g. "My Station Solar
  Production Today (History)" — if you want the backfilled pre-existing
  history to actually appear on the dashboard. Its trade-off: it is only
  refreshed when the integration (re)loads (startup gap-fill, bounded to
  the last 7 days) or when you call the `apsystems_cloud_crawler.backfill` service
  manually — not on every live poll — so "today" may lag behind until the
  next restart/manual backfill.

If you've already added the native sensor and only now realize you wanted
history too: remove the sensor as the Energy source, add the
`apsystems_cloud_crawler:...` statistic instead, then call `apsystems_cloud_crawler.backfill`
(see below) for whatever date range you want filled in.

On startup, the integration checks the last known timestamp for each of
these six external statistics and, if there's a gap (e.g. first install, or
Home Assistant was offline), fetches up to 7 days of history from the
portal (5-minute intervals aggregated into hourly points, as above) to fill
it in. If the portal returns no usable data
for a given day (its retention window for historical data is not publicly
documented), the backward backfill loop simply stops early rather than
erroring.

### Manual backfill service (`apsystems_cloud_crawler.backfill`)

The automatic startup gap-fill above is bounded to 7 days back and only
runs when the integration (re)loads. If you want to backfill a specific
date range on demand — for example, right after first installing the
integration (to cover history from before it was ever running), or any
time you suspect a gap — call the `apsystems_cloud_crawler.backfill` service/action:

* **Developer Tools → Actions**: search for "APSystems Cloud Crawler: Backfill
  energy history", pick the config entry (account/station), and choose a
  start date and (optionally) an end date (defaults to today).
* **YAML example**:
  ```yaml
  action: apsystems_cloud_crawler.backfill
  data:
    config_entry_id: <your config entry id>
    start_date: "2026-01-01"
    end_date: "2026-01-15"
  ```

Notes:
* The service writes to the exact same namespaced external-statistic IDs
  (`apsystems_cloud_crawler:<entry_id>_<key>`) as the automatic backfill — it never
  touches the native sensors' own statistics or any other integration's
  data.
* It fetches one day's data at a time (shared across all six counters, not
  once per counter) and pauses briefly between days so a long range
  doesn't hammer the portal.
* A day with no usable response (outside the portal's retention window) is
  logged as a warning and skipped; the rest of the requested range still
  runs.
* A single call is capped at 400 days to avoid an unbounded, accidental
  multi-year request; split a longer range into multiple calls if needed.
* **`force` option**: by default, the service only writes *new* hours
  that aren't already covered by a previously-written statistic point for
  that counter — requesting a range that's entirely older than the most
  recent point already on file is a silent no-op (nothing to resume into).
  Turn on `force: true` to actually (re)write such a range — use it to (a)
  fill in older history discovered *after* a more recent backfill already
  ran, or (b) self-heal a statistic point you suspect was written
  incorrectly by an older version of this integration (e.g. after
  upgrading to a release that fixes a backfill bug — the fix only applies
  to newly-written points, never retroactively corrects what's already in
  the database). Forcing causes a one-time cumulative-total discontinuity
  at the edge of the overwritten range; the hourly energy values
  themselves (what the Energy dashboard's bars show) are unaffected.
  ```yaml
  action: apsystems_cloud_crawler.backfill
  data:
    config_entry_id: <your config entry id>
    start_date: "2026-10-07"
    end_date: "2026-10-07"
    force: true
  ```

### Known limitations

* The portal's day-level retention window for historical 5-minute data is
  unverified; backfill may silently come up short for older gaps if the
  portal has already discarded that day's detail.
* `storageStatus`, battery strategy `mode`, and generator `status`/
  `work_status` codes are exposed as raw passthrough integers. Their
  semantic meaning (e.g. what `1` vs `2` means) has not been confirmed
  against official documentation, so no enum/label mapping is applied —
  only the raw value is exposed, to avoid surfacing a guess as fact.
* Only the first inverter/battery pack is used if an account ever reports
  more than one (`ABD` array with length > 1); multi-station/multi-pack
  accounts are not supported in this version.
* Generator telemetry is the least-verified part of this integration
  (hence it is opt-in and off by default).

## Development / testing

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements_test.txt
pip install homeassistant  # custom_components/apsystems_cloud_crawler/__init__.py imports real HA modules
pytest tests/ -q
```

The crypto (RSA/AES login payload construction, including the tricky
double-URL-encoding quirk described in the inline comments of
`crypto.py`), the JSON-parsing/data-model logic, and a couple of
non-network `api.py` helpers are covered by regular unit tests
(`tests/test_crypto.py`, `tests/test_models.py`, `tests/test_api.py`) that
run in any normal test invocation / CI.

There is also `tests/test_integration_live.py`, marked
`@pytest.mark.integration` and **skipped by default** (it never runs in a
plain `pytest tests/` invocation or CI). It exercises the full login
handshake and all portal endpoints against the real EMA site using a
development/test account. To run it, opt in explicitly:

```bash
RUN_APSYSTEMS_INTEGRATION_TEST=1 pytest tests/test_integration_live.py -v -s
```

The credentials in that file are for a throwaway development/test account
only, intentionally kept there (and nowhere else in the codebase) so this
one manual validation path is reproducible; **a real user always supplies
their own credentials through the Config Flow UI** and nothing in normal
integration code paths ever reads from that file or an environment
variable for credentials.

## License

MIT — see [LICENSE](LICENSE).
