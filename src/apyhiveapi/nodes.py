"""Typed snapshots of Hive's ``GET /nodes/all`` response.

Each dataclass mirrors one kind of node Hive returns, with Hive's own field
names in snake_case and Hive's own values: a heating mode can be ``BOOST``,
a previous mode can be ``False``. Nothing is mapped to Home Assistant's
model. A field Hive left out, or sent with an unexpected type, is ``None``.
Only the node types this library supports are kept; the rest are dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

HEATING_PRODUCT_TYPES = frozenset({"heating"})
HOT_WATER_PRODUCT_TYPES = frozenset({"hotwater"})
DEVICE_TYPES = frozenset({"hub", "thermostatui", "boilermodule"})


@dataclass(frozen=True)
class User:
    """The account details Hive returns as ``user``."""

    temperature_unit: str | None


@dataclass(frozen=True)
class HeatingProduct:
    """A ``heating`` product."""

    id: str
    type: str
    name: str | None
    online: bool | None
    model: str | None
    version: str | None
    zone: str | None
    temperature: float | None
    working: bool | None
    mode: str | None
    target: float | None
    boost: int | None
    previous_mode: str | bool | None
    previous_target: float | None


@dataclass(frozen=True)
class HotWaterProduct:
    """A ``hotwater`` product."""

    id: str
    type: str
    name: str | None
    online: bool | None
    model: str | None
    version: str | None
    zone: str | None
    mode: str | None
    status: str | None
    boost: int | None
    previous_mode: str | bool | None


type Product = HeatingProduct | HotWaterProduct


@dataclass(frozen=True)
class Device:
    """A ``hub``, ``thermostatui`` or ``boilermodule`` device."""

    id: str
    type: str
    name: str | None
    online: bool | None
    model: str | None
    manufacturer: str | None
    version: str | None
    zone: str | None
    battery: int | None
    mac_address: str | None


@dataclass(frozen=True)
class Nodes:
    """One ``GET /nodes/all`` response: the account, products and devices."""

    user: User
    products: Mapping[str, Product]
    devices: Mapping[str, Device]


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _mode(value: object) -> str | bool | None:
    return value if isinstance(value, str | bool) else None


def _heating(node: dict[str, Any]) -> HeatingProduct:
    props, state = _dict(node.get("props")), _dict(node.get("state"))
    previous = _dict(props.get("previous"))
    return HeatingProduct(
        id=node["id"],
        type=node["type"],
        name=_str(state.get("name")),
        online=_bool(props.get("online")),
        model=_str(props.get("model")),
        version=_str(props.get("version")),
        zone=_str(props.get("zone")),
        temperature=_float(props.get("temperature")),
        working=_bool(props.get("working")),
        mode=_str(state.get("mode")),
        target=_float(state.get("target")),
        boost=_int(state.get("boost")),
        previous_mode=_mode(previous.get("mode")),
        previous_target=_float(previous.get("target")),
    )


def _hot_water(node: dict[str, Any]) -> HotWaterProduct:
    props, state = _dict(node.get("props")), _dict(node.get("state"))
    return HotWaterProduct(
        id=node["id"],
        type=node["type"],
        name=_str(state.get("name")),
        online=_bool(props.get("online")),
        model=_str(props.get("model")),
        version=_str(props.get("version")),
        zone=_str(props.get("zone")),
        mode=_str(state.get("mode")),
        status=_str(state.get("status")),
        boost=_int(state.get("boost")),
        previous_mode=_mode(_dict(props.get("previous")).get("mode")),
    )


def _device(node: dict[str, Any]) -> Device:
    props, state = _dict(node.get("props")), _dict(node.get("state"))
    return Device(
        id=node["id"],
        type=node["type"],
        name=_str(state.get("name")),
        online=_bool(props.get("online")),
        model=_str(props.get("model")),
        manufacturer=_str(props.get("manufacturer")),
        version=_str(props.get("version")),
        zone=_str(props.get("zone")),
        battery=_int(props.get("battery")),
        mac_address=_str(props.get("macAddress")),
    )


def _nodes_of(items: object, types: frozenset[str]) -> list[dict[str, Any]]:
    """Return the well-formed nodes of *types*: dicts with a string ID and type."""
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item.get("type") in types
    ]


def parse_nodes(response: dict[str, Any]) -> Nodes:
    """Build a :class:`Nodes` snapshot from a parsed ``/nodes/all`` body."""
    products: dict[str, Product] = {
        node["id"]: _heating(node)
        for node in _nodes_of(response.get("products"), HEATING_PRODUCT_TYPES)
    }
    products.update(
        (node["id"], _hot_water(node))
        for node in _nodes_of(response.get("products"), HOT_WATER_PRODUCT_TYPES)
    )
    devices = {
        node["id"]: _device(node)
        for node in _nodes_of(response.get("devices"), DEVICE_TYPES)
    }
    user = User(
        temperature_unit=_str(_dict(response.get("user")).get("temperatureUnit"))
    )
    return Nodes(user=user, products=products, devices=devices)
