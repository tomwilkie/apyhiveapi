# CLAUDE.md

Guidance for Claude Code when working in this repository, locally and in
GitHub Actions. Both Claude jobs in `.github/workflows/claude.yml` (the
automatic PR review and `@claude` mentions) load this file, so project
knowledge and rules belong here, not in the workflow prompt.

This is pyhive-integration, the async Python library (`apyhiveapi`) behind
the Hive integration for Home Assistant. The source lives in `src/`
(`api/`, `session/`, `devices/`, `helper/`) and is published as the single
package `apyhiveapi`. There is no sync package, no deprecation shims and no
camelCase compatibility aliases: the public API is snake_case only.

## The Home Assistant fork is the only consumer

This library exists to power one client: the owner's fork of the Hive
integration, a custom component at `~/Documents/repos/ha-hive`
(`custom_components/hive/`, GitHub `tomwilkie/ha-hive`). Code the fork does
not use is removed rather than kept, and when the library's API changes the
fork is updated alongside it. A change that looks fine here can still be a
bug on the Home Assistant side.

**Always check the fork's source.** Whenever you review, change or answer a
question about this library's code, read the fork's files that call the code
involved before you conclude. Do not rely on memory of the integration: it
changes. In what you post, name the files you checked and quote the lines
that matter. If nothing in the fork uses the code involved, say so
explicitly: that code is a removal candidate.

