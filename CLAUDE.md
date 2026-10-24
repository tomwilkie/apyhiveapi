# CLAUDE.md

Guidance for Claude Code when working in this repository, locally and in
GitHub Actions. Both Claude jobs in `.github/workflows/claude.yml` (the
automatic PR review and `@claude` mentions) load this file, so project
knowledge and rules belong here, not in the workflow prompt.

This is pyhive-integration, the async Python library (`apyhiveapi`) behind
the Hive integration for Home Assistant. It is a hard fork of Pyhass/Pyhive,
restarted at version 0.0.1 and installed from its GitHub releases rather than
PyPI. It covers Hive heating (thermostats and TRVs) and hot water only. The source is one flat package in
`src/apyhiveapi/`: no sync package, no mixins, no deprecation shims and no
camelCase compatibility aliases. The public API is snake_case only.

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
  `water_heater.py` and `manifest.json` (which pins this library's version).
  Its `light.py`, `switch.py`, `binary_sensor.py` and `sensor.py` platforms
  have no counterpart in this library any more. In GitHub Actions, where the
  local checkout is not available, fetch the raw files from
  `https://raw.githubusercontent.com/tomwilkie/ha-hive/main/custom_components/hive/<file>`.
- Upstream integration, for reference only:
  <https://github.com/home-assistant/core/tree/dev/homeassistant/components/hive>
- Developer docs:
  - Entities overview: <https://developers.home-assistant.io/docs/core/entity/>
  - climate: <https://developers.home-assistant.io/docs/core/entity/climate/>
  - water_heater: <https://developers.home-assistant.io/docs/core/entity/water-heater/>
  - Config flow (login, SMS 2FA, reauth): <https://developers.home-assistant.io/docs/config_entries_config_flow_handler/>
  - Setup failures (`ConfigEntryNotReady` / `ConfigEntryAuthFailed` / `ConfigEntryError`): <https://contributors.home-assistant.io/core/integrations/setup-failures/>
  - Blocking calls in the event loop: <https://developers.home-assistant.io/docs/asyncio_blocking_operations/>

The fork breaks when: a method it calls is renamed or removed, a return shape
or `device.status` value type/unit it reads changes, a new exception is raised
that it doesn't catch, or something blocks the event loop. The public API it
depends on is:

- `Hive(websession)` with `start_session`, `update_data`, `update_interval`,
  `device_list` and `close`
- `Auth(username, password)` (the `HiveAuth` class) with `login`, `sms_2fa`,
  `device_login`, `is_device_registered`, `device_registration`,
  `get_device_data` and `forget_device`
- `Device` attributes, read attribute-style or dict-style with snake_case keys
  (`device.hive_id`, `device["device_data"]`, `device.get("status")`)
- `device.status` keys and value types, and the `hive.heating` /
  `hive.hotwater` methods (`get_climate`, `get_water_heater`, `get_mode`,
  `set_mode`, `set_target_temperature`, `set_boost_on`, `set_boost_off`)
- the exceptions exported from `apyhiveapi` (see "Error handling" below) and
  `ValueError` for invalid command arguments

The fork still uses the previous names (`hive.session.*`, camelCase methods
and `Device` keys, imports from `apyhiveapi.helper.hive_exceptions`), catches
aiohttp's `HTTPException` (which this library no longer raises), treats the
`set_*` methods as returning `bool`, awaits `get_device_data()` and
`update_interval()`, which are now synchronous, and passes a device to
`update_data()`, which no longer takes one. It has to be migrated to this API before
its `manifest.json` is bumped to this version. The mapping it needs is:
`HiveReauthRequired` → `ConfigEntryAuthFailed`, `HiveConnectionError` →
`ConfigEntryNotReady`, any other `HiveError` at setup → `ConfigEntryError`,
and `HiveError` from a command → `HomeAssistantError`.

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

- Edit the source in `src/apyhiveapi/` only; there is no generated code.
- Keep it flat and concrete: plain classes and functions, no mixins, no
  compatibility aliases or shims. When a name changes, rename it in the fork.
- Everything is fully annotated and `mypy --strict` passes on `src/`. Ruff runs
  with every rule enabled (`select = ["ALL"]`); the ignore lists in
  `pyproject.toml` are deliberate and short, so fix findings rather than
  adding `noqa` comments.
- The placeholder `aws_access_key_id` / `aws_secret_access_key` /
  `aws_session_token` strings in `auth.py` must stay; they are not leaked
  secrets and boto3 Cognito auth breaks without them.
- Never log tokens or PII; log payloads via `apyhiveapi.helpers.sanitize_payload`.
  Data in `src/apyhiveapi/data/*.json` must be anonymised.
- New behaviour and bug fixes need tests in `tests/unit`, `tests/module` or
  `tests/e2e`; coverage must stay at 99%.

### Error handling

Exceptions are classified by what the caller should do, not by where they
happened. Everything raised is a subclass of `HiveError` (`exceptions.py`):

| Exception | Meaning | Caller's reaction |
|---|---|---|
| `HiveConnectionError` | Hive or Cognito unreachable, timeout, HTTP 5xx, Cognito throttling, unreadable body | transient: retry later |
| `HiveApiError` | Hive or Cognito refused the request or answered unexpectedly | give up on this request |
| `HiveDeviceOffline` | a command was sent to a device that is offline or no longer on the account | tell the user |
| `HiveReauthRequired` | tokens, refresh token or saved device credentials rejected, no tokens, or an SMS code is needed | the user must log in again |
| `HiveInvalidUsername` / `HiveInvalidPassword` / `HiveInvalid2FACode` | subclasses of `HiveReauthRequired` raised by the login flow | show a form error |

The rules that keep it consistent:

- **Translate third-party errors once, at the boundary.** Every botocore error
  is handled in `HiveAuth._cognito()` and every aiohttp error in `api._fetch()`
  / `HiveApi.request()`. Nothing above those two functions imports or catches
  aiohttp or botocore exceptions, and no `HiveError` is ever re-wrapped as
  another `HiveError`. Chain the original with `raise ... from err`.
- **Handle or raise, never both.** Do not log an exception and re-raise it.
  The handler that finally deals with it logs once: a WARNING without a
  traceback for expected failures, `_LOGGER.exception` only for unexpected ones.
- **Programming errors stay uncaught.** `KeyError`, `TypeError`,
  `RuntimeError` and friends are never caught or converted; a traceback is the
  right outcome for a bug. The one exception is device discovery, which skips
  a malformed product with a warning rather than failing the whole session.
- **Retry only what is transient.** There is no retry helper. Polls that fail
  with any `HiveError` other than `HiveReauthRequired` keep the last known
  state and are retried after `POLL_RETRY_DELAY`; a rejected token leads to one
  re-login through `Hive._api_call()`. "Reauth required" is raised only when
  Hive or Cognito actually rejected the credentials or asked for an SMS code,
  never as a relabelling of some other failure.
- **Commands raise, readers return None, polls return bool.** `set_*` methods
  return nothing and raise on failure (`HiveDeviceOffline` included).
  `ValueError` is for invalid arguments (a boost of zero minutes). `get_*`
  methods never raise and report unknown values as `None`. `update_data()` and
  `get_devices()` return whether new data was stored and raise only
  `HiveReauthRequired`. `start_session()` lets every error propagate so the
  fork can map it to the right `ConfigEntry*` exception.
- **Read JSON through one accessor.** Handlers read product state with
  `_get_product_state()`; a missing key is data, not an error, and is not
  logged (the poll-level `_keep_missing_state()` already warns once per poll
  about incomplete state).

## Commands

All commands run from the repository root.

```bash
# Install the package and the development tools
pip install -e ".[dev]"

# Run all linters (via pre-commit)
pre-commit run --all-files

# Run tests (always run the full suite — coverage is measured against all of src/)
pytest tests/

# Run a single test class
pytest tests/unit/test_heating.py::TestGetClimate

# Build the wheel and check it imports in a clean venv
make verify-dist

# Individual linters
ruff check --fix src/ tests/ scripts/ examples/
ruff format src/ tests/ scripts/ examples/
pylint src/
mypy src/
bandit --configfile=tests/bandit.yaml src/
```

All work happens on `main` through pull requests. A release is a version bump
in `pyproject.toml` merged into `main`: `release.yml` tags it and publishes a
GitHub Release with the wheel attached (see `docs/workflows/README.md`).

A PII check pre-commit hook (`scripts/check_data_pii.py`) runs on any
`src/apyhiveapi/data/*.json` change — ensure fixture data is anonymised before
committing.

### Development scripts

- `scripts/run_session.py` (`make run-file`, `make run-live`, `make call ...`)
  runs a polling session in the background against the fixture or a real
  account and takes admin commands over a local socket. Read-only unless
  started with `WRITE=1`.
- `scripts/verify_dist.py` installs the built wheel in a throwaway venv and
  imports every module (`make verify-dist`).
- `examples/example_login.py` is the minimal standalone login example the
  README points at.

### Test organisation

Tests are split into three subdirectories under `tests/`:

| Directory | Purpose |
|---|---|
| `tests/unit/` | Pure unit tests — heavily mocked, no I/O |
| `tests/module/` | Module-level tests using the file-based session (`username="use@file.com"`) |
| `tests/e2e/` | End-to-end tests over the whole library in file mode |

`tests/conftest.py` provides the `file_session` fixture (a started file-mode
`Hive`), `make_session()` (a `MagicMock` session with `data`, `config`,
`device_recovered`, `report_device_offline`, `set_state` and `get_devices`,
which is all the device handlers use) and `make_device()`.

`pyproject.toml` sets `asyncio_mode = "auto"`, so async test functions do
**not** need `@pytest.mark.asyncio`.

Coverage is enforced at **99%** (`fail_under = 99` in `pyproject.toml`).
Running only a subset of tests will report very low coverage and fail — always
verify with the full `pytest tests/` run.

## Architecture

### Source layout

```text
src/apyhiveapi/
├── __init__.py         ← public exports: Hive, Auth, Device, SMS_REQUIRED, the exceptions
├── hive.py             ← Hive — the session: login and token refresh, polling,
│                         start_session / create_devices, device bookkeeping
├── auth.py             ← HiveAuth (exported as Auth) — Cognito SRP login, SMS 2FA,
│                         token refresh, trusted devices; _cognito() translates botocore errors
├── api.py              ← HiveApi — HTTP calls to beekeeper.hivehome.com, and get_login_info()
│                         for the SSO login page; _fetch() translates aiohttp errors
├── srp.py              ← pure SRP/HKDF crypto functions and constants
├── device_handler.py   ← DeviceHandler — shared read / refresh / _set_state plumbing
├── heating.py          ← Climate (hive.heating): thermostats and TRV controls
├── hotwater.py         ← WaterHeater (hive.hotwater)
├── models.py           ← Device, SessionData, SessionTokens, SessionConfig dataclasses
├── const.py            ← SMS_REQUIRED, PRODUCT_ENTITY_TYPES, THERMOSTAT_DEVICE_TYPES
├── exceptions.py       ← the HiveError hierarchy
├── helpers.py          ← sanitize_payload for logging
└── data/data.json      ← anonymised all-nodes response for file mode
```

### `Hive` (`hive.py`)

`Hive(websession=None, username=None, password=None)` is the whole session.
It owns `hive.auth` (`HiveAuth`), `hive.api` (`HiveApi`), `hive.tokens`,
`hive.config`, `hive.data` (`SessionData` with `products`, `devices` and
`user`, dicts keyed by Hive ID), `hive.device_list`, `hive.heating` and
`hive.hotwater`.

- **Login and tokens** — `login()` logs in with the stored credentials,
  completes the device challenge with `auth.device_login()` and raises
  `HiveReauthRequired` if Hive wants an SMS code. `update_tokens()` stores a
  Cognito response or a saved config entry. `hive_refresh_tokens()` refreshes
  at 90% of the token lifetime (`REFRESH_THRESHOLD`) and falls back to
  `login()` when the refresh token is rejected. `_api_call()` wraps every API
  call: refresh first, and on `HiveReauthRequired` (HTTP 401/403) log in once
  and retry. `set_state()` is the handlers' way to change a product, a no-op in
  file mode.
- **Polling** — `update_data()` is rate limited to `config.scan_interval`
  (`update_interval()` sets it, never below `MIN_SCAN_INTERVAL`) and serialised
  through `update_lock`; concurrent callers return `False` instead of waiting.
  `get_devices()` polls now through `_poll()`, which fetches `/nodes/all` (or
  the fixture) and stores products, devices and user. Products that come back
  without their `REQUIRED_STATE_KEYS` keep the previous state for up to
  `MAX_STALE_STATE_POLLS` polls. There is no entity cache: `_store_nodes()` is
  synchronous, so readers never see a half-updated poll.
- **Discovery** — `start_session(config)` applies the config (username and
  password, `tokens`, `device_data`, `options.scan_interval`; the username
  `use@file.com` switches to file mode), polls once and calls
  `create_devices()`, which builds `device_list` with the keys `parent`,
  `climate` and `water_heater` from `PRODUCT_ENTITY_TYPES`. Heating and hot
  water products attach to the thermostat in their zone, TRV controls to their
  first TRV; a malformed product is skipped with a warning. An entity is named
  after its product (`hive_name` / `ha_name`), the device after the hardware
  that carries it (`device_name`); Hive's `Receiver` is shown as `Heating`.
- **Device bookkeeping** — `device_recovered()`, `report_device_offline()`
  (logs once until the device recovers) and `_device_name()`.

`Hive` creates its own `aiohttp.ClientSession` when none is passed and closes
it in `close()` / `__aexit__`. The fork always passes Home Assistant's shared
session; the owned-session path exists for the tests and the dev scripts.

### Device handlers

`DeviceHandler(session)` (`device_handler.py`) holds what `Climate` and
`WaterHeater` share: `_get_product_state()` (safe nested read from
`session.data.products`), `_as_float()`, `_is_online()`, `_refresh_device()`
(copies the device's props into `device.device_data` with an `online` flag and
reports offline devices through the session), `_boost_status()` and
`_set_state(device, **state)`, which raises `HiveDeviceOffline` for an
offline or unknown device and otherwise calls `session.set_state()` and
re-polls.

| `hive.*` | Class | Hive types | `device.status` after `get_*` |
|---|---|---|---|
| `hive.heating` | `Climate` | `heating`, `trvcontrol` | `current_temperature` (float), `target_temperature` (float), `action` (bool, calling for heat), `mode` (`SCHEDULE` / `MANUAL` / `OFF`), `boost` (`ON` / `OFF`); each `None` when unknown or offline |
| `hive.hotwater` | `WaterHeater` | `hotwater` | `current_operation` (`SCHEDULE` / `ON` / `OFF` / `None`) |

`get_climate()` also sets `device.min_temp` / `device.max_temp`
(`MIN_TARGET_TEMPERATURE` 5.0, `MAX_TARGET_TEMPERATURE` 32.0). Both handlers
offer `get_mode`, `set_mode`, `set_boost_on` and `set_boost_off`; `Climate`
adds `set_target_temperature`. While a boost is active Hive reports `BOOST`,
so `get_mode` reports the mode the boost will return to, and `set_boost_off`
restores it (a no-op when nothing is boosting).

### `Device` (`models.py`)

`Device` is a dataclass: `hive_id`, `hive_name`, `hive_type`, `ha_type`,
`device_id`, `device_name`, `device_data`, `parent_device`, `ha_name`,
`temperature_unit`, `status`, `min_temp`, `max_temp`. It supports
attribute-style and dict-style access (`device["hive_id"]`, `device.get(...)`,
`"status" in device`) with the snake_case names only.

### Authentication (`auth.py`)

Two flows:

1. **First login** — `Auth.login()` returns `ChallengeName == "SMS_MFA"` (use
   the `SMS_REQUIRED` constant). Call `auth.sms_2fa(code, result)`, then
   `auth.device_registration()` to make this machine a trusted device, then
   save the credentials from `auth.get_device_data()`
   (`device_group_key`, `device_key`, `device_password`, `token_created`).
2. **Subsequent logins** — pass the saved `device_data` in the `start_session`
   config. `login()` is answered with a device challenge, which
   `device_login()` completes without an SMS.

Every Cognito call goes through `HiveAuth._cognito(method, code_map, **kwargs)`,
which runs the boto3 call in the executor and maps Cognito's error codes to
the library's exceptions. Only the Hive account owner is supported. Guest
accounts cannot authenticate.

Token layers are distinct:

- `HiveAuth.access_token` — Cognito AccessToken stored on the auth object by
  every successful login, 2FA or refresh; used by the trusted-device methods
- `Hive.tokens.token_data` — session-layer token store (`"token"`,
  `"accessToken"`, `"refreshToken"`); used by `HiveApi` for API requests

All timestamps are timezone-aware UTC `datetime`s.

### File-based testing

Set `username="use@file.com"` to make the session load
`src/apyhiveapi/data/data.json`, an anonymised all-nodes response (`user`,
`products`, `devices`), instead of calling the API. Used for development
without live credentials. Commands send nothing in file mode.
