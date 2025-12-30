"""Device Manager."""

import asyncio
from typing import Callable, Optional

from bleak import AdvertisementDataCallback

from .ble_data.advertising_data import (
    AdvertisingData,
    CombustionProductType,
)
from .ble_data.gauge_advertising_data import GaugeAdvertisingData
from .ble_data.hop_count import HopCount
from .ble_data.probe_status import ProbeStatus
from .ble_manager import BleManager, BleManagerDelegate, BluetoothMode
from .connection_manager import ConnectionManager
from .devices.device import Device
from .devices.meat_net_node import MeatNetNode
from .devices.probe import Probe
from .exceptions import DFUNotImplementedError
from .logger import LOGGER
from .message_handlers import MessageHandlers
from .uart import (
    LogRequest,
    LogResponse,
    ReadOverTemperatureRequest,
    ReadOverTemperatureResponse,
    Response,
    SessionInfoRequest,
    SessionInfoResponse,
    SessionInformation,
    SetColorResponse,
    SetIDResponse,
    SetPredictionResponse,
    responses_from_data,
)
from .uart.meatnet import (
    NodeProbeStatusRequest,
    NodeReadFirmwareRevisionRequest,
    NodeReadFirmwareRevisionResponse,
    NodeReadHardwareRevisionRequest,
    NodeReadHardwareRevisionResponse,
    NodeReadLogsRequest,
    NodeReadLogsResponse,
    NodeReadModelInfoRequest,
    NodeReadModelInfoResponse,
    NodeReadSessionInfoRequest,
    NodeReadSessionInfoResponse,
    NodeRequest,
    NodeResponse,
    NodeSetPredictionResponse,
    NodeUARTMessage,
)

DeviceListener = Callable[[list[Device], list[Device]], None]


