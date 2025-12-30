from typing import TYPE_CHECKING
from datetime import datetime

from ..ble_data.advertising_data import AdvertisingData
from ..ble_data.gauge_advertising_data import GaugeAdvertisingData
from ..devices.device import Device
from ..dfu_manager import DFUDeviceType

if TYPE_CHECKING:
    from ..device_manager import DeviceManager
    from .probe import Probe


class MeatNetNode(Device):
    def __init__(
        self,
        advertising: AdvertisingData | None,
        device_manager: "DeviceManager",
        is_connectable: bool,
        rssi: int,
        identifier: str,
    ):
        super().__init__(
            unique_identifier=str(identifier),
            device_manager=device_manager,
            ble_identifier=identifier,
            rssi=rssi,
        )
        self.serial_number_string = None
        self.probes: dict[int, Probe] = {}
        self.dfu_type = DFUDeviceType.UNKNOWN

        # Gauge-specific fields (if this node is a Gauge)
        self.gauge_serial: str | None = None
        self.gauge_temperature_c: float | None = None
        self.gauge_sensor_present: bool | None = None
        self.gauge_sensor_overheating: bool | None = None
        self.gauge_low_battery: bool | None = None
        self.gauge_alarm_high_raw: int | None = None
        self.gauge_alarm_low_raw: int | None = None

        if advertising is not None:
            self.update_with_advertising(advertising, is_connectable, rssi)
        else:
            self._rssi.update(rssi)
            self.is_connectable = is_connectable
            self.last_update_time = datetime.now()

    def update_with_advertising(
        self, advertising: AdvertisingData, is_connectable: bool, rssi: int
    ):
        self._rssi.update(rssi)
        self.is_connectable = is_connectable
        self.last_update_time = datetime.now()

    def update_with_gauge_advertising(
        self, advertising: GaugeAdvertisingData, is_connectable: bool, rssi: int
    ):
        """Update this node with Gauge-specific advertising data."""
        self._rssi.update(rssi)
        self.is_connectable = is_connectable
        self.last_update_time = datetime.now()

        self.gauge_serial = advertising.serial
        self.gauge_temperature_c = advertising.temperature_c
        self.gauge_sensor_present = advertising.sensor_present
        self.gauge_sensor_overheating = advertising.sensor_overheating
        self.gauge_low_battery = advertising.low_battery
        self.gauge_alarm_high_raw = advertising.alarm_high_raw
        self.gauge_alarm_low_raw = advertising.alarm_low_raw

    def update_networked_probe(self, probe: "Probe"):
        if probe is not None:
            self.probes[probe.serial_number] = probe

    def has_connection_to_probe(self, serial_number: int):
        return serial_number in self.probes or str(serial_number) in self.probes  # todo ... yucky

    def update_with_model_info(self, model_info: str):
        super().update_with_model_info(model_info)
        if "Timer" in model_info:
            self.dfu_type = "display"
        elif "Charger" in model_info:
            self.dfu_type = "charger"

    def __str__(self):
        return f"MeatNetNode: {self.unique_identifier}"
