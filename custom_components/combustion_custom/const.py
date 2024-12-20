from enum import Enum


DOMAIN = "combustion"
EVENT_DISCOVERED = DOMAIN + ".discovered"
EVENT_REFRESH = DOMAIN + ".refresh"

CONF_TIMEOUT = "timeout"


class TempUnit(Enum):
    CELSIUS = "Celsius"
    FAHRENHEIT = "Fahrenheit"