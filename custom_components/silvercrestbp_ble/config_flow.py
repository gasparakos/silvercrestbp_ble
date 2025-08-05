"""Config flow for SilvercrestBP BLE integration."""

from __future__ import annotations

from typing import Any
import logging
import asyncio

import voluptuous as vol
import dbus
import dbus.exceptions

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow
from homeassistant.data_entry_flow import FlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

from .silvercrest_bp import SilvercrestBPBluetoothDeviceData
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Config flow constants
CONF_PAIRING_CODE = "pairing_code"


class SilvercrestBPConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SilvercrestBP."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_device: SilvercrestBPBluetoothDeviceData | None = None
        self._discovered_devices: dict[str, str] = {}
        self._device_requires_pairing: bool = False
        self._device_address: str | None = None
        self._device_name: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        device = SilvercrestBPBluetoothDeviceData()
        if not device.supported(discovery_info):
            return self.async_abort(reason="not_supported")
        self._discovery_info = discovery_info
        self._discovered_device = device
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery."""
        assert self._discovered_device is not None
        device = self._discovered_device
        assert self._discovery_info is not None
        discovery_info = self._discovery_info
        
        title = device.title or device.get_device_name() or discovery_info.name
        self._device_address = discovery_info.address
        self._device_name = title
        
        # Check if this is an SBM69 device that requires pairing
        if self._is_sbm69_device(discovery_info):
            self._device_requires_pairing = True
            if user_input is not None:
                return await self.async_step_pairing_code()
        else:
            if user_input is not None:
                return self.async_create_entry(title=title, data={})

        self._set_confirm_only()
        placeholders = {"name": title}
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="bluetooth_confirm", description_placeholders=placeholders
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step to pick discovered device."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            
            self._device_address = address
            self._device_name = self._discovered_devices[address]
            
            # Check if this device requires pairing by looking for SBM69 in name
            if "SBM69" in self._device_name:
                self._device_requires_pairing = True
                return await self.async_step_pairing_code()
            
            return self.async_create_entry(
                title=self._discovered_devices[address], data={}
            )

        current_addresses = self._async_current_ids()
        for discovery_info in async_discovered_service_info(self.hass, False):
            address = discovery_info.address
            if address in current_addresses or address in self._discovered_devices:
                continue
            device = SilvercrestBPBluetoothDeviceData()
            if device.supported(discovery_info):
                self._discovered_devices[address] = (
                    device.title or device.get_device_name() or discovery_info.name
                )

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(self._discovered_devices)}
            ),
        )
    
    async def async_step_pairing_code(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle pairing code input for SBM69 devices."""
        errors = {}
        
        if user_input is not None:
            pairing_code = user_input[CONF_PAIRING_CODE]
            
            # Validate pairing code format
            if not pairing_code or len(pairing_code) != 6 or not pairing_code.isdigit():
                errors[CONF_PAIRING_CODE] = "invalid_pairing_code"
            else:
                # Attempt pairing with the device
                pairing_result = await self._async_pair_device(self._device_address, pairing_code)
                
                if pairing_result["success"]:
                    return self.async_create_entry(
                        title=self._device_name,
                        data={"requires_pairing": True, "pairing_completed": True}
                    )
                else:
                    errors["base"] = pairing_result["error"]
        
        return self.async_show_form(
            step_id="pairing_code",
            data_schema=vol.Schema({
                vol.Required(CONF_PAIRING_CODE): cv.string
            }),
            errors=errors,
            description_placeholders={
                "device_name": self._device_name,
                "device_address": self._device_address
            }
        )
    
    @callback
    def _is_sbm69_device(self, discovery_info: BluetoothServiceInfoBleak) -> bool:
        """Check if the discovered device is an SBM69 that requires pairing."""
        device_name = discovery_info.name or ""
        return "SBM69" in device_name
    
    async def _async_pair_device(self, address: str, pairing_code: str) -> dict[str, Any]:
        """Attempt to pair with the device using D-Bus."""
        try:
            _LOGGER.info(f"Attempting to pair with {address} using code {pairing_code}")
            
            # This is a simplified version - in practice you'd want to use proper async D-Bus
            # For now, we'll simulate the pairing process
            result = await self.hass.async_add_executor_job(
                self._pair_device_sync, address, pairing_code
            )
            return result
            
        except Exception as e:
            _LOGGER.error(f"Error during pairing: {e}")
            return {"success": False, "error": "pairing_failed"}
    
    def _pair_device_sync(self, address: str, pairing_code: str) -> dict[str, Any]:
        """Synchronous pairing using D-Bus."""
        try:
            import dbus.mainloop.glib
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            
            bus = dbus.SystemBus()
            device_path = f"/org/bluez/hci0/dev_{address.replace(':', '_')}"
            
            # Check if device exists
            try:
                device_obj = bus.get_object("org.bluez", device_path)
                device = dbus.Interface(device_obj, "org.bluez.Device1")
                
                # Check if already paired
                properties = dbus.Interface(device_obj, "org.freedesktop.DBus.Properties")
                is_paired = properties.Get("org.bluez.Device1", "Paired")
                
                if is_paired:
                    _LOGGER.info(f"Device {address} is already paired")
                    return {"success": True, "error": None}
                
                # Attempt pairing
                _LOGGER.info(f"Initiating pairing for {address}")
                device.Pair()
                
                # In a real implementation, you'd need to handle the pairing agent
                # that responds with the pairing code when requested
                
                return {"success": True, "error": None}
                
            except dbus.exceptions.DBusException as e:
                if "Does not exist" in str(e):
                    return {"success": False, "error": "device_not_found"}
                elif "Already exists" in str(e):
                    return {"success": True, "error": None}
                else:
                    _LOGGER.error(f"D-Bus error during pairing: {e}")
                    return {"success": False, "error": "pairing_failed"}
                    
        except ImportError:
            _LOGGER.warning("D-Bus not available, cannot perform automatic pairing")
            return {"success": False, "error": "dbus_not_available"}
        except Exception as e:
            _LOGGER.error(f"Unexpected error during pairing: {e}")
            return {"success": False, "error": "pairing_failed"}