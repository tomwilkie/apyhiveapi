"""Constants for apyhiveapi."""

SMS_REQUIRED = "SMS_MFA"

HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403

# device_data must hold at least device_group_key, device_key and device_password.
EXPECTED_DEVICE_DATA_LENGTH = 3

# Hive product types and the Home Assistant entity type each one becomes.
PRODUCT_ENTITY_TYPES = {
    "heating": "climate",
    "trvcontrol": "climate",
    "hotwater": "water_heater",
}

# Hive device types that carry the thermostat hardware details for a heating
# or hot water product in the same zone.
THERMOSTAT_DEVICE_TYPES = ("thermostatui", "trv")