- The fork (the actual caller): `~/Documents/repos/ha-hive/custom_components/hive/`
  with `__init__.py`, `config_flow.py`, `entity.py`, `const.py`, `climate.py`,
  `water_heater.py`, `light.py`, `switch.py`, `binary_sensor.py`, `sensor.py`
  and `manifest.json` (which pins this library's version).
- Upstream integration, for reference only:
  <https://github.com/home-assistant/core/tree/dev/homeassistant/components/hive>
- Developer docs:
  - Entities overview: <https://developers.home-assistant.io/docs/core/entity/>
  - climate: <https://developers.home-assistant.io/docs/core/entity/climate/>
  - water_heater: <https://developers.home-assistant.io/docs/core/entity/water-heater/>
  - light: <https://developers.home-assistant.io/docs/core/entity/light/>
  - switch: <https://developers.home-assistant.io/docs/core/entity/switch/>
  - binary_sensor: <https://developers.home-assistant.io/docs/core/entity/binary-sensor/>
  - sensor: <https://developers.home-assistant.io/docs/core/entity/sensor/>
  - Config flow (login, SMS 2FA, reauth): <https://developers.home-assistant.io/docs/config_entries_config_flow_handler/>
  - Setup failures (`ConfigEntryNotReady` / `ConfigEntryAuthFailed`): <https://developers.home-assistant.io/docs/integration_setup_failures/>
  - Blocking calls in the event loop: <https://developers.home-assistant.io/docs/asyncio_blocking_operations/>

The fork breaks when: a method it calls is renamed or removed, a return shape
or `device.status` value type/unit it reads changes, a new exception is raised
that it doesn't catch, or something blocks the event loop. The public API it
depends on is:

- `Hive(websession)`, `hive.session` (the same object), `start_session`,
  `update_data`, `update_interval`, `device_list`
- `Auth(username, password)` with `login`, `sms_2fa`, `is_device_registered`,
  `device_registration`, `get_device_data`, `forget_device`
- `Device` attributes read dict-style with snake_case keys (`device["hive_id"]`,
  `device["device_data"]`, `device["status"]`, `device.get("attributes", {})`)
- `device.status` keys and value types, and the `hive.*` device module methods
  (`get_climate`, `set_mode`, `set_target_temperature`, `set_boost_on`,
  `set_boost_off`, `get_water_heater`, `get_light`, `turn_on`, `turn_off`,
  `get_switch`, `get_sensor`)
- the exceptions in `helper/hive_exceptions.py` and aiohttp's `HTTPException`

### Dependency changes

Home Assistant installs this library inside its own environment, so judge
version ranges in `pyproject.toml` `dependencies` against what Home Assistant
pins *today*, not against the reasons given in older issues or PRs — those
constraints change. Before calling a range right or wrong, fetch:

- <https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/package_constraints.txt>
  (hard pins applied to every integration install, e.g. boto3/botocore)
- <https://raw.githubusercontent.com/home-assistant/core/dev/requirements_all.txt>
  (versions of other integrations' packages, e.g. aiobotocore)

A range that can't be satisfied alongside those pins is a bug: Home Assistant
fails with `RequirementsNotFound`. Quote the pins you checked. Home Assistant
requires Python 3.14; this library supports 3.13 and later.

## Project rules

- Edit the source in `src/` only; there is no generated code.
- Do not add compatibility aliases or shims. Rename in the fork instead.
- The placeholder `aws_access_key_id` / `aws_secret_access_key` /
  `aws_session_token` strings in `hive_auth_async.py` must stay; they are not
  leaked secrets and boto3 Cognito auth breaks without them.
- Never log tokens or PII; log payloads via `HiveHelper.sanitize_payload`.
  Data in `src/data/*.json` must be anonymised.
- New behaviour and bug fixes need tests in `tests/unit`, `tests/module` or
  `tests/e2e`; coverage must stay at 99%.

## Commands

All commands run from the repository root.

```bash
# Install the package and the development tools
pip install -e ".[dev]"

# Run all linters (via pre-commit)
pre-commit run --all-files

# Run tests (always run the full suite — coverage is measured against all of src/)
pytest tests/

# Run a single test
pytest tests/module/test_hub.py::TestHiveHubSensorStatus

# Build the wheel and check it imports in a clean venv
make verify-dist

# Individual linters
ruff check --fix src/
ruff format src/
pylint src/
mypy src/
bandit --configfile=tests/bandit.yaml src/
```

The `no-commit-to-branch` pre-commit hook blocks commits to `master`; day-to-day
work happens on `main`.

A PII check pre-commit hook (`scripts/check_data_pii.py`) runs on any `src/data/*.json` changes — ensure fixture data is anonymised before committing.

### Test organisation

Tests are split into three subdirectories under `tests/`:

| Directory | Purpose |
|---|---|
| `tests/unit/` | Pure unit tests — heavily mocked, no I/O |
| `tests/module/` | Module-level tests using the file-based session (`username="use@file.com"`) |
| `tests/e2e/` | End-to-end / integration tests |

`pyproject.toml` sets `asyncio_mode = "auto"`, so async test functions do **not** need `@pytest.mark.asyncio`.

Coverage is enforced at **99%** (`fail_under = 99` in `pyproject.toml`). Running only a subset of tests will report very low coverage and fail — always verify with the full `pytest tests/` run.

## Architecture

### Entry Point

`src/hive.py` — `Hive` class is the public API. It inherits `HiveSession` and composes all device modules:

```python
from apyhiveapi import Hive

hive = Hive(websession)
device_list = await hive.start_session(config)
```

`hive.session` is `hive` itself; the fork calls `hive.session.start_session(...)`.

### Source layout

```text
src/
├── hive.py                      ← Hive — public entry point; inherits HiveSession
├── __init__.py                  ← exports Hive, Auth, SMS_REQUIRED, all exceptions
├── api/
│   ├── hive_async_api.py        ← HiveApiAsync — HTTP calls to beekeeper API and the SSO login page
│   ├── hive_auth_async.py       ← HiveAuthAsync — AWS Cognito SRP login flows
│   ├── device_registration.py   ← DeviceRegistrationMixin — Cognito device lifecycle
│   └── srp_crypto.py            ← pure SRP/HKDF crypto functions and constants
├── session/
│   ├── __init__.py              ← HiveSession — composes the three session mixins
│   ├── auth.py                  ← SessionAuthMixin — login, token refresh, retry backoff
│   ├── polling.py               ← PollingMixin — rate-limited polling, cache, get_devices
│   └── discovery.py             ← DiscoveryMixin — start_session, create_devices, add_list
├── devices/
│   ├── heating.py               ← HiveHeating / Climate
│   ├── hotwater.py              ← HiveHotwater / WaterHeater
│   ├── light.py                 ← HiveLight / Light
│   ├── plug.py                  ← HiveSmartPlug / Switch
│   ├── sensor.py                ← HiveSensor / Sensor
│   ├── hub.py                   ← HiveHub
│   ├── boost.py                 ← BoostMixin (shared by heating + hotwater)
│   └── color.py                 ← LightColorHandler (colour sub-domain for lights)
└── helper/
    ├── const.py                 ← HIVE_TYPES, PRODUCTS, DEVICES, HIVETOHA, HTTP_OK …
    ├── hivedataclasses.py       ← Device, EntityConfig, SessionTokens, SessionConfig
    ├── device_attributes.py     ← HiveAttributes — HA state attribute computation
    ├── device_handler_base.py   ← BaseDeviceHandler — shared get_*/set_state plumbing
    ├── hive_helper.py           ← HiveHelper — name resolution, schedule parsing
    ├── hive_exceptions.py       ← all custom exceptions
    └── map.py                   ← Map — dict with attribute-style access
```

### Core Session

`src/session/__init__.py` — `HiveSession` inherits three mixins:

| Mixin | File | Responsibility |
|---|---|---|
| `SessionAuthMixin` | `session/auth.py` | Token storage, refresh at 90% lifetime, `_retry_login` backoff |
| `PollingMixin` | `session/polling.py` | Rate-limited polling, entity cache, `update_data`, `update_interval`, `get_devices` |
| `DiscoveryMixin` | `session/discovery.py` | `start_session`, `create_devices`, `add_list`, file-mode loading |

`HiveSession` creates its own `aiohttp.ClientSession` when none is passed and
closes it in `close()` / `__aexit__`. The fork always passes Home Assistant's
shared session; the owned-session path exists for the tests and the dev
scripts.

### API Layer

- **`src/api/hive_async_api.py` (`HiveApiAsync`)** — async HTTP calls to `beekeeper.hivehome.com` (`get_all`, `set_state`) and `get_login_info`, which scrapes the Cognito pool details from the SSO login page
- **`src/api/hive_auth_async.py` (`HiveAuthAsync`)** — AWS Cognito SRP authentication flows; stores `self.access_token` (Cognito AccessToken) after `login()` or `sms_2fa()`
- **`src/api/device_registration.py` (`DeviceRegistrationMixin`)** — mixed into `HiveAuthAsync`; device registration, confirm, forget, `get_device_data()`. Uses `self.access_token` (not `self.session.tokens`).
- **`src/api/srp_crypto.py`** — pure stateless SRP/HKDF crypto functions (`N_HEX`, `compute_hkdf`, etc.)

### Device Modules (all in `src/devices/`)

After `start_session()`, device modules are available as attributes on `hive`:

| `hive.*` | Class | File | Covers |
|---|---|---|---|
| `hive.heating` | `Climate` | `devices/heating.py` | Thermostats, TRV controls |
| `hive.hotwater` | `WaterHeater` | `devices/hotwater.py` | Hot water |
| `hive.light` | `Light` | `devices/light.py` | All Hive light types |
| `hive.switch` | `Switch` | `devices/plug.py` | Smart plugs, Heat on Demand |
| `hive.sensor` | `Sensor` | `devices/sensor.py` | Motion, contact and diagnostic sensors |
| `hive.hub` | `HiveHub` | `devices/hub.py` | Hub / Sense detection |

All device handlers inherit `BaseDeviceHandler` (`src/helper/device_handler_base.py`), which provides:

- `_cached_if_busy`, `_update_online_status`, `_apply_device_props`, `_report_offline` — the shared per-poll steps of every `get_*` method
- `_execute_state_change(device, **state_kwargs)` — token refresh → `set_state` → `get_devices`
- `_get_product_state(device, *path_keys, default=None)` — safe nested read from `session.data.products`
- `_map_hive_to_ha(mapping_key, value)` — translate Hive API value through `HIVETOHA`

`BoostMixin` (`devices/boost.py`) is composed into `HiveHeating` and `HiveHotwater`.
`LightColorHandler` (`devices/color.py`) is composed into `HiveLight` for colour sub-domain.

### Supported Devices

| Device | HA entity type | Hive types |
|---|---|---|
| Heating (thermostat, TRV) | `climate` | `heating`, `trvcontrol` |
| Hot Water | `water_heater` | `hotwater` |
| Lights | `light` | `warmwhitelight`, `tuneablelight`, `colourtuneablelight` |
| Smart Plugs | `switch` | `activeplug` |
| Motion Sensor | `binary_sensor` | `motionsensor` |
| Contact Sensor | `binary_sensor` | `contactsensor` |
| Hub / Sense | `binary_sensor` | `hub`, `sense` |

Hive "actions" (automations) are not supported: the fork never creates
entities for them.

### Device Discovery (`create_devices`)

`PRODUCTS` and `DEVICES` dicts in `src/helper/const.py` map Hive product/device types to lists of `EntityConfig` dataclass instances (entity_type, ha_name, hive_type, temperature_unit). `DiscoveryMixin.create_devices()` iterates these configs and calls `add_list(...)` for each to build `device_list` for Home Assistant entity creation. Only entity types the fork matches on `hive_type` are created.

### `device_list` structure

`hive.device_list` is a dict keyed by HA entity type, populated by `start_session()`:

```python
{
    "parent": [...],  # physical Hive hub devices
    "binary_sensor": [...],  # motion/contact sensors, hub connectivity, Sense detection
    "climate": [...],  # heating zones, TRVs
    "light": [...],
    "sensor": [...],  # battery, power, temperatures, heating/hot water state
    "switch": [...],  # smart plugs, Heat on Demand
    "water_heater": [...],
}
```

Each entry is a `Device` instance. After calling the relevant `get_*` module method, `device.status` holds the current state:

```python
# Heating
device.status["current_temperature"]  # float
device.status["target_temperature"]  # float
device.status["mode"]  # "SCHEDULE" | "MANUAL" | "OFF"
device.status["boost"]  # "ON" | "OFF"

# Light
device.status["state"]  # bool
device.status["brightness"]  # int 0-255
device.status.get("color_temp")  # int mireds (tunable/colour only)
device.status.get("hs_color")  # tuple (colour lights in COLOUR mode)

# Switch / Smart Plug
device.status["state"]  # bool
device.status.get("power_usage")  # float watts (activeplug only)
```

### Data Model

`session.data` (a `Map`) holds:

- `products` / `devices` — dicts keyed by device ID from the Hive API
- `user` — account info

### Helpers

- `src/helper/const.py` — `HIVE_TYPES`, `PRODUCTS`, `DEVICES`, `HIVETOHA` mappings, HTTP constants
- `src/helper/hivedataclasses.py` — `Device`, `EntityConfig`, `SessionTokens`, `SessionConfig` dataclasses. `Device` supports attribute-style (`device.hive_id`) and dict-style (`device["hive_id"]`, `device.get(...)`, `"status" in device`) access with the snake_case attribute names.
- `src/helper/hive_helper.py` (`HiveHelper`) — device name resolution, error tracking, schedule NNL parsing (`get_schedule_nnl`), payload sanitisation for logs
- `src/helper/hive_exceptions.py` — all custom exceptions (`HiveReauthRequired`, `HiveAuthError`, etc.)
- `src/helper/map.py` — `Map` class: dict wrapper allowing attribute-style access (`session.config.home_id`)
- `src/helper/device_attributes.py` (`HiveAttributes`) — computes HA state attributes (online/offline, battery, mode) for all device types
- `src/helper/device_handler_base.py` (`BaseDeviceHandler`) — shared `get_*` / `_execute_state_change` / `_get_product_state` plumbing inherited by all device handlers
- `src/data/*.json` — fixture files for offline/file-based testing

### Authentication

Two flows:

1. **First login** — `Auth.login()` returns `ChallengeName == "SMS_MFA"` (use the `SMS_REQUIRED` constant). Call `auth.sms_2fa(code, result)`, then save the three device credentials from `await auth.get_device_data()` (`device_group_key`, `device_key`, `device_password`). Device registration happens automatically inside `sms_2fa()` — no separate registration call needed.

2. **Subsequent logins** — pass saved `device_data` in the `start_session` config. The library calls `device_login()` internally — no SMS required.

`get_device_data()` is `async def` — always `await` it in async code.

Only the Hive account owner is supported. Guest accounts cannot authenticate.

### File-Based Testing

Set `username="use@file.com"` to make the session load data from `src/data/data.json` instead of calling the API. Used for development without live credentials.

### Token Refresh Strategy

Tokens refresh proactively at 90% of their lifetime (`_refresh_threshold = 0.90`). On `HiveRefreshTokenExpired` or `HiveFailedToRefreshTokens`, the session falls back to `_retry_login` (3 attempts with backoff). A `HiveReauthRequired` exception propagates up when user interaction is needed (SMS 2FA). This is the only exception application code needs to handle explicitly — all intermediate refresh failures are handled internally.

Token layers are distinct:

- `HiveAuthAsync.access_token` — Cognito AccessToken stored on the auth object during `login()`/`sms_2fa()`; used by `DeviceRegistrationMixin`
- `HiveSession.tokens.token_data` — session-layer token store (`"token"`, `"accessToken"`, `"refreshToken"`); used by `HiveApiAsync` for API requests
