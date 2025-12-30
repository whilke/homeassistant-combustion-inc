"""Predictive Probe."""

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Coroutine, Optional

from ..ble_data import AdvertisingData, CombustionProductType
from ..ble_data.battery_status_virtual_sensors import BatteryStatus
from ..ble_data.hop_count import HopCount
from ..ble_data.mode_id import ProbeColor, ProbeID, ProbeMode
from ..ble_data.probe_status import ProbeStatus
from ..ble_data.probe_temperatures import ProbeTemperatures
from ..ble_data.virtual_sensors import VirtualSensors
from ..devices.device import Device
from ..instant_read_filter import InstantReadFilter
from ..logged_probe_data_count import LoggedProbeDataPoint
from ..prediction.prediction_info import PredictionInfo
from ..prediction.prediction_manager import PredictionManager
from ..probe_temperature_log import ProbeTemperatureLog
from ..uart import LogResponse, SessionInformation
from ..uart.meatnet import NodeReadLogsResponse
from ..utilities.asyncio_utils import ensure_future
from ..utilities.monitor import Monitorable, RemoveListener, UpdateListener

if TYPE_CHECKING:
    from ..device_manager import DeviceManager


DEADBAND_RANGE_IN_CELSIUS = 0.05


class VirtualTemperatures:
    """Virtual temperature values for this Probe."""

    def __init__(
        self,
        core_temperature=DEADBAND_RANGE_IN_CELSIUS,
        surface_temperature=DEADBAND_RANGE_IN_CELSIUS,
        ambient_temperature=DEADBAND_RANGE_IN_CELSIUS,
    ):
        self.core_temperature = core_temperature
        """The Core temperature, in Celsius"""

        self.surface_temperature = surface_temperature
        """The Surface temperature, in Celsius"""

        self.ambient_temperature = ambient_temperature
        """The Ambient temperature, in Celsius"""


class Overheating:
    """Information regarding sensor overheating."""

    def __init__(self, is_overheating: bool, overheating_sensors: list[int]) -> None:
        self.is_overheating: bool = is_overheating
        """Denotes if this sensor is overheating."""

        self.overheating_sensors: list[int] = overheating_sensors
        """The list of overheating sensors."""


