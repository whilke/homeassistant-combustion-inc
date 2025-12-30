import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from .devices.device import Device
from .logger import LOGGER
from .utilities.asyncio_utils import ensure_future

if TYPE_CHECKING:
    from .device_manager import DeviceManager
    from .devices.meat_net_node import MeatNetNode
    from .devices.probe import Probe


class ConnectionManager:
    def __init__(self, device_manager: "DeviceManager"):
        self.meat_net_enabled = False
        self.dfu_mode_enabled = False
        self.connection_timers: dict[str, asyncio.Task] = {}
        self.last_status_update: dict[str, datetime] = {}
        self.PROBE_STATUS_STALE_TIMEOUT = 10.0
        # Cooldown windows to avoid hammering the BLE stack on repeated failures.
        self._probe_connect_backoff_until: dict[str, datetime] = {}
        self._node_connect_backoff_until: dict[str, datetime] = {}
        self.CONNECT_FAILURE_BACKOFF_SECONDS = 5.0
        self.device_manager = device_manager

    def _in_backoff(self, backoff_map: dict[str, datetime], key: str) -> bool:
        until = backoff_map.get(key)
        return bool(until and datetime.now() < until)

    def note_connect_failed(self, device: Device) -> None:
        until = datetime.now() + timedelta(seconds=self.CONNECT_FAILURE_BACKOFF_SECONDS)

        if hasattr(device, "serial_number_string"):
            serial = getattr(device, "serial_number_string")
            if isinstance(serial, str):
                self._probe_connect_backoff_until[serial] = until
                LOGGER.debug(
                    "Backoff probe connect (%s) for %ss",
                    serial,
                    self.CONNECT_FAILURE_BACKOFF_SECONDS,
                )
                return

        # Fallback: use BLE identifier / unique identifier for nodes
        key = device.ble_identifier or device.unique_identifier
        self._node_connect_backoff_until[key] = until
        LOGGER.debug(
            "Backoff node connect (%s) for %ss",
            key,
            self.CONNECT_FAILURE_BACKOFF_SECONDS,
        )

    def note_connect_succeeded(self, device: Device) -> None:
        if hasattr(device, "serial_number_string"):
            serial = getattr(device, "serial_number_string")
            if isinstance(serial, str) and serial in self._probe_connect_backoff_until:
                del self._probe_connect_backoff_until[serial]
                return

        key = device.ble_identifier or device.unique_identifier
        if key in self._node_connect_backoff_until:
            del self._node_connect_backoff_until[key]

    def received_probe_advertising(self, probe: Optional["Probe"]):
        if probe is None:
            return

        # Avoid repeated connect storms if the probe (or the host) is currently refusing connects.
        if self._in_backoff(self._probe_connect_backoff_until, probe.serial_number_string):
            return

        probe_status_stale = True

        if probe.serial_number_string in self.last_status_update:
            last_update_time = self.last_status_update[probe.serial_number_string]
            probe_status_stale = (
                datetime.now() - last_update_time
            ).total_seconds() > self.PROBE_STATUS_STALE_TIMEOUT

        if self.dfu_mode_enabled:
            ensure_future(probe.connect(), "probe.connect[dfu]")
        elif (
            self.meat_net_enabled
            and probe_status_stale
            and probe.connection_state != "connected"
            and probe.serial_number_string not in self.connection_timers
        ):
            self.connection_timers[probe.serial_number_string] = asyncio.create_task(
                self.connect_probe_after_delay(probe)
            )

    def clear_handlers_for_probe(self, probe: "Probe", msg: str = "None"):
        """Clear all handlers for the given probe."""
        if probe.serial_number_string in self.connection_timers:
            self.connection_timers[probe.serial_number_string].cancel(msg)
            del self.connection_timers[probe.serial_number_string]

    async def connect_probe_after_delay(self, probe: "Probe"):
        await asyncio.sleep(3)
        updated_probe = self.get_probe_with_serial(probe.serial_number_string)
        if updated_probe:
            await updated_probe.connect()
        del self.connection_timers[probe.serial_number_string]

    def received_probe_advertising_from_node(self, probe: Optional["Probe"], node: "MeatNetNode"):
        if self.meat_net_enabled:
            node_key = node.ble_identifier or node.unique_identifier
            if self._in_backoff(self._node_connect_backoff_until, node_key):
                return
            ensure_future(node.connect(), "probe.connect[meatnet]")

    def received_status_for(self, probe: "Probe", direct_connection: bool):
        self.last_status_update[probe.serial_number_string] = datetime.now()

        if not direct_connection and self.meat_net_enabled and not self.dfu_mode_enabled:
            updated_probe = self.get_probe_with_serial(probe.serial_number_string)
            if updated_probe and updated_probe.connection_state == Device.ConnectionState.CONNECTED:
                ensure_future(updated_probe.disconnect(), "probe.disconnect[prefer_meatnet]")

    def get_probe_with_serial(self, serial: str) -> Optional["Probe"]:
        probes = self.device_manager.get_probes()
        return next((probe for probe in probes if probe.serial_number_string == serial), None)
