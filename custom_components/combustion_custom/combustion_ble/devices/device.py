from datetime import datetime
from typing import TYPE_CHECKING, Optional

from ..exceptions import DFUNotImplementedError
from ..utilities.asyncio_utils import ensure_future
from ..utilities.monitor import Monitorable, RemoveListener, UpdateListener

if TYPE_CHECKING:
    from ..device_manager import DeviceManager


class Device:
    MIN_RSSI = -128
    # BLE advertisements can be bursty on some hosts; 15s was causing HA availability flapping.
    STALE_TIMEOUT = 60.0

    class ConnectionState:
        DISCONNECTED = "disconnected"
        CONNECTING = "connecting"
        CONNECTED = "connected"
        FAILED = "failed"

    class DFUErrorMessage:
        def __init__(self, error, message):
            self.error = error
            self.message = message

    class DFUUploadProgress:
        def __init__(self, part, total_parts, progress):
            self.part = part
            self.total_parts = total_parts
            self.progress = progress

    def __init__(
        self,
        unique_identifier: str,
        device_manager: "DeviceManager",
        ble_identifier: Optional[str] = None,
        rssi=None,
    ):
        self.unique_identifier: str = unique_identifier
        self.ble_identifier: Optional[str] = ble_identifier if ble_identifier else None
        self._rssi: Monitorable[int] = Monitorable(rssi if rssi is not None else self.MIN_RSSI)
        self.firmware_version: Optional[str] = None
        self.hardware_revision: Optional[str] = None
        self.sku: Optional[str] = None
        self.manufacturing_lot: Optional[str] = None
        self.connection_state: str = Device.ConnectionState.DISCONNECTED
        self.is_connectable: bool = False
        self.maintaining_connection: bool = False
        self.stale: bool = False
        self.dfu_state = None
        self.dfu_error = None
        self.dfu_upload_progress = None
        self.last_update_time: datetime = datetime.now()
        self.dfu_service_controller = None
        self.device_manager: "DeviceManager" = device_manager

    @property
    def rssi(self) -> int:
        """The current RSSI."""
        return self._rssi.value

    def add_rssi_listener(self, listener: UpdateListener[int]) -> RemoveListener:
        """Add a listener for RSSI changes."""
        return self._rssi.add_update_listener(listener)

    def _update_connection_state(self, state: str):
        self.connection_state = state

        if self.connection_state == Device.ConnectionState.DISCONNECTED:
            self.firmware_version = None

        # Important: do NOT auto-retry immediately on FAILED.
        # A FAILED connect (often due to non-connectable advertising / device busy) can otherwise
        # cause an infinite reconnect loop that starves scanning and makes HA sensors flap.
        if self.maintaining_connection and self.connection_state == Device.ConnectionState.DISCONNECTED:
            ensure_future(self.connect(), name="device.connect[update_connection_state]")

    def _update_device_stale(self):
        self.stale = (datetime.now() - self.last_update_time).total_seconds() > self.STALE_TIMEOUT
        if self.stale:
            self.is_connectable = False

    def is_dfu_running(self) -> bool:
        if not self.dfu_state:
            return False

        raise DFUNotImplementedError()

    def dfu_complete(self):
        raise DFUNotImplementedError()

    def update_with_model_info(self, model_info: str):
        # Parse the SKU and lot number, which are delimited by a ':'
        parts = model_info.split(":")
        if len(parts) == 2:
            self.sku = parts[0]
            self.manufacturing_lot = parts[1]

    async def connect(self):
        self.maintaining_connection = True

        if self.connection_state != Device.ConnectionState.CONNECTED:
            await self.device_manager._connect_to_device(self)

    async def disconnect(self):
        self.maintaining_connection = False
        await self.device_manager._disconnect_from_device(self)

    def run_software_upgrade(self, dfu_file):
        raise DFUNotImplementedError()

    def dfu_state_did_change(self, state):
        raise DFUNotImplementedError()

    def dfu_error_did_occur(self, error, message):
        raise DFUNotImplementedError()

    def dfu_progress_did_change(self, part, total_parts, progress):
        raise DFUNotImplementedError()

    def log_with_level(self, level, message):
        raise DFUNotImplementedError()

    # Hashable implementation
    def __hash__(self):
        return hash(self.unique_identifier)

    def __eq__(self, other: object):
        if not isinstance(other, Device):
            return False
        return self.unique_identifier == other.unique_identifier if other else False
