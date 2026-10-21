"""Constants for Pyhiveapi."""

from typing import Any

from .hivedataclasses import EntityConfig

SYNC_PACKAGE_NAME = "pyhiveapi"
SYNC_PACKAGE_DIR = "/pyhiveapi/"
ASYNC_PACKAGE_NAME = "apyhiveapi"
ASYNC_PACKAGE_DIR = "/apyhiveapi/"
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


def _diagnostic_sensor(ha_name: str, hive_type: str) -> EntityConfig:
    """Return the config for a diagnostic sensor entity."""
    return EntityConfig(
        entity_type="sensor",
        ha_name=ha_name,
        hive_type=hive_type,
        category="diagnostic",
    )


def _heating_sensors() -> list[EntityConfig]:
    """Return the diagnostic sensors shared by heating zones and TRV controls."""
    return [
        _diagnostic_sensor(" Current Temperature", "Heating_Current_Temperature"),
        _diagnostic_sensor(" Target Temperature", "Heating_Target_Temperature"),
        _diagnostic_sensor(" State", "Heating_State"),
        _diagnostic_sensor(" Mode", "Heating_Mode"),
        _diagnostic_sensor(" Boost", "Heating_Boost"),
    ]


def _light_entities() -> list[EntityConfig]:
    """Return the entities created for every type of Hive light."""
    return [
        EntityConfig(entity_type="light"),
        _diagnostic_sensor(" Mode", "Mode"),
        _diagnostic_sensor(" Availability", "Availability"),
    ]


def _battery_and_availability_sensors() -> list[EntityConfig]:
    """Return the sensors created for battery-powered devices."""
    return [
        _diagnostic_sensor(" Battery Level", "Battery"),
        _diagnostic_sensor(" Availability", "Availability"),
    ]


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
            category="config",
        ),
        *_heating_sensors(),
    ],
    "trvcontrol": [EntityConfig(entity_type="climate"), *_heating_sensors()],
    "hotwater": [
        EntityConfig(entity_type="water_heater"),
        _diagnostic_sensor("Hotwater State", "Hotwater_State"),
        _diagnostic_sensor("Hotwater Mode", "Hotwater_Mode"),
        _diagnostic_sensor("Hotwater Boost", "Hotwater_Boost"),
    ],
    "activeplug": [
        EntityConfig(entity_type="switch"),
        _diagnostic_sensor(" Mode", "Mode"),
        _diagnostic_sensor(" Availability", "Availability"),
        _diagnostic_sensor(" Power", "Power"),
    ],
    "warmwhitelight": _light_entities(),
    "tuneablelight": _light_entities(),
    "colourtuneablelight": _light_entities(),
    "motionsensor": [
        EntityConfig(entity_type="binary_sensor"),
        _diagnostic_sensor(" Current Temperature", "Current_Temperature"),
    ],
    "contactsensor": [
        EntityConfig(entity_type="binary_sensor"),
    ],
}

DEVICES = {
    "contactsensor": _battery_and_availability_sensors(),
    "hub": [
        EntityConfig(
            entity_type="binary_sensor",
            ha_name="Hive Hub Status",
            hive_type="Connectivity",
            category="diagnostic",
        ),
    ],
    "motionsensor": _battery_and_availability_sensors(),
    "sense": [
        EntityConfig(
            entity_type="binary_sensor",
            ha_name="Hive Hub Status",
            hive_type="Connectivity",
        ),
    ],
    "thermostatui": _battery_and_availability_sensors(),
    "trv": _battery_and_availability_sensors(),
}
