from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timezone

from bleak import BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
    retry_bluetooth_connection_error,
)
from bluetooth_data_tools import short_address
from bluetooth_sensor_state_data import BluetoothData
from home_assistant_bluetooth import BluetoothServiceInfo
from sensor_state_data import SensorDeviceClass, SensorUpdate, Units
from sensor_state_data.enum import StrEnum

from .const import (
    CHARACTERISTIC_BLOOD_PRESSURE,
    # CHARACTERISTIC_BATTERY,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class SilvercrestBPSensor(StrEnum):

    SYSTOLIC = "systolic"
    DIASTOLIC = "diastolic"
    PULSE = "pulse"
    SIGNAL_STRENGTH = "signal_strength"
    # BATTERY_PERCENT = "battery_percent"
    TIMESTAMP = "timestamp"


class SilvercrestBPBluetoothDeviceData(BluetoothData):
    """Data for SilvercrestBP BLE sensors."""

    def __init__(self) -> None:
        super().__init__()
        self._event = asyncio.Event()

    def _start_update(self, service_info: BluetoothServiceInfo) -> None:
        """Update from BLE advertisement data."""
        _LOGGER.debug("Parsing SilvercrestBP BLE advertisement data: %s", service_info)
        self.set_device_manufacturer("Silvercrest")
        
        # Set device type based on model
        device_name = service_info.name or ""
        if "SBM69" in device_name:
            self.set_device_type("Blood Pressure Monitor SBM69")
        else:
            self.set_device_type("Blood Pressure Monitor SBM70")
            
        name = f"{service_info.name} {short_address(service_info.address)}"
        self.set_device_name(name)
        self.set_title(name)
    
    def supported(self, service_info: BluetoothServiceInfo) -> bool:
        """Check if the device is supported."""
        device_name = service_info.name or ""
        return device_name in ["SBM69", "SBM70"]

    def poll_needed(
        self, service_info: BluetoothServiceInfo, last_poll: float | None
    ) -> bool:
        """
        This is called every time we get a service_info for a device. It means the
        device is working and online.
        """
        return not last_poll or last_poll > UPDATE_INTERVAL

    @retry_bluetooth_connection_error()
    def notification_handler(self, _, data) -> None:
        """Helper for command events, parsing and updating sensor data."""
        try:
            # Debug log for raw data received
            _LOGGER.debug("Raw data received from BLE device: %s (length: %d)", data.hex(), len(data))
            
            # Parse data based on length (SBM69=19 bytes, SBM70=17 bytes)
            if len(data) == 19:  # SBM69 format
                parsed_data = self._parse_sbm69_data(data)
            elif len(data) == 17:  # SBM70 format  
                parsed_data = self._parse_sbm70_data(data)
            else:
                _LOGGER.warning("Unexpected data length: %d bytes", len(data))
                return
            
            if not parsed_data:
                return
                
            # Update timestamp
            if parsed_data.get('timestamp'):
                self.update_sensor(
                    key=str(SilvercrestBPSensor.TIMESTAMP),
                    native_unit_of_measurement=None,
                    native_value=parsed_data['timestamp'],
                    name="Measured Date",
                )

            _LOGGER.info(
                "Parsed data from BPM device (systolic: %s, diastolic: %s, pulse: %s)",
                parsed_data['systolic'], parsed_data['diastolic'], parsed_data['pulse']
            )

            # Update sensors
            self.update_sensor(
                key=str(SilvercrestBPSensor.SYSTOLIC),
                native_unit_of_measurement=Units.PRESSURE_MMHG,
                native_value=parsed_data['systolic'],
                device_class=SensorDeviceClass.PRESSURE,
                name="Systolic",
            )
            self.update_sensor(
                key=str(SilvercrestBPSensor.DIASTOLIC),
                native_unit_of_measurement=Units.PRESSURE_MMHG,
                native_value=parsed_data['diastolic'],
                device_class=SensorDeviceClass.PRESSURE,
                name="Diastolic",
            )
            self.update_sensor(
                key=str(SilvercrestBPSensor.PULSE),
                native_unit_of_measurement="bpm",
                native_value=parsed_data['pulse'],
                name="Pulse",
            )
            
        except Exception as e:
            _LOGGER.error("Unexpected error while handling BLE notification: %s", str(e))
        finally:
            self._event.set()

    async def async_poll(self, ble_device: BLEDevice) -> SensorUpdate:
        """
        Poll the device to retrieve any values we can't get from passive listening.
        """
        _LOGGER.debug("Connecting to BLE device: %s", ble_device.address)
        client = await establish_connection(
            BleakClientWithServiceCache, ble_device, ble_device.address
        )
        try:
            await client.start_notify(
                CHARACTERISTIC_BLOOD_PRESSURE, self.notification_handler
            )
        except Exception as e:
            _LOGGER.error("Failed to start notify on BLE device %s: %s", ble_device.address, str(e))

        # battery_char = client.services.get_characteristic(CHARACTERISTIC_BATTERY)
        # battery_payload = await client.read_gatt_char(battery_char)
        # self.update_sensor(
        #     key=str(SilvercrestBPSensor.BATTERY_PERCENT),
        #     native_unit_of_measurement=Units.PERCENTAGE,
        #     native_value=battery_payload[0],
        #     device_class=SensorDeviceClass.BATTERY,
        #     name="Battery",
        # )

        # Wait to see if a callback comes in.
        try:
            # Wait to see if a callback comes in within 15 seconds.
            await asyncio.wait_for(self._event.wait(), timeout=15)
        except asyncio.TimeoutError:
            _LOGGER.warning("Timeout while waiting for command data from BLE device.")
        except Exception as e:
            _LOGGER.error("Unexpected error while waiting for BLE response: %s", str(e))
        finally:
            try:
                await client.stop_notify(CHARACTERISTIC_BLOOD_PRESSURE)
            except Exception as e:
                _LOGGER.error("Failed to stop notification on BLE device: %s", str(e))
            try:
                await client.disconnect()
            except Exception as e:
                _LOGGER.error("Failed to disconnect from BLE device: %s", str(e))
            _LOGGER.debug("Disconnected from active Bluetooth client")
        return self._finish_update()
    
    def _parse_sbm69_data(self, data: bytes) -> dict | None:
        """Parse SBM69 19-byte data format."""
        try:
            # SBM69 format: 19 bytes
            # Bytes 1-2: Systolic (LE), 3-4: Diastolic (LE), 5-6: Mean arterial (LE)
            # Bytes 7-8: Year (LE), 9: Month, 10: Day, 11: Hour, 12: Minute, 13: Second
            # Bytes 14-15: Pulse (LE), 16: User ID, 17-18: Additional flags
            
            systolic = data[2] * 256 + data[1]
            diastolic = data[4] * 256 + data[3]
            mean_arterial = data[6] * 256 + data[5]  # Available in SBM69
            
            year = data[8] * 256 + data[7]
            month = data[9]
            day = data[10]
            hour = data[11]
            minute = data[12]
            second = data[13]
            
            pulse = data[15] * 256 + data[14]
            user_id = data[16]
            
            # Create timestamp
            timestamp = None
            try:
                timestamp = datetime(year, month, day, hour, minute, second)
                local_timezone = datetime.now(timezone.utc).astimezone().tzinfo
                timestamp = timestamp.replace(tzinfo=local_timezone)
            except ValueError as e:
                _LOGGER.error("Failed to parse SBM69 timestamp: %s", str(e))
            
            return {
                'systolic': systolic,
                'diastolic': diastolic,
                'pulse': pulse,
                'timestamp': timestamp,
                'mean_arterial': mean_arterial,  # SBM69 specific
                'user_id': user_id
            }
            
        except Exception as e:
            _LOGGER.error("Error parsing SBM69 data: %s", str(e))
            return None
    
    def _parse_sbm70_data(self, data: bytes) -> dict | None:
        """Parse SBM70 17-byte data format (original format)."""
        try:
            # SBM70 format: 17 bytes (original format)
            systolic = data[2] * 256 + data[1]
            diastolic = data[4] * 256 + data[3]
            # Skip arterial pressure for SBM70
            
            year = data[8] * 256 + data[7]
            month = data[9]
            day = data[10]
            hour = data[11]
            minute = data[12]
            
            pulse = data[15] * 256 + data[14]
            user_id = data[16] if len(data) > 16 else 1

            # Create timestamp
            timestamp = None
            try:
                timestamp = datetime(year, month, day, hour, minute, 0)
                local_timezone = datetime.now(timezone.utc).astimezone().tzinfo
                timestamp = timestamp.replace(tzinfo=local_timezone)
            except ValueError as e:
                _LOGGER.error("Failed to parse SBM70 timestamp: %s", str(e))

            return {
                'systolic': systolic,
                'diastolic': diastolic,
                'pulse': pulse,
                'timestamp': timestamp,
                'user_id': user_id
            }
            
        except Exception as e:
            _LOGGER.error("Error parsing SBM70 data: %s", str(e))
            return None