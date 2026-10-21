"""Constants for apyhiveapi."""

from typing import Any

from .hivedataclasses import EntityConfig

SMS_REQUIRED = "SMS_MFA"


# HTTP return codes.
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_ACCEPTED = 202
HTTP_MOVED_PERMANENTLY = 301
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_METHOD_NOT_ALLOWED = 405
HTTP_UNPROCESSABLE_ENTITY = 422
HTTP_TOO_MANY_REQUESTS = 429
HTTP_INTERNAL_SERVER_ERROR = 500
HTTP_BAD_GATEWAY = 502
HTTP_SERVICE_UNAVAILABLE = 503

EXPECTED_DEVICE_DATA_LENGTH = 3


HIVETOHA: dict[str, Any] = {
    "Attribute": {True: "Online", False: "Offline"},
    "Boost": {None: "OFF", False: "OFF"},
    "Heating": {False: "OFF", "ENABLED": True, "DISABLED": False},
    "Hotwater": {"MANUAL": "ON", None: "OFF", False: "OFF"},
    "Hub": {
        "Status": {True: 1, False: 0},
        "Smoke": {True: 1, False: 0},
        "Dog": {True: 1, False: 0},
        "Glass": {True: 1, False: 0},
    },
    "Light": {"ON": True, "OFF": False},
    "Sensor": {
        "OPEN": True,
        "CLOSED": False,
        True: "Online",
        False: "Offline",
    },
    "Switch": {"ON": True, "OFF": False},
}

HIVE_TYPES = {
    "Hub": ["hub", "sense"],
    "Thermo": ["thermostatui", "trv"],
    "Heating": ["heating", "trvcontrol"],
    "Hotwater": ["hotwater"],
    "Light": ["warmwhitelight", "tuneablelight", "colourtuneablelight"],
    "Sensor": ["motionsensor", "contactsensor"],
    "Switch": ["activeplug"],
}


def _sensor(ha_name: str, hive_type: str) -> EntityConfig:
    """Return the config for a sensor entity."""
    return EntityConfig(entity_type="sensor", ha_name=ha_name, hive_type=hive_type)


def _heating_sensors() -> list[EntityConfig]:
    """Return the sensors shared by heating zones and TRV controls."""
    return [
        _sensor(" Current Temperature", "Heating_Current_Temperature"),
        _sensor(" Target Temperature", "Heating_Target_Temperature"),
        _sensor(" State", "Heating_State"),
        _sensor(" Mode", "Heating_Mode"),
        _sensor(" Boost", "Heating_Boost"),
    ]


def _battery_sensors() -> list[EntityConfig]:
    """Return the sensors created for battery-powered devices."""
    return [_sensor(" Battery Level", "Battery")]


PRODUCTS = {
    "sense": [
        EntityConfig(
            entity_type="binary_sensor",
            ha_name="Glass Detection",
            hive_type="GLASS_BREAK",
        ),
        EntityConfig(
            entity_type="binary_sensor", ha_name="Smoke Detection", hive_type="SMOKE_CO"
        ),
        EntityConfig(
            entity_type="binary_sensor",
            ha_name="Dog Bark Detection",
            hive_type="DOG_BARK",
        ),
    ],
    "heating": [
        EntityConfig(entity_type="climate"),
        EntityConfig(
            entity_type="switch",
            ha_name=" Heat on Demand",
            hive_type="Heating_Heat_On_Demand",
        ),
        *_heating_sensors(),
    ],
    "trvcontrol": [EntityConfig(entity_type="climate"), *_heating_sensors()],
    "hotwater": [
        EntityConfig(entity_type="water_heater"),
        _sensor("Hotwater State", "Hotwater_State"),
        _sensor("Hotwater Mode", "Hotwater_Mode"),
        _sensor("Hotwater Boost", "Hotwater_Boost"),
    ],
    "activeplug": [
        EntityConfig(entity_type="switch"),
        _sensor(" Power", "Power"),
    ],
    "warmwhitelight": [EntityConfig(entity_type="light")],
    "tuneablelight": [EntityConfig(entity_type="light")],
    "colourtuneablelight": [EntityConfig(entity_type="light")],
    "motionsensor": [
        EntityConfig(entity_type="binary_sensor"),
        _sensor(" Current Temperature", "Current_Temperature"),
    ],
    "contactsensor": [
        EntityConfig(entity_type="binary_sensor"),
    ],
}

DEVICES = {
    "contactsensor": _battery_sensors(),
    "hub": [
        EntityConfig(
            entity_type="binary_sensor",
            ha_name="Hive Hub Status",
            hive_type="Connectivity",
        ),
    ],
    "motionsensor": _battery_sensors(),
    "sense": [
        EntityConfig(
            entity_type="binary_sensor",
            ha_name="Hive Hub Status",
            hive_type="Connectivity",
        ),
    ],
    "thermostatui": _battery_sensors(),
    "trv": _battery_sensors(),
}
