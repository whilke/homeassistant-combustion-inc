import enum
from typing import Optional

from bleak import (
    AdvertisementDataCallback,
    BleakClient,
    BleakError,
    BleakGATTCharacteristic,
    BleakScanner,
)
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak_retry_connector import establish_connection

from .ble_data.advertising_data import AdvertisingData, CombustionProductType
from .ble_data.gauge_advertising_data import GaugeAdvertisingData
from .ble_data.probe_status import ProbeStatus
from .const import (
    BT_MANUFACTURER_ID,
    DEVICE_STATUS_CHARACTERISTIC,
    FW_VERSION_CHARACTERISTIC,
    HW_VERSION_CHARACTERISTIC,
    MODEL_NUMBER_CHARACTERISTIC,
    SERIAL_NUMBER_CHARACTERISTIC,
    UART_RX_CHARACTERISTIC,
    UART_TX_CHARACTERISTIC,
)
from .exceptions import CombustionError
from .logger import LOGGER
from .uart import Request, SessionInfoRequest
from .uart.meatnet import NodeRequest
from .utilities.asyncio_utils import ensure_future


class BleManagerDelegate:
    def did_connect_to(self, identifier: str):
        pass

    def did_fail_to_connect_to(self, identifier: str):
        pass

    def did_disconnect_from(self, identifier: str):
        pass

    def handle_bootloader_advertising(self, advertising_name: str, rssi: int, identifier: str):
        pass

    def update_device_with_advertising(
        self, advertising: AdvertisingData, is_connectable: bool, rssi: int, identifier: str
    ):
        pass

    def update_device_with_gauge_advertising(
        self, advertising: GaugeAdvertisingData, is_connectable: bool, rssi: int, identifier: str
    ):
        pass

    def update_device_with_status(self, identifier: str, status: ProbeStatus):
        pass

    def handle_uart_data(self, identifier: str, data: bytes):
        pass

    def update_device_fw_version(self, identifier: str, fw_version: str):
        pass

    def update_device_hw_revision(self, identifier: str, hw_revision: str):
        pass

    def update_device_serial_number(self, identifier: str, serial_number: str):
        pass

    def update_device_model_info(self, identifier: str, model_info: str):
        pass


class BluetoothMode(enum.Enum):
    """Mode for bluetooth device discovery."""

    ACTIVE = "active"
    """Active will initialize a BleakScanner to discover devices automatically."""

    PASSIVE = "passive"
    """Passive will allow you to interface this SDK with an externally-managed BleakScanner."""


class PendingGattReads:
    """Track pending GATT Read requests."""

    def __init__(self) -> None:
        self.pending_reads: dict[str, set[int]] = {}

    def add(self, identifier: str, char: BleakGATTCharacteristic):
        if identifier not in self.pending_reads:
            self.pending_reads[identifier] = set[int]()
        reads: set[int] = self.pending_reads.get(identifier, set[int]())
        reads.add(char.handle)

    def remove(self, identifier: str, char: BleakGATTCharacteristic | None):
        if identifier not in self.pending_reads or char is None:
            return
        reads: set[int] = self.pending_reads.get(identifier, set[int]())
        reads.discard(char.handle)

    def has(self, identifier: str, char: BleakGATTCharacteristic | None) -> bool:
        if identifier not in self.pending_reads or char is None:
            return False
        reads: set[int] = self.pending_reads.get(identifier, set[int]())
        return char.handle in reads


