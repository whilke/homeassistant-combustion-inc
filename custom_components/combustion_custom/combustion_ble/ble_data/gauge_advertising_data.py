"""Gauge Bluetooth Advertising Data (MSD)."""

from __future__ import annotations

from typing import NamedTuple, Optional

from .advertising_data import CombustionProductType


class GaugeAdvertisingData(NamedTuple):
    """Parsed data from a Gauge Manufacturer Specific Data payload.

    Note: Bleak exposes manufacturer payloads *without* the 2-byte company identifier.
    Call `from_bleak_data` when parsing `AdvertisementData.manufacturer_data[BT_MANUFACTURER_ID]`.
    """

    type: CombustionProductType
    serial: str
    temperature_c: float | None
    sensor_present: bool
    sensor_overheating: bool
    low_battery: bool
    alarm_high_raw: int
    alarm_low_raw: int

    @staticmethod
    def from_data(data: bytes) -> Optional["GaugeAdvertisingData"]:
        """Create instance from full MSD bytes (including the 2-byte vendor id)."""
        if data is None or len(data) < 24:
            return None

        # Vendor ID
        vendor_id = int.from_bytes(data[0:2], byteorder="big")
        if vendor_id != 0x09C7:
            return None

        # Product type
        product_type = CombustionProductType(data[2])
        if product_type != CombustionProductType.GAUGE:
            return None

        # Serial number (10 bytes; ASCII-ish, may contain nulls)
        serial_bytes = data[3:13]
        serial = serial_bytes.decode("ascii", errors="ignore").rstrip("\x00").strip()
        if not serial:
            # Keep a deterministic fallback for registry/ids
            serial = serial_bytes.hex().upper()

        # Raw temperature (13-bit packed, 0.1°C steps; -20°C offset)
        # Encoding per Gauge spec: Temperature = (raw * 0.1) - 20
        # We assume little-endian packing for the 16-bit field and mask to 13 bits.
        raw_temp_field = int.from_bytes(data[13:15], byteorder="little")
        raw_temp = raw_temp_field & 0x1FFF
        temperature_c = (raw_temp * 0.1) - 20.0

        # Status flags
        flags = data[15]
        sensor_present = bool(flags & 0x01)
        sensor_overheating = bool(flags & 0x02)
        low_battery = bool(flags & 0x04)

        # Per spec: if sensor is not present, raw temperature value will be 0.
        if not sensor_present or raw_temp == 0:
            temperature_c = None

        # Alarm status fields (packed; keep raw status for now)
        # Encoding per Gauge spec:
        # - bytes 17..20: packed uint32: high Alarm Status (16 bits), then low Alarm Status (16 bits)
        alarm_high_raw = int.from_bytes(data[17:19], byteorder="little")
        alarm_low_raw = int.from_bytes(data[19:21], byteorder="little")

        return GaugeAdvertisingData(
            type=product_type,
            serial=serial,
            temperature_c=temperature_c,
            sensor_present=sensor_present,
            sensor_overheating=sensor_overheating,
            low_battery=low_battery,
            alarm_high_raw=alarm_high_raw,
            alarm_low_raw=alarm_low_raw,
        )

    @staticmethod
    def from_bleak_data(data: bytes) -> Optional["GaugeAdvertisingData"]:
        """Create instance from Bleak manufacturer payload (without vendor id)."""
        vendor_id = 0x09C7.to_bytes(2, "big")
        return GaugeAdvertisingData.from_data(vendor_id + data)
