import threading
import time
from bleak import BleakScanner
from .combustion_ble.ble_manager import BluetoothMode
from .combustion_ble.device_manager import DeviceManager
from .combustion_ble.devices.device import Device

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import dispatcher_send

from .const import EVENT_DISCOVERED


class MeatNetManager:

    hass: HomeAssistant
    devices: dict[str, Device] = {}
    thread: threading.Thread
    running: False
    deviceManager: DeviceManager
    scanner: BleakScanner

    # Track when a BLE address transitions from generic node -> gauge
    _device_kind: dict[str, str]

    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self._device_kind = {}

    def worker(self):

        while self.running:
            time.sleep(0.5)

            devices = self.deviceManager.get_devices()
            for device in devices:
                id = device.unique_identifier
                try:
                    is_gauge = bool(getattr(device, "gauge_serial", None))
                    kind = "gauge" if is_gauge else "device"

                    if self.devices.get(id) is None:
                        dispatcher_send(self.hass, EVENT_DISCOVERED, device)
                        self.devices[id] = device
                        self._device_kind[id] = kind
                        continue

                    # If a MeatNet node later reveals itself to be a Gauge, re-dispatch so
                    # the HA platform can add Gauge-only entities.
                    if self._device_kind.get(id) != "gauge" and kind == "gauge":
                        dispatcher_send(self.hass, EVENT_DISCOVERED, device)
                        self._device_kind[id] = "gauge"
                except Exception:
                    pass

    async def async_start(self) -> None:

        if (DeviceManager.shared is not None):
            self.deviceManager = DeviceManager.shared
        else:
            self.deviceManager = DeviceManager()

        self.deviceManager.enable_meatnet()
        detection_callback = await self.deviceManager.init_bluetooth(mode=BluetoothMode.PASSIVE)

        self.scanner = BleakScanner(detection_callback=detection_callback)
        await self.scanner.start()

        self.thread = threading.Thread(target=self.worker)
        self.running = True
        self.thread.start()

    async def async_stop(self) -> None:

        try:
            self.running = False
            await self.deviceManager.async_stop()
            await self.scanner.stop()
            self.thread.join(2)
            self.devices.clear()
            self._device_kind.clear()
        except:
            pass