class BleManager:
    shared: "BleManager" = None  # type: ignore

    def __init__(self):
        self.clients: dict[str, BleakClient] = {}
        self.ble_devices: dict[str, BLEDevice] = {}
        self.scanner: Optional[BleakScanner] = None
        self.delegate: Optional[BleManagerDelegate] = None
        self.device_status_characteristics: dict[str, BleakGATTCharacteristic] = {}
        self.uart_characteristics: dict[str, BleakGATTCharacteristic] = {}
        self.fw_revision_characteristics: dict[str, BleakGATTCharacteristic] = {}
        self.hw_revision_characteristics: dict[str, BleakGATTCharacteristic] = {}
        self.serial_number_characteristics: dict[str, BleakGATTCharacteristic] = {}
        self.model_number_characteristics: dict[str, BleakGATTCharacteristic] = {}
        self._pending_gatt_reads = PendingGattReads()
        self._pending_connections: set[str] = set()
        self.is_stopping = False

    async def init_bluetooth(
        self, mode: BluetoothMode = BluetoothMode.ACTIVE
    ) -> None | AdvertisementDataCallback:
        """Initialize Bluetooth"""
        if mode == BluetoothMode.ACTIVE:
            await self.init_bluetooth_scanning()
            return None
        elif mode == BluetoothMode.PASSIVE:
            return self.detection_callback

        raise TypeError("Unsupported mode: %s", mode)

    async def init_bluetooth_scanning(self):
        if self.scanner:
            raise CombustionError("Bluetooth has already been initialized.")
        if self.is_stopping:
            raise CombustionError("Cannot initialize bluetooth while it is stopping.")

        LOGGER.debug("Initializing bluetooth with our own BleakScanner.")
        self.scanner = BleakScanner(detection_callback=self.detection_callback)
        await self.scanner.start()

    async def stop_bluetooth(self):
        self.is_stopping = True
        if self.scanner:
            try:
                await self.scanner.stop()
            except Exception:
                LOGGER.exception("Error stopping Bleak scanner")
            if self.clients:
                client_list = [c for c in self.clients]
                for client in client_list:
                    try:
                        if self.clients[client].is_connected:
                            await self.clients[client].disconnect()
                    except Exception:
                        LOGGER.exception("Error disconnecting client")
        self.clients = {}
        self._pending_connections = set()
        self._pending_gatt_reads = PendingGattReads()
        self.scanner = None
        self.is_stopping = False

    def detection_callback(self, device: BLEDevice, advertisement_data: AdvertisementData):
        if BT_MANUFACTURER_ID not in advertisement_data.manufacturer_data:
            return

        # Store the BLEDevice for use with establish_connection
        self.ble_devices[device.address] = device

        msd_payload = advertisement_data.manufacturer_data[BT_MANUFACTURER_ID]

        # Peek product type byte (offset 2 in full MSD, but Bleak omits vendor id).
        # Full MSD format: [vendor_id(2)][product_type(1)]...
        if len(msd_payload) < 1:
            return

        product_type_byte = msd_payload[0]
        if product_type_byte == CombustionProductType.GAUGE.value:
            gauge_adv = GaugeAdvertisingData.from_bleak_data(msd_payload)
            if gauge_adv and self.delegate:
                self.delegate.update_device_with_gauge_advertising(
                    advertising=gauge_adv,
                    is_connectable=True,  # TODO: support non-connectable devices
                    rssi=advertisement_data.rssi,
                    identifier=device.address,
                )
            return

        advertising_data = AdvertisingData.from_bleak_data(msd_payload)
        if advertising_data and self.delegate:
            self.delegate.update_device_with_advertising(
                advertising=advertising_data,
                is_connectable=True,  # TODO: support non-connectable devices
                rssi=advertisement_data.rssi,
                identifier=device.address,
            )

    async def connect(self, identifier: str):
        if not self.delegate:
            return
        if identifier in self._pending_connections:
            return

        if identifier in self.clients:
            client = self.clients[identifier]
            if client.is_connected:
                self.delegate.did_connect_to(identifier)
                return

        ble_device = self.ble_devices.get(identifier)
        if not ble_device:
            LOGGER.warning("No BLEDevice found for identifier [%s]", identifier)
            self.delegate.did_fail_to_connect_to(identifier)
            return

        successful = False
        self._pending_connections.add(identifier)
        try:
            client = await establish_connection(
                BleakClient,
                ble_device,
                ble_device.name or identifier,
                disconnected_callback=self.disconnected_callback(identifier),
            )
            self.clients[identifier] = client
            successful = True
        except Exception as ex:
            LOGGER.debug("Failed to connect to [%s]: %s", identifier, ex)
            self.delegate.did_fail_to_connect_to(identifier)
        finally:
            self._pending_connections.discard(identifier)

        if successful:
            self.delegate.did_connect_to(identifier)
            self.handle_discovered_services(identifier, client)

    def disconnected_callback(self, identifier: str):
        def cb(client: BleakClient):
            if self.delegate:
                self.delegate.did_disconnect_from(identifier)
            if identifier in self.clients:
                del self.clients[identifier]
            if identifier in self.uart_characteristics:
                del self.uart_characteristics[identifier]
            if identifier in self.device_status_characteristics:
                del self.device_status_characteristics[identifier]
            if identifier in self.fw_revision_characteristics:
                del self.fw_revision_characteristics[identifier]
            if identifier in self.hw_revision_characteristics:
                del self.hw_revision_characteristics[identifier]
            if identifier in self.serial_number_characteristics:
                del self.serial_number_characteristics[identifier]
            if identifier in self.model_number_characteristics:
                del self.model_number_characteristics[identifier]

        return cb

    async def disconnect(self, identifier: str):
        if identifier in self.clients:
            client = self.clients[identifier]
            if client.is_connected:
                try:
                    await client.disconnect()
                except BleakError as be:
                    LOGGER.error("Error disconnecting from [%s]: %s", identifier, be)
                # Additional cleanup handled by disconnected_callback

    async def send_request(
        self, identifier: str, request: Request | NodeRequest
    ):  # todo this does not need to be async. we can ensure_future instead
        connection_peripheral = self.get_connected_peripheral(identifier)
        if not connection_peripheral:
            return

        uart_char = self.uart_characteristics.get(identifier)
        client = self.clients.get(identifier)
        try:
            if client and client.is_connected and uart_char:
                await client.write_gatt_char(uart_char, request.data, response=False)
        except BleakError as be:
            LOGGER.error("Error sending request to [%s]: %s", identifier, be)

    async def read_firmware_revision(self, identifier: str) -> None:
        connection_peripheral = self.get_connected_peripheral(identifier)
        if connection_peripheral:
            uart_char = self.fw_revision_characteristics.get(identifier)
            client = self.clients.get(identifier)
            if self._pending_gatt_reads.has(identifier, uart_char):
                LOGGER.debug(
                    "Discarding concurent request to read_firmware_revision for [%s]", identifier
                )
                return
            try:
                if client and client.is_connected and uart_char and self.delegate:
                    self._pending_gatt_reads.add(identifier, uart_char)
                    data = await client.read_gatt_char(uart_char, use_cached=True)
                    fw_version = data.decode(encoding="utf-8")
                    self.delegate.update_device_fw_version(identifier, fw_version)
            except BleakError as be:
                LOGGER.error("Error reading firmware version from [%s]: %s", identifier, be)
            finally:
                self._pending_gatt_reads.remove(identifier, uart_char)

    async def read_hardware_revision(self, identifier: str) -> None:
        connection_peripheral = self.get_connected_peripheral(identifier)
        if connection_peripheral:
            uart_char = self.hw_revision_characteristics.get(identifier)
            client = self.clients.get(identifier)
            if self._pending_gatt_reads.has(identifier, uart_char):
                LOGGER.debug(
                    "Discarding concurent request to read_hardware_revision for [%s]", identifier
                )
                return
            try:
                if client and client.is_connected and uart_char and self.delegate:
                    self._pending_gatt_reads.add(identifier, uart_char)
                    data = await client.read_gatt_char(uart_char, use_cached=True)
                    hw_revision = data.decode(encoding="utf-8")
                    self.delegate.update_device_hw_revision(identifier, hw_revision)
            except BleakError as be:
                LOGGER.debug("Error reading hardware version from [%s]: %s", identifier, be)
            finally:
                self._pending_gatt_reads.remove(identifier, uart_char)

    async def read_serial_number(self, identifier: str) -> None:
        connection_peripheral = self.get_connected_peripheral(identifier)
        if connection_peripheral:
            uart_char = self.serial_number_characteristics.get(identifier)
            client = self.clients.get(identifier)
            if self._pending_gatt_reads.has(identifier, uart_char):
                LOGGER.debug(
                    "Discarding concurent request to read_serial_number for [%s]", identifier
                )
                return
            try:
                if client and client.is_connected and uart_char and self.delegate:
                    self._pending_gatt_reads.add(identifier, uart_char)
                    data = await client.read_gatt_char(uart_char, use_cached=True)
                    serial_number = data.decode(encoding="utf-8")
                    self.delegate.update_device_serial_number(identifier, serial_number)
            except BleakError as be:
                LOGGER.debug("Error reading serial number from [%s]: %s", identifier, be)
            finally:
                self._pending_gatt_reads.remove(identifier, uart_char)

    async def read_model_number(self, identifier: str) -> None:
        connection_peripheral = self.get_connected_peripheral(identifier)
        if connection_peripheral:
            uart_char = self.model_number_characteristics.get(identifier)
            client = self.clients.get(identifier)
            if self._pending_gatt_reads.has(identifier, uart_char):
                LOGGER.debug(
                    "Discarding concurent request to read_model_number for [%s]", identifier
                )
                return
            try:
                if client and client.is_connected and uart_char and self.delegate:
                    self._pending_gatt_reads.add(identifier, uart_char)
                    data = await client.read_gatt_char(uart_char, use_cached=True)
                    model_number = data.decode(encoding="utf-8")
                    self.delegate.update_device_model_info(identifier, model_number)
            except BleakError as be:
                LOGGER.debug("Error reading model number from [%s]: %s", identifier, be)
            finally:
                self._pending_gatt_reads.remove(identifier, uart_char)

    def get_connected_peripheral(self, identifier: str) -> BleakClient | None:
        connected_clients = [
            self.clients[c]
            for c in self.clients
            if self.clients[c].address == identifier and self.clients[c].is_connected
        ]
        if not connected_clients:
            return None

        return connected_clients[0]

    def handle_uart_data(self, identifier: str, data: bytes):
        if self.delegate:
            self.delegate.handle_uart_data(identifier, data)

    def handle_discovered_services(self, identifier: str, client: BleakClient):
        def uart_tx_notify_callback(char: BleakGATTCharacteristic, data: bytearray):
            if char.uuid == UART_TX_CHARACTERISTIC:
                self.handle_uart_data(identifier, bytes(data))
            elif char.uuid == DEVICE_STATUS_CHARACTERISTIC:
                probe_status = ProbeStatus.from_data(data)
                if probe_status and self.delegate:
                    self.delegate.update_device_with_status(identifier, probe_status)
            else:
                LOGGER.debug("uart_tx_notify_callback ignoring unknown char [%s]", char.uuid)

        for service in client.services:
            for characteristic in service.characteristics:
                if characteristic.uuid == UART_RX_CHARACTERISTIC:
                    self.uart_characteristics[identifier] = characteristic
                elif characteristic.uuid == DEVICE_STATUS_CHARACTERISTIC:
                    self.device_status_characteristics[identifier] = characteristic
                elif (
                    characteristic.uuid == FW_VERSION_CHARACTERISTIC
                    or characteristic.uuid == HW_VERSION_CHARACTERISTIC
                    or characteristic.uuid == SERIAL_NUMBER_CHARACTERISTIC
                    or characteristic.uuid == MODEL_NUMBER_CHARACTERISTIC
                ):
                    if characteristic.uuid == FW_VERSION_CHARACTERISTIC:
                        self.fw_revision_characteristics[identifier] = characteristic
                        ensure_future(
                            self.read_firmware_revision(identifier),
                            name="ble_manager[read_firmware_revision]",
                        )
                    elif characteristic.uuid == HW_VERSION_CHARACTERISTIC:
                        self.hw_revision_characteristics[identifier] = characteristic
                        ensure_future(
                            self.read_hardware_revision(identifier),
                            name="ble_manager[read_hardware_revision]",
                        )
                    elif characteristic.uuid == SERIAL_NUMBER_CHARACTERISTIC:
                        self.serial_number_characteristics[identifier] = characteristic
                        ensure_future(
                            self.read_serial_number(identifier),
                            name="ble_manager[read_serial_number]",
                        )
                    elif characteristic.uuid == MODEL_NUMBER_CHARACTERISTIC:
                        self.model_number_characteristics[identifier] = characteristic
                        ensure_future(
                            self.read_model_number(identifier),
                            name="ble_manager[read_model_number]",
                        )
                elif characteristic.uuid == UART_TX_CHARACTERISTIC:
                    if characteristic.descriptors:
                        client = self.get_connected_peripheral(identifier)
                        if client:
                            ensure_future(
                                client.start_notify(characteristic, uart_tx_notify_callback),
                                name="ble_manager[start_notify:uart_tx]",
                            )
                            status_char = self.device_status_characteristics.get(identifier)
                            if status_char:
                                ensure_future(
                                    client.start_notify(status_char, uart_tx_notify_callback),
                                    name="ble_manager[start_notify:device_status]",
                                )
                                ensure_future(
                                    self.send_request(identifier, request=SessionInfoRequest()),
                                    name="ble_manager[send_request:session_info]",
                                )


# Instantiate the BleManager singleton
BleManager.shared = BleManager()
