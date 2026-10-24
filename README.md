# pyhive-integration

![CI](https://github.com/tomwilkie/pyhive-integration/actions/workflows/ci.yml/badge.svg) ![Release](https://img.shields.io/github/v/release/tomwilkie/pyhive-integration) ![License](https://img.shields.io/github/license/tomwilkie/pyhive-integration)

An async Python library (`apyhiveapi`) for the [Hive](https://www.hivehome.com/) smart home platform. It exists to power [tomwilkie/ha-hive](https://github.com/tomwilkie/ha-hive), a fork of the Hive integration for [Home Assistant](https://www.home-assistant.io/), and exposes exactly what that integration needs: Hive heating (thermostats and TRVs) and hot water.

> This library is a hard fork of [Pyhass/Pyhive](https://github.com/Pyhass/Pyhive), cut down to what the ha-hive integration needs and restarted at version 0.0.1. It is not published on PyPI: install it from a GitHub release (see below). The import name is `apyhiveapi`.

---

## Features

- Async only, built on `aiohttp`
- AWS Cognito SRP authentication with SMS two-factor authentication support
- Automatic token refresh at 90% of token lifetime, with a silent re-login when the refresh token is rejected
- Polling-based device state, rate limited to one poll per scan interval across all entities
- Full device discovery — returns a ready-to-use device list for the integration's entities
- File-based offline mode for development and testing without live credentials

## Supported Devices

| Device Type | Capabilities |
| --- | --- |
| **Heating** (thermostat, TRV) | Current / target temperature, mode (schedule / manual / off), boost on/off |
| **Hot Water** | Mode (schedule / on / off), boost on/off |

Lights, smart plugs, sensors and the hub are not supported: the ha-hive integration does not use them.

---

## Installation

This library is not on PyPI. Install a release wheel from GitHub, or the current `main`:

```bash
pip install "pyhive-integration @ https://github.com/tomwilkie/pyhive-integration/releases/download/v0.0.1/pyhive_integration-0.0.1-py3-none-any.whl"
pip install "pyhive-integration @ git+https://github.com/tomwilkie/pyhive-integration"
```

Requires Python 3.13+.

---

## Quick Start

```python
import asyncio
import getpass

from aiohttp import ClientSession
from apyhiveapi import SMS_REQUIRED, Auth, Hive


async def main():
    username = input("Hive email: ")
    password = getpass.getpass("Hive password: ")

    auth = Auth(username=username, password=password)
    tokens = await auth.login()
    if tokens.get("ChallengeName") == SMS_REQUIRED:
        tokens = await auth.sms_2fa(input("SMS code: "), tokens)
        await auth.device_registration()

    async with ClientSession() as websession:
        hive = Hive(websession)
        devices = await hive.start_session(
            {
                "username": username,
                "password": password,
                "tokens": tokens,
                "device_data": auth.get_device_data(),
            }
        )
        for climate in devices["climate"]:
            await hive.heating.get_climate(climate)
            print(climate.ha_name, climate.status)


asyncio.run(main())
```

---

## Authentication

Authentication uses the AWS Cognito SRP flow. On the first login `login()` returns a
`ChallengeName` of `SMS_MFA` (the `SMS_REQUIRED` constant) when the account has SMS
two-factor authentication enabled; pass the code to `sms_2fa(code, tokens)`. Then call
`device_registration()` to register the machine as a trusted device and save the
credentials `get_device_data()` returns, so later sessions can log in without an SMS.

> **Note:** Only the Hive account owner is supported. Guest accounts cannot be used.

### Standalone login example

[`examples/example_login.py`](examples/example_login.py) is a complete runnable
script. The first run asks for your email, password and SMS code, registers the
machine as a trusted device and saves the device credentials to
`hive_device.json`. Later runs log in with that file and skip the SMS. Both runs
list every discovered device.

```bash
pip install "pyhive-integration @ git+https://github.com/tomwilkie/pyhive-integration"
python examples/example_login.py
```

---

## Controlling Devices

After `start_session`, device modules are available directly on the `Hive` instance.
Commands return nothing and raise a `HiveError` when the change could not be made;
readers never raise and report unknown values as `None`.

```python
# Heating
await hive.heating.set_target_temperature(device, 21.0)
await hive.heating.set_mode(device, "SCHEDULE")
await hive.heating.set_boost_on(device, 30, 22.0)
await hive.heating.set_boost_off(device)

# Hot water
await hive.hotwater.set_mode(device, "MANUAL")
await hive.hotwater.set_boost_on(device, 60)
await hive.hotwater.set_boost_off(device)

# Refresh state (rate limited to the configured scan interval)
await hive.update_data()
device = await hive.heating.get_climate(device)
print(device.status, device.device_data["online"])
```

---

## Errors

Every exception the library raises is a subclass of `HiveError`, grouped by what the
caller should do about it. aiohttp and botocore exceptions never escape the library.

| Exception | Meaning | What to do |
|---|---|---|
| `HiveConnectionError` | Hive or Cognito could not be reached, timed out, failed with a 5xx, sent an unreadable response, or returned no devices when the session started | Retry later |
| `HiveApiError` | Hive or Cognito refused the request or answered unexpectedly | Report it; retrying will not help |
| `HiveDeviceOffline` | A command was sent to a device that is offline or no longer on the account | Report it |
| `HiveReauthRequired` | The tokens, refresh token or saved device credentials were rejected, there are no tokens, or Hive wants an SMS code. The session then keeps raising it without contacting Hive until it is given new tokens | Log in again |
| `HiveInvalidUsername`, `HiveInvalidPassword`, `HiveInvalid2FACode` | Subclasses of `HiveReauthRequired` raised by the login flow | Show the user a form error |

Invalid arguments, such as a boost of zero minutes, raise `ValueError`. Polling through
`update_data` logs transient failures, keeps the last known state and retries after
30 seconds; only `HiveReauthRequired` propagates from it.

---

## Offline / File-Based Testing

Set `username="use@file.com"` to load device state from the bundled JSON fixture in `src/apyhiveapi/data/` instead of making live API calls. Useful for development without real Hive credentials.

```python
async with Hive(username="use@file.com", password="") as hive:
    await hive.start_session({})
```

---

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run linters
pre-commit run --all-files

# Run tests
pytest tests/
```

---

## Links

- [ha-hive](https://github.com/tomwilkie/ha-hive), the Home Assistant integration this library powers
- [Releases](https://github.com/tomwilkie/pyhive-integration/releases)
- [Source](https://github.com/tomwilkie/pyhive-integration)
- [Issue Tracker](https://github.com/tomwilkie/pyhive-integration/issues)

---

## License

MIT License — see [LICENSE](https://github.com/tomwilkie/pyhive-integration/blob/main/LICENSE) for details. The original work is copyright Pyhive; the changes in this fork are copyright Tom Wilkie.
