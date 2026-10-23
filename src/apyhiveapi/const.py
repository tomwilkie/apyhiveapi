"""Constants for apyhiveapi."""

SMS_REQUIRED = "SMS_MFA"

# Hive product types and the Home Assistant entity type each one becomes.
PRODUCT_ENTITY_TYPES = {
    "heating": "climate",
    "trvcontrol": "climate",
    "hotwater": "water_heater",
}

# Hive device types that carry the thermostat hardware details for a heating
# or hot water product in the same zone.
THERMOSTAT_DEVICE_TYPES = ("thermostatui", "trv")
