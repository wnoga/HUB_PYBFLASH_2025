import json
import utime
try:
    import pyb
    import uasyncio
except:
    import asyncio as uasyncio

from my_logger import JSONLogger
from AFE import AFEDevice, AFECommand
from my_utilities import AFECommandChannel, AFECommandSubdevice, AFECommandGPIO, AFECommandAverage
from my_utilities import wdt
from my_utilities import p
from my_utilities import VerbosityLevel
from my_utilities import AFECommandChannelMask
from my_utilities import extract_bracketed
from my_utilities import millis, is_timeout, is_delay
from my_utilities import convert_to_si
from my_RxDeviceCAN import RxDeviceCAN
from my_utilities import get_configuration_from_files

@micropython.native
def calc_adc_resistor_divider(adc_val: int, r1: float, r2: float) -> float:
    """Calculates voltage from a 12-bit ADC value using a resistor divider ratio."""
    return (3.3 * adc_val / 4095.0) * ((r1 + r2) / r1)

@micropython.native
def set_power_pins(state: bool):
    """Controls the power state via GPIO pins E12 and E10."""
    pin_e12 = pyb.Pin(pyb.Pin.cpu.E12, pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
    pin_e10 = pyb.Pin(pyb.Pin.cpu.E10, pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
    if state:
        pin_e12.value(1)
        pin_e10.value(0)
    else:
        pin_e12.value(0)
        pin_e10.value(1)

@micropython.native
def get_subdevice_ch_id(group: str) -> int:
    return (
        AFECommandSubdevice.AFECommandSubdevice_master
        if group == 'M'
        else AFECommandSubdevice.AFECommandSubdevice_slave
    )

@micropython.native
def get_T_measured_ch_id(group: str) -> int:
    return (
        AFECommandChannel.AFECommandChannel_7
        if group == 'M'
        else AFECommandChannel.AFECommandChannel_6
    )

@micropython.native
def get_U_measured_ch_id(group: str) -> int:
    return (
        AFECommandChannel.AFECommandChannel_2
        if group == 'M'
        else AFECommandChannel.AFECommandChannel_3
    )

@micropython.native
def get_I_measured_ch_id(group: str) -> int:
    return (
        AFECommandChannel.AFECommandChannel_4
        if group == 'M'
        else AFECommandChannel.AFECommandChannel_5
    )

@micropython.native
def get_general_ch_id_mask(group: str) -> int:
    return AFECommandChannelMask.master if group == 'M' else AFECommandChannelMask.slave

@micropython.native
def get_float_cmd_map(g):
    subdev_ch = get_subdevice_ch_id(g)
    gen_mask = get_general_ch_id_mask(g)
    float_cmd_map = {
        "T_measured_a": (AFECommand.setChannel_a_byMask, get_T_measured_ch_id(g)),
        "T_measured_b": (AFECommand.setChannel_b_byMask, get_T_measured_ch_id(g)),
        "U_measured_a": (AFECommand.setChannel_a_byMask, get_U_measured_ch_id(g)),
        "U_measured_b": (AFECommand.setChannel_b_byMask, get_U_measured_ch_id(g)),
        "I_measured_a": (AFECommand.setChannel_a_byMask, get_I_measured_ch_id(g)),
        "I_measured_b": (AFECommand.setChannel_b_byMask, get_I_measured_ch_id(g)),
        "U_set_a": (AFECommand.setRegulator_a_dac_byMask, subdev_ch),
        "U_set_b": (AFECommand.setRegulator_b_dac_byMask, subdev_ch),
        "V_opt": (AFECommand.setRegulator_V_opt_byMask, subdev_ch),
        "dV/dT": (AFECommand.setRegulator_dV_dT_byMask, subdev_ch),
        "T_opt": (AFECommand.setRegulator_T_opt_byMask, subdev_ch),
        "dT": (AFECommand.setRegulator_dT_byMask, subdev_ch),
        "V_offset": (AFECommand.setRegulator_V_offset_byMask, subdev_ch),
    }
    return subdev_ch, gen_mask, float_cmd_map

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
        
        self.discovery_start_time = millis()
        self.discovery_timeout_ms = 300000

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
        self.adc_U_SIPM_MEAS = pyb.ADC(pyb.Pin.cpu.A3)
        self.adc_I_SIPM_MEAS = pyb.ADC(pyb.Pin.cpu.C2)
        self.adc_VSUP_MEAS = pyb.ADC(pyb.Pin.cpu.C3)

        self.msg_to_process = None

        self.logger_sync_active = True
    
    def hub_adc_read(self):     
        retavls = {"I_SIPM_MEAS": self.adc_I_SIPM_MEAS.read(),
                   "U_SIPM_MEAS": calc_adc_resistor_divider(self.adc_U_SIPM_MEAS.read(), 1, 33),
                   "VSUP_MEAS": calc_adc_resistor_divider(self.adc_VSUP_MEAS.read(), 10, 43)}
        print(retavls)
    
    def hub_update_afe_status(self):
        for afe in self.afe_devices:
            self.get_subdevice_status(afe.device_id, AFECommandSubdevice.AFECommandSubdevice_both,addToCmd={"callback":p.print})

    async def powerOn(self):
        set_power_pins(True)
        await self.logger.log(VerbosityLevel["INFO"],
                              {
            "device_id": 0,
            "timestamp_ms": millis(),
            "info": "powerOn"
        })
        

    async def powerOff(self):
        set_power_pins(False)
        await self.logger.log(VerbosityLevel["INFO"],
                              {
            "device_id": 0,
            "timestamp_ms": millis(),
            "info": "powerOff"
        })
        
    async def reset_all(self):
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

    async def get_subdevice_status(self, afe_id, subdevice_mask, addToCmd=None, callback=None):
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
        if addToCmd:
            commandKwargs.update(addToCmd)
        if callback:
            commandKwargs["callback"] = callback
        
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

    async def process_received_messages(self, timer=None):
        """Process messages received from the CAN bus."""
        if not self.rx_process_active or self.msg_to_process is None:
            return

        # Pop message and reset buffer state atomically
        message = self.msg_to_process
        self.msg_to_process = None

        afe_id = (message[0] >> 2) & 0xFF  # extract AFE ID
        afe = self.get_afe_by_id(afe_id)

        if afe is None:
            # Create and register newly discovered AFE device instance
            afe = AFEDevice(self.can_interface, afe_id, logger=self.logger)
            self.afe_devices.append(afe)

            if not self.afe0:
                self.afe0 = afe

            log_payload = {
                "device_id": 0,
                "timestamp_ms": millis(),
                "info": "found new AFE %d" % afe_id,
            }
            await self.logger.log(VerbosityLevel["INFO"], log_payload)

        # Process the received data using the AFE device's handler
        await afe.process_received_data(message)
        
    async def discover_devices_async(self):
        """Periodically discover AFEs on the CAN bus with a timeout."""
        if not self.discovery_active:
            return

        now = millis()

        # Check timeout using MicroPython ticks
        if utime.ticks_diff(now, self.discovery_start_time) >= self.discovery_timeout_ms:
            await self.logger.log(
                VerbosityLevel["INFO"],
                {
                    "device_id": 0,
                    "timestamp_ms": now,
                    "message": "Discovery timeout reached (5 minutes). Stopping.",
                },
            )
            await self.stop_discovery()
            return

        # Stop if maximum device count reached
        if len(self.afe_devices) >= self.afe_devices_max:
            await self.stop_discovery()
            return

        if self.use_tx_delay and is_delay(self.last_tx_time, self.tx_delay_ms):
            return

        # Handle CAN interface state issues
        can_state = self.can_interface.state()
        if can_state > 1:
            if can_state > 2:
                log_level = VerbosityLevel["ERROR"]
                msg = "CAN bus error state %d, attempting restart." % can_state
                self.can_interface.restart()
            else:
                log_level = VerbosityLevel["WARNING"]
                msg = "CAN bus warning state %d." % can_state

            await self.logger.log(
                log_level,
                {
                    "device_id": 0,
                    "timestamp_ms": now,
                    "error": msg,
                },
            )
            return

        # Wrap discovery ID around range limits
        curr_id = self.current_discovery_id
        if curr_id > self.afe_id_max:
            curr_id = self.afe_id_min

        # Check if device ID is already online without generator allocation
        is_online = False
        for afe in self.afe_devices:
            if afe.device_id == curr_id and afe.is_online:
                is_online = True
                break

        if not is_online:
            send_result = await self.can_interface.send(
                toSend=b"\x00\x11",
                can_address=curr_id << 2,
                timeout_ms=self.tx_timeout_ms,
            )
            if send_result is None:
                self.last_tx_time = now
                await self.logger.log(
                    VerbosityLevel["DEBUG"],
                    "Sent discovery to ID: %d" % curr_id,
                )

        self.current_discovery_id = curr_id + 1

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

    async def default_get_measurement_last(self, afe_id=35, callback=None):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        cmd_kwargs = {
            "timeout_ms": 20220,
            "preserve": True,
            "timeout_start_on": 5000,
        }
        if callback is not None:
            cmd_kwargs["callback"] = callback
        await afe.enqueue_command(
            AFECommand.getSensorDataSi_last_byMask, [0xFF], **cmd_kwargs
        )

    async def callback_1(self, msg=None):
        if msg:
            msg["callback"] = None
            dumped = json.dumps(msg)
            await p.print("callback: %s" % dumped)

    def default_start_measurement(
        self,
        afe_id=35,
        enable_temperature_loop=True,
        enable_offset_for_sipm_from_file=False,
        refresh_rate_ms=5000,
        **args,
    ):
        pass

    async def default_hv_set(self, afe_id=35, enable=False):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        val = 1 if enable else 0
        await afe.enqueue_gpio_set(afe.AFEGPIO_EN_HV0, val)
        await afe.enqueue_gpio_set(afe.AFEGPIO_EN_HV1, val)

    async def default_cal_in_set(self, afe_id=35, enable=False):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        val = 1 if enable else 0
        await afe.enqueue_gpio_set(afe.AFEGPIO_EN_CAL_IN0, val)
        await afe.enqueue_gpio_set(afe.AFEGPIO_EN_CAL_IN1, val)

    async def default_set_dac(self, afe_id=35, dac_master=3000, dac_slave=3000):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return

        await afe.logger.log(
            VerbosityLevel["INFO"],
            {
                "device_id": afe.device_id,
                "timestamp_ms": millis(),
                "info": "default_set_dac",
            },
        )
        cmd_kwargs = {
            "timeout_ms": 10220,
            "preserve": False,
            "timeout_start_on_send_ms": 2000,
        }

        for g in ("M", "S"):
            subdev = get_subdevice_ch_id(g)
            dac_val = dac_master if g == "M" else dac_slave
            hv_gpio = afe.AFEGPIO_EN_HV0 if g == "M" else afe.AFEGPIO_EN_HV1

            await afe.enqueue_u16_for_channel(
                AFECommand.setDACValueRaw_bySubdeviceMask,
                subdev,
                dac_val,
                **cmd_kwargs,
            )
            await afe.enqueue_command(
                AFECommand.setDAC_bySubdeviceMask, [subdev, 1], **cmd_kwargs
            )
            await afe.enqueue_gpio_set(hv_gpio, 1, **cmd_kwargs)

    async def afe_set_sipm_voltage_si(
        self, afe_id, afe_subdevice: AFECommandSubdevice, voltage, **kwargs
    ):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        cmd_kwargs = {
            "timeout_ms": 10220,
            "preserve": True,
            "timeout_start_on_send_ms": 2000,
        }
        await afe.enqueue_float_for_channel(
            AFECommand.setDACValueSi_bySubdeviceMask,
            afe_subdevice,
            voltage,
            **cmd_kwargs,
        )

    async def afe_set_sipm_target_voltage_si(
        self, afe_id, afe_subdevice: AFECommandSubdevice, voltage, **kwargs
    ):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        cmd_kwargs = {
            "timeout_ms": 10220,
            "preserve": True,
            "timeout_start_on_send_ms": 2000,
        }
        await afe.enqueue_float_for_channel(
            AFECommand.setDACTargetSi_bySubdeviceMask,
            afe_subdevice,
            voltage,
            **cmd_kwargs,
        )

    async def default_start_temperature_loop(
        self,
        afe_id=35,
        status=1,
        subdevice=AFECommandSubdevice.AFECommandSubdevice_both,
        **commandKwargs,
    ):
        if not subdevice:
            return
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return

        await afe.logger.log(
            VerbosityLevel["INFO"],
            {
                "device_id": afe.device_id,
                "timestamp_ms": millis(),
                "info": "default_start_temperature_loop",
            },
        )
        cmd_kwargs = {
            "timeout_ms": 10220,
            "preserve": True,
            "timeout_start_on_send_ms": 2000,
        }

        await afe.enqueue_command(
            AFECommand.setTemperatureLoopForChannelState_byMask_asStatus,
            [subdevice, 1 if status else 0],
            **cmd_kwargs,
        )

    async def default_start_temperature_ramp(
        self,
        afe_id=35,
        status=1,
        subdevice=AFECommandSubdevice.AFECommandSubdevice_both,
        **commandKwargs,
    ):
        if not subdevice:
            return
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return

        await afe.logger.log(
            VerbosityLevel["INFO"],
            {
                "device_id": afe.device_id,
                "timestamp_ms": millis(),
                "info": "default_start_temperature_loop",
            },
        )
        cmd_kwargs = {
            "timeout_ms": 10220,
            "preserve": True,
            "timeout_start_on_send_ms": 2000,
        }

        await afe.enqueue_command(
            AFECommand.setRegulator_ramp_enabled_byMask,
            [subdevice, 1 if status else 0],
            **cmd_kwargs,
        )

    async def start_afe_temperature_loop(
        self,
        afe_id,
        afe_subdevice: AFECommandSubdevice,
        preserve=False,
        callback=None,
    ):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        cmd_kwargs = {
            "timeout_ms": 10220,
            "preserve": preserve,
            "timeout_start_on_send_ms": 2000,
        }
        if callback:
            cmd_kwargs["callback"] = callback

        await afe.enqueue_command(
            AFECommand.setTemperatureLoopForChannelState_byMask_asStatus,
            [afe_subdevice, 1],
            **cmd_kwargs,
        )

    async def stop_afe_temperature_loop(
        self,
        afe_id,
        afe_subdevice: AFECommandSubdevice,
        preserve=False,
        callback=None,
    ):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        cmd_kwargs = {
            "timeout_ms": 10220,
            "preserve": preserve,
            "timeout_start_on_send_ms": 2000,
        }
        if callback:
            cmd_kwargs["callback"] = callback

        await afe.enqueue_command(
            AFECommand.setTemperatureLoopForChannelState_byMask_asStatus,
            [afe_subdevice, 0],
            **cmd_kwargs,
        )

    async def default_periodic_measurement_download_all(
        self, afe_id=35, ms=10000
    ):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        await afe.logger.log(
            VerbosityLevel["INFO"],
            {
                "device_id": afe.device_id,
                "timestamp_ms": millis(),
                "info": "default_periodic_measurement_download_all",
            },
        )
        cmd_kwargs = {
            "timeout_ms": 10220,
            "preserve": True,
            "timeout_start_on_send_ms": 2000,
        }
        await afe.enqueue_u32_for_channel(
            AFECommand.setChannel_period_ms_byMask, 0xFF, ms, **cmd_kwargs
        )

    async def default_setCanMsgBurstDelay_ms(self, afe_id=35, ms=10, **kwargs):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        await afe.logger.log(
            VerbosityLevel["INFO"],
            {
                "device_id": afe.device_id,
                "timestamp_ms": millis(),
                "info": "default_setCanMsgBurstDelay_ms",
            },
        )
        cmd_kwargs = {
            "timeout_ms": 10220,
            "preserve": True,
            "timeout_start_on_send_ms": 2000,
            "error_callback": self.callback_afe_error,
        }
        if kwargs:
            cmd_kwargs.update(kwargs)

        await afe.enqueue_u32_for_channel(
            AFECommand.setCanMsgBurstDelay_ms, 0x00, ms, **cmd_kwargs
        )

    async def default_setAfe_can_watchdog_timeout_ms(
        self, afe_id=35, ms=60000
    ):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        await afe.logger.log(
            VerbosityLevel["INFO"],
            {
                "device_id": afe.device_id,
                "timestamp_ms": millis(),
                "info": "default_setAfe_can_watchdog_timeout_ms",
            },
        )
        cmd_kwargs = {
            "timeout_ms": 10220,
            "preserve": True,
            "timeout_start_on_send_ms": 2000,
            "error_callback": self.callback_afe_error,
        }
        await afe.enqueue_u32_for_channel(
            AFECommand.setAfe_can_watchdog_timeout_ms, 0x00, ms, **cmd_kwargs
        )

    async def default_accept(self, afe_id=35):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        cmd_kwargs = {
            "timeout_ms": 10220,
            "preserve": True,
            "timeout_start_on_send_ms": 2000,
            "error_callback": self.callback_afe_error,
            "callback": afe.callback_is_configured,
        }
        await afe.enqueue_command(AFECommand.getTimestamp, None, **cmd_kwargs)

    async def default_get_UID(self, afe_id=35):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        cmd_kwargs = {
            "timeout_ms": 10220,
            "preserve": True,
            "error_callback": self.callback_afe_error,
        }
        await afe.enqueue_command(AFECommand.getSerialNumber, None, **cmd_kwargs)

    async def default_get_sync_timestamp(self, afe_id=35):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        cmd_kwargs = {
            "timeout_ms": 10220,
            "preserve": True,
            "error_callback": self.callback_afe_error,
        }
        await afe.enqueue_command(AFECommand.getSyncTimestamp, None, **cmd_kwargs)

    # # Alias to retain backward compatibility with original typo function call
    # defualt_getSyncTimestamp = default_get_sync_timestamp

    async def afe_clearRegulator_T_old(
        self,
        afe_id=41,
        afe_subdevice: AFECommandSubdevice = AFECommandSubdevice.AFECommandSubdevice_both,
    ):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        cmd_kwargs = {
            "timeout_ms": 10220,
            "preserve": True,
        }
        await afe.enqueue_float_for_channel(
            AFECommand.clearRegulator_T_old, afe_subdevice, 0.0, **cmd_kwargs
        )

    async def default_afe_pause(self, afe_id=35):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        cmd_kwargs = {
            "timeout_ms": 10220,
            "preserve": True,
            "error_callback": None,
        }
        await afe.enqueue_u32_for_channel(
            AFECommand.setChannel_period_ms_byMask, 0xFF, 0, **cmd_kwargs
        )

    async def default_full(self, afe_id=35):
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

        cfg_m = afe.configuration.get("M", {})
        cfg_s = afe.configuration.get("S", {})

        temp_loop_enabled_master = cfg_m.get("temp_loop_enabled")
        temp_loop_enabled_slave = cfg_s.get("temp_loop_enabled")
        temp_loop_fixed_V_master = cfg_m.get("fixed_V")
        temp_loop_fixed_V_slave = cfg_s.get("fixed_V")

        if temp_loop_enabled_master and temp_loop_enabled_slave:
            await self.default_start_temperature_loop(
                afe_id, 1, AFECommandSubdevice.AFECommandSubdevice_both
            )
        else:
            await self.default_start_temperature_loop(
                afe_id,
                status=temp_loop_enabled_master,
                subdevice=AFECommandSubdevice.AFECommandSubdevice_master,
            )
            await self.default_start_temperature_loop(
                afe_id,
                status=temp_loop_enabled_slave,
                subdevice=AFECommandSubdevice.AFECommandSubdevice_slave,
            )

        if temp_loop_fixed_V_master and not temp_loop_enabled_master:
            await p.print(
                "#### Try set fixed voltage %s to master"
                % temp_loop_fixed_V_master
            )
            await self.afe_set_sipm_target_voltage_si(
                afe_id,
                AFECommandSubdevice.AFECommandSubdevice_master,
                temp_loop_fixed_V_master,
            )

        if temp_loop_fixed_V_slave and not temp_loop_enabled_slave:
            await p.print(
                "#### Try set fixed voltage %s to slave"
                % temp_loop_fixed_V_slave
            )
            await self.afe_set_sipm_target_voltage_si(
                afe_id,
                AFECommandSubdevice.AFECommandSubdevice_slave,
                temp_loop_fixed_V_slave,
            )

        await self.default_setCanMsgBurstDelay_ms(afe_id, 50)
        await self.default_accept(afe_id)
        await self.default_get_sync_timestamp(afe_id)

    async def default_configure_afe(self, afe_id=35, **kwargs):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return -1  # return Error

        await self.default_setCanMsgBurstDelay_ms(afe_id, 0)
        await afe.begin_configuration(timeout_ms=20000)
        await self.default_get_UID(afe_id)
        await self.default_procedure(afe_id)
        await self.default_setCanMsgBurstDelay_ms(afe_id, 50)
        await self.default_accept(afe_id)
        await self.default_get_sync_timestamp(afe_id)

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
            await p.print("get measurement")
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

    async def default_procedure(self, afe_id=35, **kwargs):
        """Sets up the default procedure for an AFE device using dispatch mapping."""
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return

        configuration = await get_configuration_from_files(afe_id)
        afe.configuration = (
            configuration.copy()
            if hasattr(configuration, "copy")
            else configuration
        )

        await afe.logger.log(
            VerbosityLevel["INFO"],
            {
                "device_id": afe.device_id,
                "timestamp_ms": millis(),
                "info": "default_procedure",
                "msg": configuration,
            },
        )

        command_kwargs = {
            "timeout_ms": 10220,
            "preserve": False,
            "timeout_start_on_send_ms": 3000,
            "callback_error": self.callback_afe_error,
        }
        if kwargs:
            command_kwargs.update(kwargs)

        for g in ("M", "S"):
            avg_number = 256
            time_sample_ms = 1000
            subdev_ch, gen_mask, float_cmd_map = get_float_cmd_map(g)
            group_cfg = afe.configuration.get(g, {})

            for k, v in group_cfg.items():
                parts = k.split(" ")
                ks = parts[0]

                unit = None
                if len(parts) > 1:
                    brackets = extract_bracketed(parts[1])
                    unit = brackets[0] if brackets else None

                if unit:
                    v = convert_to_si(v, unit)

                # 1. Handle Standard Float Commands via Dispatch Lookup
                if ks in float_cmd_map:
                    cmd_enum, target_ch = float_cmd_map[ks]
                    await afe.enqueue_float_for_channel(
                        cmd_enum, target_ch, v, **command_kwargs
                    )

                # 2. Handle Special-Case Parameters
                elif ks == "offset":
                    await afe.enqueue_u8_for_channel(
                        AFECommand.setAD8402Value_byte_byMask,
                        subdev_ch,
                        int(v),
                        **command_kwargs,
                    )
                elif ks == "avg_number":
                    avg_number = int(round(v)) if v else 256
                elif ks == "avg_mode":
                    avg_mode = AFECommandAverage[v if v else "NONE"]
                    await afe.enqueue_command(
                        AFECommand.setAveragingMode_byMask,
                        [subdev_ch, avg_mode],
                        **command_kwargs,
                    )
                elif ks == "avg_alpha":
                    alpha = v if v else 1e-6
                    await afe.enqueue_float_for_channel(
                        AFECommand.setAveragingAlpha_byMask,
                        gen_mask,
                        alpha,
                        **command_kwargs,
                    )
                elif ks == "time_sample":
                    time_sample_ms = int(round(v * 1000)) if v else 1000
                    await afe.enqueue_u32_for_channel(
                        AFECommand.setChannel_dt_ms_byMask,
                        gen_mask,
                        time_sample_ms,
                        **command_kwargs,
                    )

            max_dt_ms = int(round(time_sample_ms * avg_number))
            
            await afe.enqueue_u32_for_channel(
                AFECommand.setAveraging_max_dt_ms_byMask,
                gen_mask,
                max_dt_ms,
                **command_kwargs,
            )
            
            await afe.enqueue_u32_for_channel(
                AFECommand.setTemperatureLoop_loop_every_ms,
                gen_mask,
                100,
                **command_kwargs,
            )

        await afe.enqueue_u32_for_channel(
            AFECommand.startADC, 0xFF, 250, **command_kwargs
        )

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
        await self.discover_devices_async()
        await self.process_received_messages(0)
        
        if self.afe_manage_active:
            # Cache method and attribute lookups locally to avoid micro-allocations in the loop
            use_auto_restart = self.use_automatic_restart
            default_full = self.default_full
            
            for afe in self.afe_devices:
                await afe.manage_state()
                if use_auto_restart:
                    if not afe.is_configuration_started:
                        await default_full(afe_id=afe.device_id)
                    # Eliminate chained .get() lookups by caching configuration dictionary
                    if afe.configuration["M"].get("automatic_restart"):
                        if afe.is_configured and not afe.periodic_measurement_download_is_enabled:
                            afe.periodic_measurement_download_is_enabled = True
                            await afe.start_periodic_measurement_by_config()

        # Localize current function attributes to avoid repeated self-lookups
        curr_func = self.curent_function
        if curr_func is not None:
            if is_timeout(self.curent_function_timestamp_ms, self.curent_function_timeout_ms):
                self.curent_function = None
                self.curent_function_retval = "timeout"

    async def main_loop(self):
        # Pre-cache methods and variables for the infinite loop
        main_process = self.main_process
        sleep_ms = uasyncio.sleep_ms
        yield_ms = self.main_loop_yield_ms
        wdt_feed = wdt.feed

        while self.run:
            await main_process()
            await sleep_ms(yield_ms)
            wdt_feed()


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