class DeviceManager(BleManagerDelegate):
    """
    Primary interface for Combustion BLE Devices.

    This class is designed to be a singleton.

    **Example usage:**

    .. code-block:: python
        :linenos:

        async def async_setup(self):
            self.device_manager = DeviceManager()
            self.device_manager.enable_meatnet() # Optionally enable MeatNet
            await self.device_manager.init_bluetooth()

        def do_the_work(self):
            probes = self.device_manager.get_probes()
            # Have fun with probes.

            metnet_nodes = self.device_manager.get_meatnet_nodes()
            # Have fun with meatnet nodes.

        async def async_shutdown(self):
           await self.device_manager.async_stop()
    """

    MINIMUM_PREDICTION_SETPOINT_CELSIUS = 0.0
    MAXIMUM_PREDICTION_SETPOINT_CELSIUS = 100.0
    INVALID_PROBE_SERIAL_NUMBER = 0

    shared = None

    def __init__(self):
        """Initialize.
        You may only instantiate a single instance of this class within your application.
        """
        if DeviceManager.shared:
            raise RuntimeError("An instance already exists.")
        self.devices: dict[str, Device] = {}
        self.connection_manager = ConnectionManager(self)
        self.message_handlers = MessageHandlers()
        self.device_listeners: list[DeviceListener] = []
        DeviceManager.shared = self
        BleManager.shared.delegate = self
        self.timer_task: asyncio.Task | None = asyncio.create_task(self._start_timers())

    async def init_bluetooth(
        self, mode: BluetoothMode = BluetoothMode.ACTIVE
    ) -> Optional[AdvertisementDataCallback]:
        """Initialize bluetooth operations."""
        return await BleManager.shared.init_bluetooth(mode=mode)

    def add_device_listener(self, listener: DeviceListener) -> None:
        """Add a device listener to be notified when devices are added or removed."""
        self.device_listeners.append(listener)

    def clear_device_listeners(self) -> None:
        """Remove all device listeners."""
        self.device_listeners = []

    async def async_stop(self):
        """Stop all asynchronous tasks and BLE scanning. Must be called prior to terminating your application."""
        if self.timer_task:
            self.timer_task.cancel()
            self.timer_task = None

        # Attempt to disconnect from all devices.
        for key in self.devices:
            try:
                device = self.devices[key]
                await device.disconnect()
                if isinstance(device, Probe):
                    device.stop_session_request_timer()
                    self.connection_manager.clear_handlers_for_probe(
                        device, msg="device_manager::async_stop"
                    )
            except Exception:
                LOGGER.exception(
                    "Error disconnecting client [%s] during DeviceManager shutdown.", device
                )

        try:
            await BleManager.shared.stop_bluetooth()
        except Exception:
            LOGGER.exception("Error stopping BleManager during DeviceManager shutdown.")

    async def _start_timers(self):
        while True:
            self._update_device_stale_status()
            self.message_handlers.check_for_timeout()
            await asyncio.sleep(1)

    def _update_device_stale_status(self):
        # Update the stale status of devices
        for key, device in self.devices.items():
            device._update_device_stale()

    def add_simulated_probe(self):
        # Placeholder for adding a simulated probe
        pass

    def enable_meatnet(self):
        self.connection_manager.meat_net_enabled = True

    def enable_dfu_mode(self, enable):
        raise DFUNotImplementedError()

    def _add_device(self, device: Device):
        self.devices[device.unique_identifier] = device
        for listener in self.device_listeners:
            listener([device], [])

    def _clear_device(self, device: Device):
        if device.unique_identifier in self.devices:
            del self.devices[device.unique_identifier]
            for listener in self.device_listeners:
                listener([], [device])

    def get_probes(self) -> list[Probe]:
        return [device for device in self.devices.values() if isinstance(device, Probe)]

    def get_meatnet_nodes(self):
        if self.connection_manager.meat_net_enabled:
            return [device for device in self.devices.values() if isinstance(device, MeatNetNode)]
        else:
            return []

    def get_nearest_probe(self) -> Optional[Probe]:
        """Returns the probe nearest to this device."""
        probes = self.get_probes()
        nearest = max(probes, key=lambda probe: probe.rssi, default=None)
        return nearest

    def get_devices(self) -> list[Device]:
        return list(self.devices.values())

    def get_nearest_device(self) -> Optional[Device]:
        nearest = max(self.get_devices(), key=lambda device: device.rssi, default=None)
        return nearest

    def _get_best_node_for_probe(self, serial_number: int) -> MeatNetNode | None:
        """Gets the best Node for communicating with a Probe."""
        found_node: MeatNetNode | None = None
        found_rssi: int = Device.MIN_RSSI
        meatnet_nodes = self.get_meatnet_nodes()
        for node in meatnet_nodes:
            # Check nodes to which we are connected to see if they have a route to the probe
            if node.connection_state == Device.ConnectionState.CONNECTED:
                # Choose node with the best RSSI that has a connection to probe
                if node.has_connection_to_probe(serial_number) and node.rssi > found_rssi:
                    found_node = node
                    found_rssi = node.rssi

        return found_node

    def _get_best_route_to_probe(self, serial_number) -> Device | None:
        probe = self.find_probe_by_serial_number(serial_number)
        if probe and probe.connection_state == Probe.ConnectionState.CONNECTED:
            return probe
        else:
            return self._get_best_node_for_probe(serial_number)

    def find_probe_by_serial_number(self, serial_number) -> Probe | None:
        device = self.devices.get(str(serial_number))
        if device and isinstance(device, Probe):
            return device

        return None

    async def _connect_to_device(self, device: Device):
        if device.ble_identifier:
            # If this device has a BLE identifier (advertisements are directly detected rather than through MeatNet), attempt to connect to it.
            device._update_connection_state(Device.ConnectionState.CONNECTING)
            await BleManager.shared.connect(device.ble_identifier)

    async def _disconnect_from_device(self, device: Device):
        if device.ble_identifier:
            # If this device has a BLE identifier (advertisements are directly detected rather than through MeatNet),
            # attempt to disconnect from it.
            await BleManager.shared.disconnect(device.ble_identifier)

    async def request_logs_from(self, device: Device, min_sequence: int, max_sequence: int):
        if isinstance(device, Probe):
            target_device = self._get_best_route_to_probe(device.serial_number)
            if isinstance(target_device, Probe) and target_device.ble_identifier:
                # Request logs directly from Probe
                request = LogRequest(min_sequence=min_sequence, max_sequence=max_sequence - 1)
                await BleManager.shared.send_request(target_device.ble_identifier, request)
            elif isinstance(target_device, MeatNetNode) and target_device.ble_identifier:
                # If the best route is through a Node, send it that way.
                node_request = NodeReadLogsRequest(
                    serial_number=device.serial_number,
                    min_sequence=min_sequence,
                    max_sequence=max_sequence,
                )
                await BleManager.shared.send_request(
                    identifier=target_device.ble_identifier, request=node_request
                )

    def set_probe_id(self, device, id, completion_handler):
        # TODO implement set_probe_id
        raise NotImplementedError()

    def set_probe_color(self, device, color, completion_handler):
        # TODO implement set_probe_color
        raise NotImplementedError()

    def set_removal_prediction(self, device, removal_temperature_c, completion_handler):
        # TODO implement set_removal_prediction
        raise NotImplementedError()

    def cancel_prediction(self, device, completion_handler):
        # TODO implement cancel_prediction
        raise NotImplementedError()

    async def read_session_info(self, probe: Probe):
        target_device = self._get_best_route_to_probe(probe.serial_number)
        if isinstance(target_device, Probe) and target_device.ble_identifier:
            # If the best route is directly to the Probe, send it that way.
            request = SessionInfoRequest()
            await BleManager.shared.send_request(
                identifier=target_device.ble_identifier, request=request
            )
        elif isinstance(target_device, MeatNetNode) and target_device.ble_identifier:
            node_request = NodeReadSessionInfoRequest(serial_number=probe.serial_number)
            await BleManager.shared.send_request(
                identifier=target_device.ble_identifier, request=node_request
            )

    async def read_firmware_version(self, probe: Probe):
        """Sends request to the device to read the probe firmware version."""
        target_device = self._get_best_route_to_probe(probe.serial_number)
        if isinstance(target_device, Probe) and target_device.ble_identifier:
            # If the best route is directly to the Probe, send it that way.
            await BleManager.shared.read_firmware_revision(target_device.ble_identifier)
        elif isinstance(target_device, MeatNetNode) and target_device.ble_identifier:
            # Otherwise, send via MeatNet Node
            request = NodeReadFirmwareRevisionRequest(serial_number=probe.serial_number)
            await BleManager.shared.send_request(
                identifier=target_device.ble_identifier, request=request
            )

    async def read_hardware_version(self, probe: Probe):
        if target_device := self._get_best_route_to_probe(probe.serial_number):
            if isinstance(target_device, Probe) and target_device.ble_identifier:
                # If the best route is directly to the Probe, send it that way.
                await BleManager.shared.read_hardware_revision(target_device.ble_identifier)
            elif isinstance(target_device, MeatNetNode) and target_device.ble_identifier:
                # Otherwise, send via MeatNet Node
                request = NodeReadHardwareRevisionRequest(serial_number=probe.serial_number)
                await BleManager.shared.send_request(
                    identifier=target_device.ble_identifier, request=request
                )

    async def read_model_info_for_probe(self, probe: Probe):
        if target_device := self._get_best_route_to_probe(probe.serial_number):
            if isinstance(target_device, Probe) and target_device.ble_identifier:
                # If the best route is directly to the Probe, send it that way.
                await BleManager.shared.read_model_number(target_device.ble_identifier)
            elif isinstance(target_device, MeatNetNode) and target_device.ble_identifier:
                # Otherwise, send via MeatNet Node
                request = NodeReadModelInfoRequest(serial_number=probe.serial_number)
                await BleManager.shared.send_request(
                    identifier=target_device.ble_identifier, request=request
                )

    async def read_model_info_for_node(self, node: MeatNetNode):
        await BleManager.shared.read_model_number(node.unique_identifier)

    async def read_over_temperature_flag(self, device: Device, completion_handler):
        if isinstance(device, Probe) and device.ble_identifier:
            # Store completion handler
            self.message_handlers.add_read_over_temperature_completion_handler(
                device_identifier=device.unique_identifier, completion_handler=completion_handler
            )

            # Send request to device
            request = ReadOverTemperatureRequest()
            await BleManager.shared.send_request(identifier=device.ble_identifier, request=request)

        # TODO send via node (awaiting upstream implementation)

    def restart_failed_upgrades_with(self, dfu_files):
        raise DFUNotImplementedError()

    def find_device_by_ble_identifier(self, identifier: str) -> Device | None:
        found_device: Device | None = None
        device = self.devices.get(identifier)
        if device:
            # This was a MeatNet Node as it was stored by its BLE UUID.
            found_device = device
        else:
            # Search through Devices to see if any Probes have a matching BLE identifier.
            for device in self.devices.values():
                if device.ble_identifier and device.ble_identifier == identifier:
                    found_device = device
                    break
        return found_device

    # Delegate methods
    def did_connect_to(self, identifier):
        device = self.find_device_by_ble_identifier(identifier)
        if not device:
            return
        device._update_connection_state(Device.ConnectionState.CONNECTED)
        self.connection_manager.note_connect_succeeded(device)

    def did_fail_to_connect_to(self, identifier):
        device = self.find_device_by_ble_identifier(identifier)
        if device:
            device._update_connection_state(Device.ConnectionState.FAILED)
            self.connection_manager.note_connect_failed(device)

    def did_disconnect_from(self, identifier: str):
        device = self.find_device_by_ble_identifier(identifier)
        if device:
            device._update_connection_state(Device.ConnectionState.DISCONNECTED)
            self.message_handlers.clear_handlers_for_device(identifier)

    def update_device_hw_revision(self, identifier: str, revision: str):
        if device := self.find_device_by_ble_identifier(identifier):
            device.hardware_revision = revision

    def update_device_fw_version(self, identifier: str, version: str):
        if device := self.find_device_by_ble_identifier(identifier):
            device.firmware_version = version

    def update_device_serial_number(self, identifier: str, serial_number: str):
        if (device := self.find_device_by_ble_identifier(identifier) is not None) and isinstance(
            device, MeatNetNode
        ):
            device.serial_number_string = serial_number

    def update_device_model_info(self, identifier: str, model_info: str):
        if device := self.find_device_by_ble_identifier(identifier):
            device.update_with_model_info(model_info)

    def update_device_with_status(self, identifier: str, status: ProbeStatus):
        probe = self.find_device_by_ble_identifier(identifier)
        if probe and isinstance(probe, Probe):
            probe._update_probe_status(status)
            self.connection_manager.received_status_for(probe, direct_connection=True)

    def update_device_with_node_status(
        self, serial_number: int, status: ProbeStatus, hop_count: HopCount
    ):
        if probe := self.find_probe_by_serial_number(serial_number):
            probe._update_probe_status(status, hop_count)
            self.connection_manager.received_status_for(probe, direct_connection=False)

    def update_device_with_advertising(
        self, advertising: AdvertisingData, is_connectable: bool, rssi: int, identifier: str
    ):
        """Determines which Device to create/update based on received AdvertisingData."""
        if advertising.type == CombustionProductType.PROBE:
            probe = self.update_probe_with_advertising(
                advertising, is_connectable, rssi, identifier
            )
            self.connection_manager.received_probe_advertising(probe)
        elif advertising.type == CombustionProductType.MEAT_NET_NODE:
            if not self.connection_manager.meat_net_enabled:
                return

            # Update node if it is in device list
            meatnet_node = None
            new_node = False
            if (meatnet_node := self.devices.get(identifier)) and isinstance(meatnet_node, MeatNetNode):
                meatnet_node.update_with_advertising(advertising, is_connectable, rssi)

            else:
                # Create node and add to device list
                meatnet_node = MeatNetNode(advertising, self, is_connectable, rssi, identifier)
                self._add_device(meatnet_node)
                new_node = True

            # Update the probe associated with this advertising data
            probe = self.update_probe_with_advertising(
                advertising, is_connectable=None, rssi=None, identifier=None
            )
            if probe:
                # Add probe to meatnet node
                meatnet_node.update_networked_probe(probe)

                if new_node:
                    # Notify connection manager
                    self.connection_manager.received_probe_advertising_from_node(
                        probe, meatnet_node
                    )

    def update_device_with_gauge_advertising(
        self, advertising: GaugeAdvertisingData, is_connectable: bool, rssi: int, identifier: str
    ):
        """Handle Gauge-specific advertisements.

        Gauges behave like MeatNet Nodes (repeaters) but also emit Gauge-specific MSD.
        We represent a Gauge as a MeatNetNode with extra Gauge fields.
        """
        if not self.connection_manager.meat_net_enabled:
            return

        node = self.devices.get(identifier)
        if node and isinstance(node, MeatNetNode):
            node.update_with_gauge_advertising(advertising, is_connectable, rssi)
            return

        # Create a node record from Gauge advertising; repeated-probe advertising will fill in probes later.
        node = MeatNetNode(None, self, is_connectable, rssi, identifier)
        node.update_with_gauge_advertising(advertising, is_connectable, rssi)
        self._add_device(node)

    def update_probe_with_advertising(
        self,
        advertising: AdvertisingData,
        is_connectable: bool | None,
        rssi: int | None,
        identifier: str | None,
    ) -> Probe | None:
        """Searches for or creates a Device record for the Probe represented by specified AdvertisingData."""
        found_probe: Probe | None = None
        # If this advertising data was from a Probe, attempt to find its Device entry by its serial number.
        if advertising.serial_number != DeviceManager.INVALID_PROBE_SERIAL_NUMBER:
            unique_identifier = str(advertising.serial_number)
            if (probe := self.devices.get(unique_identifier)) and isinstance(probe, Probe):
                probe.update_with_advertising(advertising, is_connectable, rssi, identifier)
                found_probe = probe
            else:
                # If we don't yet have an entry for this Probe, create one.
                device = Probe(advertising, self, is_connectable, rssi, identifier)
                self._add_device(device)
                found_probe = device
        return found_probe

    def update_device_with_log_response(self, identifier: str, log_response: LogResponse):
        if not log_response.success:
            return
        if (probe := self.find_device_by_ble_identifier(identifier)) and isinstance(probe, Probe):
            probe._process_log_response(log_response)

    def update_device_with_session_information(
        self, identifier: str, session_information: SessionInformation
    ):
        if (probe := self.find_device_by_ble_identifier(identifier)) and isinstance(probe, Probe):
            probe._update_with_session_information(session_information)

    def handle_uart_data(self, identifier: str, data: bytes):
        """Processes data received over UART, which could be Responses and/or Requests depending on the source."""
        if device := self.find_device_by_ble_identifier(identifier):
            if isinstance(device, Probe):
                # If this was a Probe, treat all the data as responses
                responses = responses_from_data(data)
                for response in responses:
                    self.handle_probe_uart_response(identifier, response)
            elif isinstance(device, MeatNetNode):
                # If this was a Node, the data could be Responses and/or Requests
                messages = NodeUARTMessage.from_data(data)
                for message in messages:
                    if isinstance(message, NodeRequest):
                        self.handle_node_uart_request(identifier, message)
                    elif isinstance(message, NodeResponse):
                        self.handle_node_uart_response(identifier, message)

    def handle_probe_uart_response(self, identifier: str, response: Response):
        """Probe direct message handling"""
        if isinstance(response, LogResponse):
            self.update_device_with_log_response(identifier, response)
        elif isinstance(response, SetIDResponse):
            self.message_handlers.call_set_id_completion_handler(identifier, response)
        elif isinstance(response, SetColorResponse):
            self.message_handlers.call_set_color_completion_handler(identifier, response)
        elif isinstance(response, SessionInfoResponse):
            if response.success:
                self.update_device_with_session_information(identifier, response.info)
        elif isinstance(response, SetPredictionResponse):
            self.message_handlers.call_set_prediction_completion_handler(identifier, response)
        elif isinstance(response, ReadOverTemperatureResponse):
            self.message_handlers.call_read_over_temperature_completion_handler(
                identifier, response
            )

    def handle_node_uart_request(self, identifier: str, request: NodeRequest):
        if isinstance(request, NodeProbeStatusRequest):
            probe_status = request.probe_status
            hop_count = request.hop_count
            self.update_device_with_node_status(request.serial_number, probe_status, hop_count)

            # Ensure the Node that sent this item has the Probe in its list of repeated devices.
            if (node := self.find_device_by_ble_identifier(identifier)) and isinstance(
                node, MeatNetNode
            ):
                if probe := self.find_probe_by_serial_number(request.serial_number):
                    node.update_networked_probe(probe)
        # elif isinstance(request, NodeSyncThermometerListRequest):
        #     if (node := self.find_device_by_ble_identifier(identifier)) and isinstance(
        #         node, MeatNetNode
        #     ):
        #         LOGGER.info("Node [%s] is connected to probes [%s]", request.mac_address, [t.__str__() for t in request.thermometers])

        # elif isinstance(request, NodeHeartbeatRequest):
        #     # TODO handle heartbeat request (commented-out in Swift impl)
        #     return

    def handle_node_uart_response(self, identifier: str, response: NodeResponse):
        if isinstance(response, NodeSetPredictionResponse):
            self.message_handlers.call_node_set_prediction_completion_handler(identifier, response)
        elif isinstance(response, NodeReadFirmwareRevisionResponse):
            probe = self.find_probe_by_serial_number(serial_number=response.probe_serial_number)
            if probe:
                probe.firmware_version = response.fw_revision
        elif isinstance(response, NodeReadHardwareRevisionResponse):
            probe = self.find_probe_by_serial_number(serial_number=response.probe_serial_number)
            if probe:
                probe.hardware_revision = response.hw_revision
        elif isinstance(response, NodeReadModelInfoResponse):
            probe = self.find_probe_by_serial_number(serial_number=response.probe_serial_number)
            if probe:
                probe.update_with_model_info(response.model_info)
        elif isinstance(response, NodeReadSessionInfoResponse):
            probe = self.find_probe_by_serial_number(serial_number=response.probe_serial_number)
            if probe:
                probe._update_with_session_information(response.info)
        elif isinstance(response, NodeReadLogsResponse):
            probe = self.find_probe_by_serial_number(serial_number=response.probe_serial_number)
            if probe:
                probe._process_log_response(log_response=response)
