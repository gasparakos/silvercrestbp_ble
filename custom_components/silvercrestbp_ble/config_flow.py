"""Config flow for SilvercrestBP BLE integration."""

from __future__ import annotations

from typing import Any
import logging
import asyncio

import voluptuous as vol
from bleak import BleakClient
from bleak.exc import BleakError

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
        """Attempt to pair with the device using BleakClient."""
        try:
            _LOGGER.info(f"Attempting to pair with {address} using code {pairing_code}")
            
            # Note: The pairing code will be handled by the system's pairing agent
            # We just initiate the pairing process here
            client = BleakClient(address)
            
            try:
                # First try to connect without pairing to check if already paired
                await client.connect(timeout=10.0)
                _LOGGER.info(f"Device {address} is already paired and connected")
                await client.disconnect()
                return {"success": True, "error": None}
                
            except BleakError as e:
                if "not paired" in str(e).lower() or "authentication" in str(e).lower():
                    _LOGGER.info(f"Device {address} requires pairing")
                    
                    try:
                        # Disconnect if partially connected
                        if client.is_connected:
                            await client.disconnect()
                        
                        # Create new client with pairing enabled
                        pair_client = BleakClient(address, pair=True)
                        await pair_client.connect(timeout=30.0)  # Longer timeout for pairing
                        
                        _LOGGER.info(f"Successfully paired and connected to {address}")
                        await pair_client.disconnect()
                        return {"success": True, "error": None}
                        
                    except BleakError as pair_error:
                        _LOGGER.error(f"Pairing failed for {address}: {pair_error}")
                        if "timeout" in str(pair_error).lower():
                            return {"success": False, "error": "pairing_timeout"}
                        elif "rejected" in str(pair_error).lower():
                            return {"success": False, "error": "pairing_rejected"}
                        else:
                            return {"success": False, "error": "pairing_failed"}
                else:
                    _LOGGER.error(f"Connection failed for {address}: {e}")
                    return {"success": False, "error": "connection_failed"}
            
        except Exception as e:
            _LOGGER.error(f"Unexpected error during pairing: {e}")
            return {"success": False, "error": "pairing_failed"}
    
