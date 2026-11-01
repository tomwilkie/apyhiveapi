# apyhiveapi

![CI](https://github.com/tomwilkie/apyhiveapi/actions/workflows/ci.yml/badge.svg) ![PyPI](https://img.shields.io/pypi/v/apyhiveapi) ![License](https://img.shields.io/github/license/tomwilkie/apyhiveapi)

An async Python library for the [Hive](https://www.hivehome.com/) smart home platform. It exists to power [tomwilkie/ha-hive](https://github.com/tomwilkie/ha-hive), a fork of the Hive integration for [Home Assistant](https://www.home-assistant.io/), and exposes exactly what that integration needs: Hive heating (thermostats and TRVs) and hot water.

> This library is a hard fork of [Pyhass/Pyhive](https://github.com/Pyhass/Pyhive), cut down to what the ha-hive integration needs and restarted at version 0.0.1. It was called `pyhive-integration` until v0.1.5; that name on PyPI belongs to the upstream project, so this one is published as `apyhiveapi`, which is also the import name.

---

## Features

- Async only, built on `aiohttp`
- AWS Cognito SRP authentication with SMS two-factor authentication support
- Automatic token refresh at 90% of token lifetime, with a silent re-login when the refresh token is rejected
- Mirrors Hive's own API: `get_nodes()` is `GET /nodes/all` and `set_state()` is `POST /nodes/{type}/{id}`. There is no polling timer and no cached state; the caller decides when to fetch
- Typed, frozen snapshots of heating and hot water products and devices, using Hive's own field names and values
- File-based offline mode for development and testing without live credentials

## Supported Devices

| Device Type | Capabilities |
| --- | --- |
| **Heating** (thermostat, TRV) | Current / target temperature, mode (schedule / manual / off), boost on/off |
| **Hot Water** | Mode (schedule / on / off), boost on/off |

Lights, smart plugs, sensors and the hub are not supported: the ha-hive integration does not use them.

---

## Installation

From PyPI, or the current `main`:

```bash
pip install apyhiveapi
pip install "apyhiveapi @ git+https://github.com/tomwilkie/apyhiveapi"
```

Every release is also attached to its [GitHub Release](https://github.com/tomwilkie/apyhiveapi/releases) as a wheel and sdist.

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
        hive.start_session(
            {
                "username": username,
                "password": password,
                "tokens": tokens,
                "device_data": auth.get_device_data(),
            }
        )
        nodes = await hive.get_nodes()
        for product in nodes.products.values():
            print(product.type, product.name, product.mode)


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
list the account's heating and hot water products and devices.

```bash
pip install apyhiveapi
python examples/example_login.py
```

---

## Reading and Controlling Devices

`start_session()` only loads the saved credentials; nothing is sent until the
first call. `get_nodes()` fetches `GET /nodes/all` and returns a `Nodes`
snapshot: `user`, `products` and `devices`, each keyed by Hive ID. Products are
`HeatingProduct` (`heating`, `trvcontrol`) or `HotWaterProduct` (`hotwater`);
devices are `Device` (`hub`, `thermostatui`, `boilermodule`, `trv`). Fields keep
Hive's names and values: a heating `mode` can be `BOOST`, a `previous_mode` can be
`False`. A value Hive left out or sent with an unexpected type is `None`.

`set_state(type, id, **state)` sends `POST /nodes/{type}/{id}` with the given
state. The library does not check the values or work out what to send: that is
the caller's job.

```python
nodes = await hive.get_nodes()
heating = nodes.products["<heating id>"]
print(heating.temperature, heating.target, heating.mode, heating.online)

await hive.set_state("heating", heating.id, mode="MANUAL", target=21.0)
await hive.set_state("heating", heating.id, mode="BOOST", boost=30, target=22.0)
await hive.set_state("hotwater", "<hot water id>", mode="SCHEDULE")
```

---

## Errors

Every exception the library raises is a subclass of `HiveError`, grouped by what the
caller should do about it. aiohttp and botocore exceptions never escape the library.

| Exception | Meaning | What to do |
|---|---|---|
| `HiveConnectionError` | Hive or Cognito could not be reached, timed out, failed with a 5xx, sent an unreadable response, or returned no products or no devices | Retry later |
| `HiveApiError` | Hive or Cognito refused the request or answered unexpectedly | Report it; retrying will not help |
| `HiveReauthRequired` | The tokens, refresh token or saved device credentials were rejected, there are no tokens, or Hive wants an SMS code. The session then keeps raising it without contacting Hive until it is given new tokens | Log in again |
| `HiveInvalidUsername`, `HiveInvalidPassword`, `HiveInvalid2FACode` | Subclasses of `HiveReauthRequired` raised by the login flow | Show the user a form error |

`get_nodes()` and `set_state()` raise on every failure and keep nothing between
calls; retrying is up to the caller.

---

## Offline / File-Based Testing

Set `username="use@file.com"` to load device state from the bundled JSON fixture in `src/apyhiveapi/data/` instead of making live API calls. Useful for development without real Hive credentials.

```python
async with Hive(username="use@file.com", password="") as hive:
    hive.start_session({})
    nodes = await hive.get_nodes()
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
- [Releases](https://github.com/tomwilkie/apyhiveapi/releases)
- [Source](https://github.com/tomwilkie/apyhiveapi)
- [Issue Tracker](https://github.com/tomwilkie/apyhiveapi/issues)

---

## License

MIT License — see [LICENSE](https://github.com/tomwilkie/apyhiveapi/blob/main/LICENSE) for details. The original work is copyright Pyhive; the changes in this fork are copyright Tom Wilkie.
