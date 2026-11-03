"""Minimal standalone login example for hiveheating.

Run it once: it asks for your Hive email and password, completes the SMS
two-factor challenge, registers this machine as a trusted device and saves
the device credentials to ``hive_device.json`` in the working directory.
Run it again: it logs in with the saved device credentials, no SMS needed,
and lists the same products and devices.

    pip install hiveheating
    python examples/example_login.py

The password is never stored. The saved file holds a device password that
lets anyone with the file log in to your Hive account without an SMS code,
so keep it private (it is written with owner-only permissions). Delete the
file to force a fresh SMS login next time.

Only the Hive account owner can log in. Guest accounts are not supported.
"""

import asyncio
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from aiohttp import ClientSession

from hiveheating import Auth, Hive, HiveReauthRequired, LoginResult

DEVICE_FILE = Path("hive_device.json")


def load_device() -> dict[str, Any]:
    """Return the saved username and device credentials, or an empty dict."""
    if not DEVICE_FILE.exists():
        return {}
    try:
        return json.loads(DEVICE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_device(username: str, device_data: list[str]) -> None:
    """Save the username and device credentials with owner-only permissions."""
    DEVICE_FILE.write_text(
        json.dumps({"username": username, "device_data": device_data}, indent=2),
        encoding="utf-8",
    )
    DEVICE_FILE.chmod(0o600)


def tokens_from(result: LoginResult) -> dict[str, Any]:
    """Keep the part of Cognito's response that new_session reads."""
    return {"AuthenticationResult": result.response["AuthenticationResult"]}


async def device_login(
    websession: ClientSession, username: str, password: str, device_data: list[str]
) -> LoginResult | None:
    """Log in with saved device credentials. Returns None if Hive rejects them."""
    auth = Auth(username, password, *device_data, websession=websession)
    try:
        # The password login comes first; Hive answers it with a device
        # challenge, which device_login() then completes without an SMS.
        result = await auth.login()
        if result.device_challenge:
            result = await auth.device_login()
    except HiveReauthRequired:
        return None
    return result if result.complete else None


async def sms_login(
    websession: ClientSession, username: str, password: str
) -> tuple[LoginResult, list[str]]:
    """Log in with password + SMS code and register this machine as a device."""
    auth = Auth(username, password, websession=websession)
    result = await auth.login()
    if result.sms_required:
        code = input("SMS code: ").strip()
        result = await auth.sms_2fa(code, result)
    if not result.complete:
        sys.exit(f"Login did not complete: {result.challenge}")

    device_data: list[str] = []
    if auth.device_key:
        # Cognito ties the new tokens to this device. Without registering it
        # the refresh token is rejected and every login would need an SMS.
        await auth.device_registration()
        group_key, key, device_password = auth.get_device_data()
        if group_key and key and device_password:
            device_data = [group_key, key, device_password]
    return result, device_data


async def build_config(websession: ClientSession) -> dict[str, Any]:
    """Log in (reusing saved device credentials when possible)."""
    stored = load_device()
    username = stored.get("username") or input("Hive email: ").strip()
    password = getpass.getpass("Hive password (hidden, not stored): ")
    device_data = stored.get("device_data") or []

    result: LoginResult | None = None
    if device_data:
        print("Logging in with the saved device credentials (no SMS)...")
        result = await device_login(websession, username, password, device_data)
        if result is None:
            print("Hive rejected the saved device credentials - signing in again.")
            DEVICE_FILE.unlink(missing_ok=True)
            device_data = []

    if result is None:
        print("Logging in with password + SMS...")
        result, device_data = await sms_login(websession, username, password)
        if all(device_data):
            save_device(username, device_data)
            print(f"Device credentials saved to {DEVICE_FILE.resolve()}")
        else:
            print("Device credentials not available - the next run will need an SMS.")

    config = {"tokens": tokens_from(result), "username": username, "password": password}
    if all(device_data):
        config["device_data"] = device_data
    return config


async def main() -> None:
    """Log in, start a session and list the products and devices."""
    async with ClientSession() as websession:
        config = await build_config(websession)
        hive = Hive(websession)
        hive.new_session(config)
        nodes = await hive.get_nodes()
        print("\nProducts:")
        for product in nodes.products.values():
            print(f"  {product.type:12} {product.name}  (mode {product.mode})")
        print("\nDevices:")
        for device in nodes.devices.values():
            print(f"  {device.type:12} {device.name}  ({device.model})")


if __name__ == "__main__":
    asyncio.run(main())
