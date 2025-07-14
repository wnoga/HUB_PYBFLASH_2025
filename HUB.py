import json
import time
import struct
import random
try:
    import _thread
    import pyb
    import micropython
    import uasyncio
except:
    import asyncio as uasyncio

from AFE import AFEDevice, AFECommand
from my_utilities import JSONLogger, AFECommandChannel, AFECommandSubdevice, AFECommandGPIO, AFECommandAverage, read_callibration_csv
from my_utilities import channel_name_xxx, e_ADC_CHANNEL
from my_utilities import wdt
from my_utilities import p
from my_utilities import VerbosityLevel
from my_utilities import AFECommandChannelMask
from my_utilities import extract_bracketed
from my_utilities import millis, is_timeout, is_delay
from my_utilities import convert_to_si
from my_RxDeviceCAN import RxDeviceCAN
from my_utilities import get_configuration_from_files


class HUBDevice:
    """
    HUBDevice class manages communication with multiple AFE devices over CAN bus.

    This class handles device discovery, message processing, and command execution
    for a network of Analog Front-End (AFE) devices. It uses a CAN bus for
    communication and supports both polling and callback-based message handling.

    Attributes:
        can_bus (pyb.CAN): The CAN bus object used for communication.
        lock (_thread.allocate_lock): A lock for thread synchronization.
        logger (EmptyLogger): Logger for logging events and errors.
        use_rxcallback (bool): Flag to enable or disable CAN RX callback.
    """

    def __init__(self, can_bus: pyb.CAN, logger: JSONLogger, rxDeviceCAN: RxDeviceCAN, use_rxcallback=True, use_automatic_restart=False):
        self.can_bus = can_bus
        self.afe_devices: list[AFEDevice] = []
        self.afe_devices_max = 8
        self.use_automatic_restart = use_automatic_restart

        self.main_loop_yield_ms = 1

        self.rx_timeout_ms = 1000
        self.run = True

        self.logger = logger
        self.use_rxcallback = use_rxcallback
        self.can_interface = rxDeviceCAN

        self.message_queue = []
        self.message_queue_max = 128

        self.discovery_active = False  # enable discovery subprocess
        self.afe_manage_active = False  # enable management of the AFEs
        self.rx_process_active = False

        self.afe_id_min = 1
        self.afe_id_max = 255
        self.current_discovery_id = self.afe_id_min

        self.tx_timeout_ms = 100
        self.last_tx_time = 0

        self.use_tx_delay = True
        self.tx_delay_ms = 100

        self.curent_function = None
        self.curent_function_timestamp_ms = 0
        self.curent_function_timeout_ms = 2500
        self.curent_function_afe_id = None
        self.curent_function_retval = None

        self.afecmd = AFECommand()

        self.afe0: AFEDevice = None

        self.msg_to_process = None

        self.logger_sync_active = True

    async def powerOn(self):  # Changed to async def
        await self.logger.log(VerbosityLevel["INFO"],
                              {
            "device_id": 0,
            "timestamp_ms": millis(),
            "info": "powerOn"
        })
        pyb.Pin.cpu.E12.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
        pyb.Pin.cpu.E12.value(1)

    async def powerOff(self):  # Changed to async def
        await self.logger.log(VerbosityLevel["INFO"],
                              {
            "device_id": 0,
            "timestamp_ms": millis(),
            "info": "powerOff"
        })
        pyb.Pin.cpu.E12.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
        pyb.Pin.cpu.E12.value(0)

    async def reset_all(self):  # Changed to async def
        await self.stop_discovery()
        self.afe_devices = []
        self.message_queue = []
        self.current_discovery_id = 1

    async def close_all(self):  # Changed to async def
        await self.logger.log(VerbosityLevel["INFO"], {
            "device_id": 0,
            "info": "CLOSE ALL", "timestamp_ms": millis()})
        self.logger.request_new_file()
        self.use_automatic_restart = False
        for afe in self.afe_devices:
            await afe.restart_device()
        await self.powerOff()

    def clear_all_logs(self):
        try:
            import os
            for filename in os.listdir("/sd/logs"):
                os.remove("/sd/logs/" + filename)
        except Exception as e:
            # This function is not async, p.print() is async.
            # Using standard print for non-async context
            print("Error clearing logs: {}".format(e))

    async def get_subdevice_status(self, afe_id, subdevice_mask, callback=None):
        """
        Requests the status of a specific subdevice (master/slave) on an AFE.

        Args:
            afe_id (int): The ID of the AFE device.
            subdevice_mask (int): The mask for the subdevice (e.g., AFECommandSubdevice.AFECommandSubdevice_master).
            callback (callable, optional): A callback function to be executed when the response is received.
        Returns:
            int: 0 on success, -1 if AFE not found.
        """
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            await p.print("AFE {} not found for get_subdevice_status.".format(afe_id))
            return -1

        commandKwargs = {"timeout_ms": 10220, "preserve": True, "timeout_start_on_send_ms": 2000, "callback_error": self.callback_afe_error}
        if callback: commandKwargs["callback"] = callback
        await afe.enqueue_command(AFECommand.getSubdeviceStatus, [subdevice_mask], **commandKwargs)
        return 0

    async def clear_old_logs(self):
        """
        Triggers the logger to delete all log files except the current one.
        """
        if self.logger and hasattr(self.logger, 'clear_old_logs'):
            await self.logger.clear_old_logs()
        else:
            # Fallback or error logging if logger doesn't have the method
            await p.print("Logger not available or does not support clearing old logs.")

    async def _dequeue_message_copy(self, _):
        self.msg_to_process = await self.can_interface.get()
        return self.msg_to_process

    def _message_queue_len(self):
        return len(self.message_queue)

    def get_afe_by_id(self, afe_id) -> AFEDevice:
        """
        Find an AFE by its short ID.

        Args:
            afe_id: The short ID of the AFE to find.
        Returns:
            The AFEDevice object if found, otherwise None.
        """
        for afe in self.afe_devices:
            if afe.device_id == afe_id:
                return afe
        return None

    # Changed to async def
    async def process_received_messages(self, timer=None):
        """
        Process messages received from the CAN bus.

        This method retrieves messages from the message queue, identifies the
        AFE device associated with each message, and processes the received data.
        If a message is from a new AFE, it creates a new AFEDevice instance.
        """
        message = None
        # Check if message processing is active
        if not self.rx_process_active:
            return  # Exit early if message processing is not active
        if self.msg_to_process is None:
            return
        message = self.msg_to_process.copy()
        self.msg_to_process = None
        if message is None:
            return
        afe_id = (message[0] >> 2) & 0xFF  # unmask the AFE ID
        afe = self.get_afe_by_id(afe_id)
        if afe is None:  # Add new discovered AFE
            # Create a new AFE device instance with the discovered ID
            afe = AFEDevice(self.can_interface, afe_id, logger=self.logger)
            await self.logger.log(VerbosityLevel["INFO"],
                                  {
                "device_id": 0,
                "timestamp_ms": millis(),
                "info": "found new AFE {}".format(afe_id)
            })
            # Add the new AFE device to the list of known devices
            self.afe_devices.append(afe)
            if not self.afe0:
                self.afe0 = afe
        # Process the received data using the AFE device's method
        await afe.process_received_data(message)

    # Renamed to avoid conflict if old one is kept temporarily
    async def discover_devices_async(self):
        """ Periodically discover AFEs on the CAN bus. """
        if not self.discovery_active:
            return

        if len(self.afe_devices) >= self.afe_devices_max:  # Use >= for safety
            self.stop_discovery()
            return

        if self.use_tx_delay and is_delay(self.last_tx_time, self.tx_delay_ms):
            return

        if self.can_interface.state() > 1:
            if self.can_interface.state() > 2:  # Corresponds to pyb.CAN.BUS_OFF or more severe

                await self.logger.log(VerbosityLevel["ERROR"], "CAN bus error state {}, attempting restart.".format(self.can_interface.state()))
                self.can_interface.restart()
            else:

                await self.logger.log(VerbosityLevel["WARNING"], "CAN bus warning state {}.".format(self.can_interface.state()))
            return

        if self.current_discovery_id > self.afe_id_max:
            self.current_discovery_id = self.afe_id_min

        if not any(afe.is_online and afe.device_id == self.current_discovery_id for afe in self.afe_devices):
            send_result = await self.can_interface.send(
                toSend=b"\x00\x11",  # Command to request AFE presence/ID
                can_address=self.current_discovery_id << 2,
                timeout_ms=self.tx_timeout_ms
            )
            if send_result is None:  # Indicates successful scheduling by can_interface
                self.last_tx_time = millis()

                await self.logger.log(VerbosityLevel["DEBUG"], "Sent discovery to ID: {}".format(self.current_discovery_id))
        self.current_discovery_id += 1

    async def start_discovery(self):  # Changed to async def
        """ Start the device discovery process. """
        self.discovery_active = True

    async def stop_discovery(self):  # Changed to async def
        """ Stop the device discovery process. """
        self.discovery_active = False
        await p.print("STOP DISCOVERY")

    def get_afe_by_id(self, afe_id) -> AFEDevice:
        if len(self.afe_devices) == 0:
            return None
        for afe in self.afe_devices:
            if afe.device_id == afe_id:
                return afe
        return None

    # # Changed to async def
    # async def get_configuration_from_files(self, afe_id, callibration_data_file_csv="dane_kalibracyjne.csv", TempLoop_file_csv="TempLoop.csv", UID=None):
    #     return await get_configuration_from_files(afe_id, callibration_data_file_csv, TempLoop_file_csv, UID)

    def _get_subdevice_ch_id(self, g):
        return AFECommandSubdevice.AFECommandSubdevice_master if g == 'M' else AFECommandSubdevice.AFECommandSubdevice_slave

    @staticmethod
    def _get_T_measured_ch_id(g):
        return AFECommandChannel.AFECommandChannel_7 if g == 'M' else AFECommandChannel.AFECommandChannel_6

    @staticmethod
    def _get_U_measured_ch_id(g):
        return AFECommandChannel.AFECommandChannel_2 if g == 'M' else AFECommandChannel.AFECommandChannel_3

    @staticmethod
    def _get_I_measured_ch_id(g):
        return AFECommandChannel.AFECommandChannel_4 if g == 'M' else AFECommandChannel.AFECommandChannel_5

    @staticmethod
    def _get_general_ch_id_mask(g):
        return AFECommandChannelMask.master if g == 'M' else AFECommandChannelMask.slave

    # Changed to async def
    async def default_get_measurement(self, afe_id=35, callback=None):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True, "timeout_start_on": 10000}
        if callback is not None:
            commandKwargs["callback"] = callback
        await afe.enqueue_command(AFECommand.getSensorDataSi_last_byMask, [
            0xFF], **commandKwargs)
        await afe.enqueue_command(AFECommand.getSensorDataSi_average_byMask, [
            0xFF], **commandKwargs)

    def default_callback_return(self, msg=None):
        return msg

    # Changed to async def
    async def default_get_measurement_last(self, afe_id=35, callback=None):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 20220,
                         "preserve": True, "timeout_start_on": 5000}
        if callback is not None:
            commandKwargs["callback"] = callback
        await afe.enqueue_command(AFECommand.getSensorDataSi_last_byMask, [
            0xFF], **commandKwargs)

    async def callback_1(self, msg=None):  # Changed to async def
        msg["callback"] = None
        msg = json.dumps(msg)
        await p.print("callback:", msg)

    def default_start_measurement(self, afe_id=35,
                                  enable_temperature_loop=True,
                                  enable_offset_for_sipm_from_file=False,
                                  refresh_rate_ms=5000,
                                  **args):
        pass

    async def default_hv_set(self, afe_id=35, enable=False):  # Changed to async def
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        for g in ["M", "S"]:
            await afe.enqueue_gpio_set(afe.AFEGPIO_EN_HV0 if g ==
                                       'M' else afe.AFEGPIO_EN_HV1, 1 if enable else 0)

    # Changed to async def
    async def default_cal_in_set(self, afe_id=35, enable=False):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        for g in ["M", "S"]:
            await afe.enqueue_gpio_set(afe.AFEGPIO_EN_CAL_IN0 if g ==
                                       'M' else afe.AFEGPIO_EN_CAL_IN1, 1 if enable else 0)

    # Changed to async def
    async def default_set_dac(self, afe_id=35, dac_master=3000, dac_slave=3000):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        await afe.logger.log(VerbosityLevel["INFO"],
                             {
            "device_id": afe.device_id,
            "timestamp_ms": millis(),
            "info": "default_set_dac"
        })
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": False, "timeout_start_on_send_ms": 2000}

        for g in ["M", "S"]:
            await afe.enqueue_u16_for_channel(AFECommand.setDACValueRaw_bySubdeviceMask, self._get_subdevice_ch_id(
                g), dac_master if g == 'M' else dac_slave, **commandKwargs)
            await afe.enqueue_command(AFECommand.setDAC_bySubdeviceMask, [
                self._get_subdevice_ch_id(g), 1], **commandKwargs)
            await afe.enqueue_gpio_set(afe.AFEGPIO_EN_HV0 if g ==
                                       'M' else afe.AFEGPIO_EN_HV1, 1, **commandKwargs)

    async def afe_set_sipm_voltage_si(self, afe_id, afe_subdevice: AFECommandSubdevice, voltage, **kwargs):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True, "timeout_start_on_send_ms": 2000}
        await afe.enqueue_float_for_channel(AFECommand.setDACValueSi_bySubdeviceMask, afe_subdevice, voltage, **commandKwargs)
    
    async def afe_set_sipm_target_voltage_si(self, afe_id, afe_subdevice: AFECommandSubdevice, voltage, **kwargs):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True, "timeout_start_on_send_ms": 2000}
        await afe.enqueue_float_for_channel(AFECommand.setDACTargetSi_bySubdeviceMask, afe_subdevice, voltage, **commandKwargs)
        
    async def default_start_temperature_loop(self, afe_id=35, status=1, subdevice=AFECommandSubdevice.AFECommandSubdevice_both, **commandKwargs):
        if not subdevice:
            return
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        await afe.logger.log(VerbosityLevel["INFO"],
                             {
            "device_id": afe.device_id,
            "timestamp_ms": millis(),
            "info": "default_start_temperature_loop"
        })
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True, "timeout_start_on_send_ms": 2000}

        await afe.enqueue_command(AFECommand.setTemperatureLoopForChannelState_byMask_asStatus, [
            subdevice, 1 if status else 0], **commandKwargs)

    async def start_afe_temperature_loop(self, afe_id, afe_subdevice: AFECommandSubdevice, preserve=False, callback=None):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 10220,
                         preserve: preserve, "timeout_start_on_send_ms": 2000}
        if callback:
            commandKwargs.update("callback", callback)
        await afe.enqueue_command(AFECommand.setTemperatureLoopForChannelState_byMask_asStatus,
                                  [afe_subdevice, 1],
                                  **commandKwargs)

    async def stop_afe_temperature_loop(self, afe_id, afe_subdevice: AFECommandSubdevice, preserve=False, callback=None):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 10220,
                         preserve: preserve, "timeout_start_on_send_ms": 2000}
        if callback:
            commandKwargs.update("callback", callback)
        await afe.enqueue_command(AFECommand.setTemperatureLoopForChannelState_byMask_asStatus,
                                  [afe_subdevice, 0],
                                  **commandKwargs)

    # Changed to async def
    async def default_periodic_measurement_download_all(self, afe_id=35, ms=10000):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        await afe.logger.log(VerbosityLevel["INFO"],
                             {
            "device_id": afe.device_id,
            "timestamp_ms": millis(),
            "info": "default_periodic_measurement_download_all"
        })
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True, "timeout_start_on_send_ms": 2000}
        await afe.enqueue_u32_for_channel(
            AFECommand.setChannel_period_ms_byMask, 0xFF, ms, **commandKwargs)

    # Changed to async def
    async def default_setCanMsgBurstDelay_ms(self, afe_id=35, ms=10, **kwargs):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        await afe.logger.log(VerbosityLevel["INFO"],
                             {
            "device_id": afe.device_id,
            "timestamp_ms": millis(),
            "info": "default_setCanMsgBurstDelay_ms"
        })
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True,
                         "timeout_start_on_send_ms": 2000,
                         "error_callback": self.callback_afe_error}
        # commandKwargs = commandKwargs | kwargs
        # for k, v in kwargs:
        #     commandKwargs.update(k, v)
        commandKwargs.update(kwargs)
        # print(commandKwargs)
        await afe.enqueue_u32_for_channel(
            AFECommand.setCanMsgBurstDelay_ms, 0x00, ms, **commandKwargs)

    # Changed to async def
    async def default_setAfe_can_watchdog_timeout_ms(self, afe_id=35, ms=60000):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        await afe.logger.log(VerbosityLevel["INFO"],
                             {
            "device_id": afe.device_id,
            "timestamp_ms": millis(),
            "info": "default_setAfe_can_watchdog_timeout_ms"
        })
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True,
                         "timeout_start_on_send_ms": 2000,
                         "error_callback": self.callback_afe_error}
        await afe.enqueue_u32_for_channel(
            AFECommand.setAfe_can_watchdog_timeout_ms, 0x00, ms, **commandKwargs)

    async def default_accept(self, afe_id=35):  # Changed to async def
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True,
                         "timeout_start_on_send_ms": 2000,
                         "error_callback": self.callback_afe_error,
                         "callback": afe.callback_is_configured}

        await afe.enqueue_command(AFECommand.getTimestamp, None, **commandKwargs)

    async def default_get_UID(self, afe_id=35):  # Changed to async def
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True,
                         "error_callback": self.callback_afe_error
                         }

        await afe.enqueue_command(AFECommand.getSerialNumber, None, **commandKwargs)

    # Changed to async def
    async def defualt_getSyncTimestamp(self, afe_id=35):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True,
                         "error_callback": self.callback_afe_error
                         }

        await afe.enqueue_command(AFECommand.getSyncTimestamp, None, **commandKwargs)

    async def afe_clearRegulator_T_old(self, afe_id=41, afe_subdevice: AFECommandSubdevice = AFECommandSubdevice.AFECommandSubdevice_both):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True,
        }
        afe.enqueue_float_for_channel(
                        AFECommand.clearRegulator_T_old, afe_subdevice, 0.0, **commandKwargs)

    async def default_afe_pause(self, afe_id=35):  # Changed to async def
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True,
                         "error_callback": None
                         }
        await afe.enqueue_u32_for_channel(
            AFECommand.setChannel_period_ms_byMask,
            0xFF, 0, **commandKwargs)

    async def default_full(self, afe_id=35):
        afe = self.get_afe_by_id(afe_id)
        await self.powerOn()
        await self.default_afe_pause(afe_id)
        await self.default_setCanMsgBurstDelay_ms(afe_id, 0)

        await self.default_setAfe_can_watchdog_timeout_ms(afe_id, 1000000)
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        await afe.begin_configuration(timeout_ms=20000)
        await self.default_get_UID(afe_id)
        await self.default_procedure(afe_id)
        await self.default_set_dac(afe_id)
        
        temp_loop_enabled_master = afe.configuration["M"].get("temp_loop_enabled")
        temp_loop_enabled_slave = afe.configuration["S"].get("temp_loop_enabled")
        if afe.configuration["M"].get("fixed_V"):
            temp_loop_enabled_master = None # disable
        if afe.configuration["S"].get("fixed_V"):
            temp_loop_enabled_slave = None # disable
        temp_loop_subdev = 0x00
        if temp_loop_enabled_master and temp_loop_enabled_master:
            # Enable both
            temp_loop_subdev = AFECommandSubdevice.AFECommandSubdevice_both
            await self.default_start_temperature_loop(afe_id, 1, temp_loop_subdev)
        else:
            # Enable and disable
            await self.default_start_temperature_loop(afe_id, status=temp_loop_enabled_master, subdevice=AFECommandSubdevice.AFECommandSubdevice_master)
            await self.default_start_temperature_loop(afe_id, status=temp_loop_enabled_slave, subdevice=AFECommandSubdevice.AFECommandSubdevice_slave)
        await self.default_setCanMsgBurstDelay_ms(afe_id, 50)
        await self.default_accept(afe_id)
        await self.defualt_getSyncTimestamp(afe_id)

    async def default_configure_afe(self, afe_id=35, **kwargs):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return -1  # return Error
        await self.default_setCanMsgBurstDelay_ms(afe_id, 0)
        await afe.begin_configuration(timeout_ms=20000)
        await self.default_get_UID(afe_id)
        # print("X")
        await self.default_procedure(afe_id)
        # print("Y")
        await self.default_setCanMsgBurstDelay_ms(afe_id, 50)
        await self.default_accept(afe_id)
        await self.defualt_getSyncTimestamp(afe_id)

        return None

    async def reset(self, afe_id=35):  # Changed to async def
        for afe in self.afe_devices:
            if afe.device_id == afe_id:
                await afe.enqueue_command(0x03)

    async def test1(self, afe_id=35, command=0xF8):  # Changed to async def
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        await afe.enqueue_command(command)

    async def test2(self, afe_id=35, command=0xF9):  # Changed to async def
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        await afe.enqueue_command(command, preserve=True)

    async def test3(self, afe_id=35, command=0xF7, mask=0xFF):  # Changed to async def
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return

        await afe.enqueue_command(command, [mask], preserve=True)

    async def test4(self, afe_id=35):  # Changed to async def
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        await self.reset(afe_id)
        pyb.delay(500)
        self.default_procedure(afe_id)
        self.default_set_dac(afe_id)
        for i in range(10):
            p.print("get measurement")
            self.default_get_measurement(afe_id)
            pyb.delay(500)

    async def d(self, cmd, data=None):  # Changed to async def
        afe = self.get_afe_by_id(35)
        if afe is None:
            return
        await afe.enqueue_command(cmd, data, preserve=True)

    async def callback_afe_error(self, kwargs=None):  # Changed to async def
        await p.print("callback_afe_error: {}".format(kwargs))
        afe: AFEDevice = kwargs["afe"]
        await afe.restart_device()

    async def default_procedure(self, afe_id=35, **kwargs):  # Changed to async def
        """
        Sets up the default procedure for an AFE device.

        This function configures various settings for the specified AFE,
        including calibration data, channel settings, and averaging modes.
        It reads calibration data from files, applies it to the AFE, and
        sets up default configurations for channels and averaging.

        Args:
            afe_id (int, optional): The ID of the AFE to configure.
                Defaults to 35.
        """
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return

        configuration = await get_configuration_from_files(afe_id)
        afe.configuration = configuration.copy()
        await afe.logger.log(VerbosityLevel["INFO"],
                             {
            "device_id": afe.device_id,
            "timestamp_ms": millis(),
            "info": "default_procedure",
            "msg": configuration
        })
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": False,
                         "timeout_start_on_send_ms": 3000,
                         "callback_error": self.callback_afe_error}

        if kwargs:
            commandKwargs.update(kwargs)

        for g in ["M", "S"]:
            ch_id = None
            avg_number = 256
            time_sample_ms = 1000
            for k, v in afe.configuration[g].items():
                ch_id = 0x00
                ks = k.split(" ")[0]
                unit = None
                if len(k.split(" ")) > 1:
                    unit = k.split(" ")[1]
                    unit = extract_bracketed(unit)
                    if len(unit):
                        unit = unit[0]
                    else:
                        unit = None
                if unit:
                    v = convert_to_si(v, unit)
                # print("Loading for AFE{}:{} => {} {} [{}]".format(afe_id,g,k,v,unit))
                if ks == "T_measured_a":
                    await afe.enqueue_float_for_channel(AFECommand.setChannel_a_byMask, self._get_T_measured_ch_id(g), v, **commandKwargs)
                elif ks == "T_measured_b":
                    await afe.enqueue_float_for_channel(
                        AFECommand.setChannel_b_byMask, self._get_T_measured_ch_id(g), v, **commandKwargs)
                elif ks == "offset":
                    await afe.enqueue_u8_for_channel(
                        AFECommand.setAD8402Value_byte_byMask, self._get_subdevice_ch_id(g), int(v), **commandKwargs)
                elif ks == "U_measured_a":
                    await afe.enqueue_float_for_channel(
                        AFECommand.setChannel_a_byMask, self._get_U_measured_ch_id(g), v, **commandKwargs)
                elif ks == "U_measured_b":
                    await afe.enqueue_float_for_channel(
                        AFECommand.setChannel_b_byMask, self._get_U_measured_ch_id(g), v, **commandKwargs)
                elif ks == "I_measured_a":
                    await afe.enqueue_float_for_channel(
                        AFECommand.setChannel_a_byMask, self._get_I_measured_ch_id(g), v, **commandKwargs)
                elif ks == "I_measured_b":
                    await afe.enqueue_float_for_channel(
                        AFECommand.setChannel_b_byMask, self._get_I_measured_ch_id(g), v, **commandKwargs)
                elif ks == "U_set_a":
                    await afe.enqueue_float_for_channel(
                        AFECommand.setRegulator_a_dac_byMask, self._get_subdevice_ch_id(g), v, **commandKwargs)
                elif ks == "U_set_b":
                    await afe.enqueue_float_for_channel(
                        AFECommand.setRegulator_b_dac_byMask, self._get_subdevice_ch_id(g), v, **commandKwargs)
                elif ks == "V_opt":
                    await afe.enqueue_float_for_channel(
                        AFECommand.setRegulator_V_opt_byMask, self._get_subdevice_ch_id(g), v, **commandKwargs)
                elif ks == "dV/dT":
                    await afe.enqueue_float_for_channel(
                        AFECommand.setRegulator_dV_dT_byMask, self._get_subdevice_ch_id(g), v, **commandKwargs)
                elif ks == "T_opt":
                    await afe.enqueue_float_for_channel(
                        AFECommand.setRegulator_T_opt_byMask, self._get_subdevice_ch_id(g), v, **commandKwargs)
                elif ks == "avg_number":  # Maximum nuber of samples used in averaging
                    avg_number = v
                    if v:
                        avg_number = v
                    else:
                        avg_number = 256
                    avg_number = int(round(avg_number))
                    continue
                elif ks == "avg_mode":
                    if not v:
                        v = "NONE"
                    avg_mode = AFECommandAverage[v]
                    await afe.enqueue_command(AFECommand.setAveragingMode_byMask, [self._get_subdevice_ch_id(g),
                                                                                   avg_mode
                                                                                   ], **commandKwargs)
                elif ks == "avg_alpha":  # Average parameter, usually weight
                    ch_id = self._get_general_ch_id_mask(g)
                    if v:
                        await afe.enqueue_float_for_channel(
                            AFECommand.setAveragingAlpha_byMask, ch_id, v, **commandKwargs)
                    else:
                        await afe.enqueue_float_for_channel(
                            AFECommand.setAveragingAlpha_byMask, ch_id, 1.0/(10000*100.0), **commandKwargs)
                elif ks == "time_sample":  # time sample
                    if v:
                        time_sample_ms = v*1000 # to ms
                    else:
                        time_sample_ms = 1000
                    time_sample_ms = int(round(time_sample_ms))
                    await afe.enqueue_u32_for_channel(
                        AFECommand.setChannel_dt_ms_byMask, self._get_general_ch_id_mask(g), time_sample_ms, **commandKwargs)
                elif ks == "dT":
                    await afe.enqueue_float_for_channel(
                        AFECommand.setRegulator_dT_byMask, self._get_subdevice_ch_id(g), v, **commandKwargs)
                elif ks == "V_offset":
                    await afe.enqueue_float_for_channel(
                        AFECommand.setRegulator_V_offset_byMask, self._get_subdevice_ch_id(g), v, **commandKwargs)
                else:
                    continue
                # for uch in afe.unmask_channel(ch_id):
                #     # await self.logger.log(VerbosityLevel["DEBUG"], {
                #     await p.print({
                #         "device_id": afe.device_id,
                #         "timestamp_ms": millis(),
                #         "debug": "AFE {} {} Loading {} (CH{} ? {}) value {}".format(
                #             afe_id, g, k, uch, e_ADC_CHANNEL[uch], v)
                #     })

            await afe.enqueue_u32_for_channel(
                AFECommand.setAveraging_max_dt_ms_byMask, self._get_general_ch_id_mask(g), int(round(time_sample_ms * avg_number)), **commandKwargs)
            await afe.enqueue_float_for_channel(
                AFECommand.setChannel_multiplicator_byMask, self._get_general_ch_id_mask(g), 1.0, **commandKwargs)

        await afe.enqueue_command(AFECommand.startADC, [
            0xFF, 0xFF], **commandKwargs)

    async def parse(self, msg):  # Changed to async def
        await p.print("Parsed: {}".format(msg))

    async def send_back_data(self, afe_id: int):  # Changed to async def
        """
        Sends back the last received message from a specific AFE.

        Args:
            afe_id (int): The ID of the AFE from which to send the last message.
        """
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        toSend = afe.executed.copy()  # get all executed commands
        afe.executed = []  # clear executed commands

        await p.print("Send back: {}".format(json.dumps(toSend)))

    # async def start_periodic_measurement_by_config(self, afe_id=35):
    #     afe = self.get_afe_by_id(afe_id)
    #     if afe is None:
    #         return -1
    #     await afe.start_periodic_measurement_by_config()


    async def main_process(self, timer=None):
        # Ensure message is dequeued before processing
        await self._dequeue_message_copy(0)
        await self.discover_devices_async()  # Changed to async version
        await self.process_received_messages(0)
        if self.afe_manage_active:
            for afe in self.afe_devices:
                await afe.manage_state()
                if self.use_automatic_restart:
                    if not afe.is_configuration_started:
                        await self.default_full(afe_id=afe.device_id)
                    if afe.configuration["M"].get("automatic_restart"):
                        if afe.is_configured and afe.periodic_measurement_download_is_enabled is False:
                            afe.periodic_measurement_download_is_enabled = True
                            await afe.start_periodic_measurement_by_config()

        if self.curent_function is not None:  # check if function is running
            if is_timeout(self.curent_function_timestamp_ms, self.curent_function_timeout_ms):
                self.curent_function = None
                self.curent_function_retval = "timeout"

    async def main_loop(self):
        while self.run:
            await self.main_process()
            wdt.feed()
            await uasyncio.sleep_ms(self.main_loop_yield_ms)


# Changed to async def
async def initialize_can_hub(can_bus: pyb.CAN, logger, use_rxcallback=True, **kwargs):
    """ Initialize the CAN bus and HUB. """
    can_bus.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
                 sjw=1, bs1=7, bs2=2, auto_restart=True)
    # can_bus.setfilter(0, can_bus.MASK32, 0, (0, 0))
    can_bus.setfilter(0, can_bus.MASK16, 0, (0, 0, 0, 0))

    await p.print("CAN Bus Initialized")
    logger.verbosity_level = VerbosityLevel["INFO"]
    # logger.verbosity_level = VerbosityLevel["DEBUG"]
    # logger.print_verbosity_level = VerbosityLevel["DEBUG"]
    logger.print_verbosity_level = VerbosityLevel["CRITICAL"]
    rxDeviceCAN = RxDeviceCAN(can_bus, use_rxcallback)
    hub = HUBDevice(can_bus, logger=logger,
                    rxDeviceCAN=rxDeviceCAN,
                    use_rxcallback=use_rxcallback, **kwargs)

    return can_bus, hub, rxDeviceCAN