class Probe(Device):
    """
    Predictive Probe.
    """

    INSTANT_READ_STALE_TIMEOUT = 5.0
    #  Number of seconds to ignore other lower-priority (higher hop count) sources of information for Instant Read
    INSTANT_READ_LOCK_TIMEOUT = 1.0

    # Number of seconds to ignore other lower-priority (higher hop count) sources of information for Normal Mode
    NORMAL_MODE_LOCK_TIMEOUT = 1.0

    # Number of seconds after which status notifications should be considered stale.
    STATUS_NOTIFICATION_STALE_TIMEOUT = 16.0

    # Overheating thresholds (in degrees C) for T1 and T2
    OVERHEATING_T1_T2_THRESHOLD = 105.0
    # Overheating thresholds (in degrees C) for T3
    OVERHEATING_T3_THRESHOLD = 115.0
    # Overheating thresholds (in degrees C) for T4
    OVERHEATING_T4_THRESHOLD = 125.0
    # Overheating thresholds (in degrees C) for T5-T8
    OVERHEATING_T5_T8_THRESHOLD = 300.0

    def __init__(
        self,
        advertising: AdvertisingData,
        device_manager: "DeviceManager",
        is_connectable=None,
        rssi=None,
        identifier: str | None = None,
    ):
        super().__init__(
            unique_identifier=str(advertising.serial_number),
            ble_identifier=identifier,
            device_manager=device_manager,
            rssi=rssi,
        )
        self._serial_number = advertising.serial_number
        self._serial_number_string = f"{self._serial_number:08X}"

        self._id = advertising.mode_id.id
        self._color = advertising.mode_id.color
        self._hop_count = advertising.hop_count
        # "Hops" as an integer suitable for UI:
        # - 0: direct from probe (either via advertising or direct connection)
        # - 1..4: repeated via MeatNet (per spec hop-count encoding)
        self._hops: int | None = None
        self._current_temperatures: Monitorable[Optional[ProbeTemperatures]] = Monitorable(None)
        self._instant_read_celsius: Optional[float] = None
        self._instant_read_fahrenheit: Optional[float] = None
        self._instant_read_temperature: Optional[float] = None
        self._min_sequence_number: Optional[int] = None
        self._max_sequence_number: Optional[int] = None
        self._percent_of_logs_synced: Optional[int] = None
        self._battery_status = Monitorable(BatteryStatus.OK)
        self._virtual_sensors: Optional[VirtualSensors] = None
        self._prediction_info: Monitorable[Optional[PredictionInfo]] = Monitorable(None)
        self._virtual_temperatures: Monitorable[VirtualTemperatures] = Monitorable(
            VirtualTemperatures()
        )
        self._temperature_logs: list[ProbeTemperatureLog] = []
        self._overheating: Monitorable[Overheating] = Monitorable(
            Overheating(is_overheating=False, overheating_sensors=[])
        )
        self._last_status_notification_time = datetime.now()
        self._status_notifications_stale = False
        self._session_information: Optional[SessionInformation] = None
        self._last_instant_read: Optional[datetime] = None
        self._last_instant_read_hop_count: Optional[HopCount] = None
        self._last_normal_mode: Optional[datetime] = None
        self._last_normal_mode_hop_count: Optional[HopCount] = None
        self._prediction_manager = PredictionManager()
        self._instant_read_filter = InstantReadFilter()
        self._session_request_task: Optional[asyncio.Task] = None

        self._prediction_manager.add_update_listener(self._publish_prediction_info)

        # Update the probe with advertising data
        self.update_with_advertising(advertising, is_connectable, rssi, identifier)

        # Start timer to re-request session information every 3 minutes
        self.start_session_request_timer()

    def as_dict(self) -> dict:
        """Dictionary representation of this device. Required for the orjson encoder to properly encode this class."""
        return {"serial_number_string": self.serial_number_string}

    @property
    def serial_number(self) -> int:
        """Serial number for this device."""
        return self._serial_number

    @property
    def serial_number_string(self) -> str:
        """Readable (string) representation of this device's serial number."""
        return self._serial_number_string

    @property
    def batery_status(self) -> BatteryStatus:
        """The current battery status."""
        return self._battery_status.value

    def add_battery_status_listener(
        self, listener: UpdateListener[BatteryStatus]
    ) -> RemoveListener:
        """Add a listener for battery status changes."""
        return self._battery_status.add_update_listener(listener)

    @property
    def virtual_temperatures(self) -> VirtualTemperatures:
        """The current virtual temperatures."""
        return self._battery_status.value

    def add_virtual_temperatures_listener(
        self, listener: UpdateListener[VirtualTemperatures]
    ) -> RemoveListener:
        """Add a listener for virtual temperatures changes."""
        return self._virtual_temperatures.add_update_listener(listener)

    @property
    def overheating(self) -> Overheating:
        """Overheating information."""
        return self._overheating.value

    def add_overheating_listener(self, listener: UpdateListener[Overheating]) -> RemoveListener:
        """Add a listener for overheating changes."""
        return self._overheating.add_update_listener(listener)

    @property
    def current_temperatures(self) -> ProbeTemperatures | None:
        """Current temperature of each thermistor in the probe."""
        return self._current_temperatures.value

    def add_current_temperatures_listener(
        self, listener: UpdateListener[ProbeTemperatures | None]
    ) -> RemoveListener:
        """Add a listener for temperature changes."""
        return self._current_temperatures.add_update_listener(listener)

    @property
    def prediction_info(self) -> Optional[PredictionInfo]:
        """Prediction information."""
        return self._prediction_info.value

    def add_prediction_info_listener(
        self, listener: UpdateListener[Optional[PredictionInfo]]
    ) -> RemoveListener:
        """Add a listener for prediction info changes."""
        return self._prediction_info.add_update_listener(listener)

    async def _session_request_timer(self):
        while True:
            await asyncio.sleep(180)  # Wait for 180 seconds
            await self._request_session_information()

    def start_session_request_timer(self):
        if self._session_request_task is None or self._session_request_task.done():
            self._session_request_task = asyncio.create_task(self._session_request_timer())

    def stop_session_request_timer(self):
        if self._session_request_task and not self._session_request_task.done():
            self._session_request_task.cancel()

    def _publish_prediction_info(self, prediction_info: PredictionInfo):
        self._prediction_info.update(prediction_info)

    def _update_connection_state(self, state):
        if state == self.ConnectionState.DISCONNECTED:
            self._session_information = None
        super()._update_connection_state(state)

    def _update_device_stale(self):
        """Updates the device's stale status. Clears instant read temperatures if they are stale
        and updates whether status notifications are stale.
        """
        if self._last_instant_read:
            time_since_last_instant_read = (
                datetime.now() - self._last_instant_read
            ).total_seconds()
            if time_since_last_instant_read > self.INSTANT_READ_STALE_TIMEOUT:
                self._instant_read_celsius = None
                self._instant_read_fahrenheit = None
                self._instant_read_temperature = None

        self._update_status_notifications_stale()
        super()._update_device_stale()

    def update_with_advertising(
        self,
        advertising: AdvertisingData,
        is_connectable: bool | None,
        rssi: Optional[int],
        ble_identifier: str | None,
    ):
        # Update probe with advertising data
        # Any advertising means we are still "seeing" the probe (directly or via MeatNet).
        # Keep last_update_time fresh to avoid HA availability flapping.
        self.last_update_time = datetime.now()

        if rssi is not None:
            self._rssi.update(rssi)
        if is_connectable is not None:
            self.is_connectable = is_connectable
        if ble_identifier is not None:
            self.ble_identifier = ble_identifier

        self._hop_count = advertising.hop_count

        # Only update rest of data if not connected to probe.
        # Otherwise, rely on status notifications to update data
        if self.connection_state != self.ConnectionState.CONNECTED:
            if advertising.mode_id.mode == ProbeMode.NORMAL:
                # If we should update normal mode, do so, but since this is Advertising info
                # and does not contain Prediction information, DO NOT lock it out. We want to
                # ensure the Prediction info gets updated over a Status notification if one
                # comes in.
                if self._should_update_normal_mode(advertising.hop_count):
                    # Update ID, Color, Battery status
                    self._update_id_color_battery(
                        advertising.mode_id.id,
                        advertising.mode_id.color,
                        advertising.battery_status_virtual_sensors.battery_status,
                    )

                    # Update temperatures, virtual sensors, and check for overheating
                    self._update_temperatures(
                        advertising.temperatures,
                        advertising.battery_status_virtual_sensors.virtual_sensors,
                    )

                    # Direct probe advertising should surface as 0 hops.
                    self._hops = 0 if advertising.type == CombustionProductType.PROBE else (
                        advertising.hop_count.value + 1
                    )

                    # last_update_time already updated above
            elif advertising.mode_id.mode == ProbeMode.INSTANT_READ:
                #  Update Instant Read temperature, providing hop count information to prioritize it.
                hop_count = None
                if advertising.type != CombustionProductType.PROBE:
                    hop_count = advertising.hop_count

                if self._update_instant_read(
                    advertising.temperatures.values[0],
                    advertising.mode_id.id,
                    advertising.mode_id.color,
                    advertising.battery_status_virtual_sensors.battery_status,
                    hop_count,
                ):
                    self._hops = 0 if advertising.type == CombustionProductType.PROBE else (
                        advertising.hop_count.value + 1
                    )
                    # last_update_time already updated above

    def _update_id_color_battery(
        self, probe_id: ProbeID, probe_color: ProbeColor, probe_battery_status: BatteryStatus
    ):
        self._id = probe_id
        self._color = probe_color
        self._battery_status.update(probe_battery_status)

    def _update_temperatures(
        self, temperatures: ProbeTemperatures, virtual_sensors: VirtualSensors
    ):
        self._current_temperatures.update(temperatures)
        self._virtual_sensors = virtual_sensors

        core = virtual_sensors.virtual_core.temperature_from(temperatures)
        surface = virtual_sensors.virtual_surface.temperature_from(temperatures)
        ambient = virtual_sensors.virtual_ambient.temperature_from(temperatures)

        self._virtual_temperatures.update(VirtualTemperatures(core, surface, ambient))

        self._check_overheating()

    def _check_overheating(self):
        if not self.current_temperatures:
            return

        any_over_temp = False
        overheating_sensor_list: list[int] = []

        # Check T1-T2
        for i in range(0, 2):
            if self.current_temperatures.values[i] >= self.OVERHEATING_T1_T2_THRESHOLD:
                any_over_temp = True
                overheating_sensor_list.append(i)

        # Check T3
        if self.current_temperatures.values[2] >= self.OVERHEATING_T3_THRESHOLD:
            any_over_temp = True
            overheating_sensor_list.append(2)

        # Check T4
        if self.current_temperatures.values[3] >= self.OVERHEATING_T4_THRESHOLD:
            any_over_temp = True
            overheating_sensor_list.append(3)

        # Check T5-T8
        for i in range(4, 8):
            if self.current_temperatures.values[i] >= self.OVERHEATING_T5_T8_THRESHOLD:
                any_over_temp = True
                overheating_sensor_list.append(i)

        if (
            self._overheating.value.is_overheating == any_over_temp
            and self._overheating.value.overheating_sensors == overheating_sensor_list
        ):
            return

        self._overheating.update(
            Overheating(is_overheating=any_over_temp, overheating_sensors=overheating_sensor_list)
        )

    def _update_probe_status(
        self, device_status: ProbeStatus, hop_count: Optional[HopCount] = None
    ):
        # Ignore status messages that have a sequence count lower than any previously received status messages
        if self._is_old_status_update(device_status):
            return
        updated = False
        if device_status.mode_id.mode == ProbeMode.NORMAL:
            if self._should_update_normal_mode(hop_count):
                self._update_id_color_battery(
                    device_status.mode_id.id,
                    device_status.mode_id.color,
                    device_status.battery_status_virtual_sensors.battery_status,
                )

                self._min_sequence_number = device_status.min_sequence_number
                self._max_sequence_number = device_status.max_sequence_number

                ensure_future(
                    self._prediction_manager.update_prediction_status(
                        device_status.prediction_status, device_status.max_sequence_number
                    ),
                    name="update_prediction_status[probe]",
                )

                self._update_temperatures(
                    device_status.temperatures,
                    device_status.battery_status_virtual_sensors.virtual_sensors,
                )

                self._add_data_to_log(LoggedProbeDataPoint.from_device_status(device_status))

                self._last_normal_mode = datetime.now()
                self._last_normal_mode_hop_count = hop_count

                updated = True
        elif device_status.mode_id.mode == ProbeMode.INSTANT_READ:
            updated = self._update_instant_read(
                device_status.temperatures.values[0],
                probe_id=device_status.mode_id.id,
                probe_color=device_status.mode_id.color,
                probe_battery_status=device_status.battery_status_virtual_sensors.battery_status,
                hop_count=hop_count,
            )
            if updated:
                self._min_sequence_number = device_status.min_sequence_number
                self._max_sequence_number = device_status.max_sequence_number

        ensure_future(self._request_missing_data(), name="request_missing_data[probe]")

        if updated:
            # Status over a direct probe connection has no hop count => 0.
            # Status proxied from a node includes hop_count => 1..4.
            self._hops = 0 if hop_count is None else (hop_count.value + 1)
            current = self._get_current_temperature_log()
            if current:
                self._update_log_percent()
                missing_range = current.missing_range(
                    device_status.min_sequence_number, device_status.max_sequence_number
                )
                if missing_range:
                    ensure_future(
                        self.device_manager.request_logs_from(
                            self, min_sequence=missing_range[0], max_sequence=missing_range[1]
                        ),
                        name="request_logs_from[probe]",
                    )

        self._last_status_notification_time = datetime.now()
        self._update_status_notifications_stale()
        self.last_update_time = datetime.now()

    def _update_instant_read(
        self,
        instant_read_value: float,
        probe_id: ProbeID,
        probe_color: ProbeColor,
        probe_battery_status: BatteryStatus,
        hop_count: Optional[HopCount],
    ) -> bool:
        if self._should_update_instant_read(hop_count):
            self._last_instant_read = datetime.now()
            self._last_instant_read_hop_count = hop_count
            self._instant_read_filter.add_reading(instant_read_value)
            self._instant_read_temperature = instant_read_value
            self._instant_read_celsius = self._instant_read_filter.values[0]
            self._instant_read_fahrenheit = self._instant_read_filter.values[1]

            self._update_id_color_battery(probe_id, probe_color, probe_battery_status)

            return True
        else:
            return False

    def _update_with_session_information(self, session_information: SessionInformation):
        self._session_information = session_information

    def _update_log_percent(self) -> None:
        current_log = self._get_current_temperature_log()
        max_sequence_number = self._max_sequence_number
        min_sequence_number = self._min_sequence_number
        if max_sequence_number is None or min_sequence_number is None or current_log is None:
            return

        number_logs_from_probe = current_log.logs_in_range(
            [min_sequence_number, max_sequence_number]
        )
        number_logs_on_probe = int(max_sequence_number - min_sequence_number + 1)
        if number_logs_from_probe == number_logs_on_probe:
            self._percent_of_logs_synced = 100
        else:
            self._percent_of_logs_synced = int(
                float(number_logs_from_probe) / float(number_logs_on_probe) * 100
            )

    def _is_old_status_update(self, device_status: ProbeStatus) -> bool:
        current_temp_log = self._get_current_temperature_log()
        if current_temp_log:
            max = current_temp_log.data_points[-1]
            if max.sequence_num is None:
                return False
            return device_status.max_sequence_number < max.sequence_num
        return False

    def _get_current_temperature_log(self) -> Optional[ProbeTemperatureLog]:
        if not self._session_information:
            return None
        return next(
            (
                log
                for log in self._temperature_logs
                if log.session_information.session_id == self._session_information.session_id
            ),
            None,
        )

    def _add_data_to_log(self, data_point: LoggedProbeDataPoint) -> None:
        current = self._get_current_temperature_log()
        if current:
            current.append_data_point(data_point=data_point)
        elif self._session_information:
            log = ProbeTemperatureLog(self._session_information)
            log.append_data_point(data_point=data_point)
            self._temperature_logs.append(log)

    def _process_log_response(self, log_response: LogResponse | NodeReadLogsResponse):
        # Process log response
        if isinstance(log_response, LogResponse):
            self._add_data_to_log(LoggedProbeDataPoint.from_log_response(log_response))
        elif isinstance(log_response, NodeReadLogsResponse):
            self._add_data_to_log(LoggedProbeDataPoint.from_node_read_logs_response(log_response))

    def _update_status_notifications_stale(self):
        """Updates the status of whether the status notifications are stale.
        This is based on the time elapsed since the last status notification.
        """
        time_since_last_notification = (
            datetime.now() - self._last_status_notification_time
        ).total_seconds()
        self._status_notifications_stale = (
            time_since_last_notification > self.STATUS_NOTIFICATION_STALE_TIMEOUT
        )

    async def _request_missing_data(self) -> None:
        tasks: list[Coroutine] = []
        if self._session_information is None:
            tasks.append(self.device_manager.read_session_info(self))

        if self.firmware_version is None:
            tasks.append(self.device_manager.read_firmware_version(self))

        if self.hardware_revision is None:
            tasks.append(self.device_manager.read_hardware_version(self))

        if self.manufacturing_lot is None or self.sku is None:
            tasks.append(self.device_manager.read_model_info_for_probe(self))

        await asyncio.gather(*tasks)  # TODO: error handling here, or at call site

    # Methods related to DFU functionalities
    def run_software_upgrade(self, dfu_file):
        # Placeholder for running software upgrade
        pass

    def dfu_state_did_change(self, state):
        pass

    def dfu_error_did_occur(self, error, message):
        pass

    def dfu_progress_did_change(self, part, total_parts, progress):
        pass

    def log_with_level(self, level, message):
        # Placeholder for logging
        pass

    def _should_update_normal_mode(self, hop_count: Optional[HopCount]) -> bool:
        if not hop_count or not self._last_normal_mode:
            return True
        time_since_last_normal_mode = (datetime.now() - self._last_normal_mode).total_seconds()
        if time_since_last_normal_mode > self.NORMAL_MODE_LOCK_TIMEOUT:
            return True

        if self._last_normal_mode_hop_count is None:
            return False

        if hop_count.value <= self._last_normal_mode_hop_count.value:
            return True

        return False

    def _should_update_instant_read(self, hop_count: Optional[HopCount]) -> bool:
        # If hopCount is nil, this is direct from a Probe and we should always update.
        if not hop_count or not self._last_instant_read:
            return True

        # If we haven't received Instant Read data for more than the lockout period, we should always update.
        time_since_last_instant_read = (datetime.now() - self._last_instant_read).total_seconds()
        if time_since_last_instant_read > self.INSTANT_READ_LOCK_TIMEOUT:
            return True

        # If we're in the lockout period and the last hop count was nil (i.e. direct from a Probe),
        # we should NOT update.
        if self._last_instant_read_hop_count is None:
            return False

        # Compare hop counts and see if we should update.
        if hop_count.value <= self._last_instant_read_hop_count.value:
            # This hop count is equal or better priority than the last, so update.
            return True
        else:
            # This hop is lower priority than the last, so do not update.
            return False

    async def _request_session_information(self):
        await self.device_manager.read_session_info(self)

    def __str__(self):
        return f"Probe: {self.unique_identifier}"
