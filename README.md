# pyhive-integration

![CI](https://github.com/Pyhass/Pyhive/actions/workflows/ci.yml/badge.svg) ![PyPI](https://img.shields.io/pypi/v/pyhive-integration) ![Python](https://img.shields.io/pypi/pyversions/pyhive-integration) ![License](https://img.shields.io/github/license/Pyhass/Pyhive)

An async Python library (`apyhiveapi`) for the [Hive](https://www.hivehome.com/) smart home platform. It exists to power the Hive integration for [Home Assistant](https://www.home-assistant.io/) and exposes exactly what that integration needs: Hive heating (thermostats and TRVs) and hot water.

> **Package rename notice:** This package replaces the legacy `pyhiveapi` package on PyPI. The import name is `apyhiveapi`.

---

## Features

- Async only, built on `aiohttp`
- AWS Cognito SRP authentication with SMS two-factor authentication support
- Automatic token refresh at 90% of token lifetime with silent retry on expiry
- Polling-based device state with a smart cache to avoid stale reads during in-progress polls
- Full device discovery — returns a ready-to-use device list for Home Assistant entity creation
- File-based offline mode for development and testing without live credentials

## Supported Devices

| Device Type | Capabilities |
| --- | --- |
| **Heating** (thermostat, TRV) | Current / target temperature, mode (schedule / manual / off), boost on/off |
| **Hot Water** | Mode (schedule / on / off), boost on/off |

Lights, smart plugs, sensors and the hub are not supported: the Home Assistant integration this library serves does not use them.

---

## Installation

```bash
pip install pyhive-integration
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

    async with ClientSession() as websession:
        hive = Hive(websession)
        devices = await hive.start_session(
            {
                "username": username,
                "password": password,
                "tokens": tokens,
                "device_data": await auth.get_device_data(),
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
two-factor authentication enabled; pass the code to `sms_2fa(code, tokens)`. That call
also registers the machine as a trusted device, and `get_device_data()` then returns
the credentials to save so later sessions can log in without an SMS.

> **Note:** Only the Hive account owner is supported. Guest accounts cannot be used.

### Standalone login example

[`examples/example_login.py`](examples/example_login.py) is a complete runnable
script. The first run asks for your email, password and SMS code, registers the
machine as a trusted device and saves the device credentials to
`hive_device.json`. Later runs log in with that file and skip the SMS. Both runs
list every discovered device.

```bash
pip install pyhive-integration
python examples/example_login.py
```

---

## Controlling Devices

After `start_session`, device modules are available directly on the `Hive` instance:

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
await hive.update_data(device)
device = await hive.heating.get_climate(device)
```

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

- [PyPI](https://pypi.org/project/pyhive-integration/)
- [Source](https://github.com/Pyhass/Pyhive)
- [Issue Tracker](https://github.com/Pyhass/Pyhive/issues)

---

## License

MIT License — see [LICENSE](https://github.com/Pyhass/Pyhive/blob/master/LICENSE) for details.
