import json
import time
import struct
import random
import pyb
# import machine # machine is not directly used here, pyb.millis is
import micropython
import uasyncio

from my_utilities import AFECommand, AFECommandGPIO, AFECommandChannel, AFECommandSubdevice, JSONLogger
from my_utilities import millis, is_timeout, is_delay
from my_utilities import e_ADC_CHANNEL, CommandStatus, ResetReason
from my_utilities import p
from my_utilities import VerbosityLevel
from my_utilities import SensorChannel, AFECommandChannelMask, AFECommandAverage
from my_utilities import extract_bracketed
from my_utilities import rtc, rtc_synced, rtc_unix_timestamp
from my_utilities import get_e_ADC_CHANNEL
from my_RxDeviceCAN import RxDeviceCAN

class AFEDevice:
    def __init__(self, can_interface: RxDeviceCAN, device_id, logger: JSONLogger, config_path=None):
        if not isinstance(can_interface, RxDeviceCAN):
            raise RuntimeError("can_interface must be an instance of RxDeviceCAN")
        self.can_interface = can_interface
        self.device_id = device_id  # Channel number
        self.unique_id = [0, 0, 0]  # 3*32-bit = 96-bit STM32 Unique ID
        self.unique_id_str = None
        self.logger: JSONLogger = logger
        self.config_path = config_path  # Path to the config file

        self.last_sync_afe_timestamp_ms = None

        # Use this if communication is faster than the AFE
        self.use_tx_delay = True
        self.tx_timeout_ms = 50

        self.can_address = device_id << 2
        self.configuration = {}
        self.is_online = False
        self.total_channels = 8
        self.firmware_version = None
        self.version_checked = False
        self.channels = [SensorChannel(x) for x in range(self.total_channels)]
        self.is_configured = False
        self.is_configuration_started = False
        self.configuration_start_timestamp_ms = 0
        self.configuration_timeout_ms = 100000
        self.default_command_timeout_ms = 1000
        self.default_can_timeout_ms = 1000
        self.verbose = 2
        self.blink_status = 0

        self.periodic_measurement_download_is_enabled = False
        self.temperatureLoop_master_is_enabled = False
        self.temperatureLoop_slave_is_enabled = False

        self.to_execute = []
        self.execute_timestamp = 0
        self.executing = None

        self.executed_max_len = 100
        self.save_periodic_data = True
        self.periodic_data = {}

        self.debug_machine_control_msg = [{}, {}]

        self.AFEGPIO_EN_HV0 = AFECommandGPIO(port="PORTB", pin=10)
        self.AFEGPIO_EN_HV1 = AFECommandGPIO(port="PORTB", pin=11)
        self.AFEGPIO_EN_CAL_IN0 = AFECommandGPIO(port="PORTB", pin=15)
        self.AFEGPIO_EN_CAL_IN1 = AFECommandGPIO(port="PORTB", pin=14)
        self.AFEGPIO_blink = AFECommandGPIO(port="PORTA", pin=9)

        self.afe_config = None
        self.afe_first_configured = None

        self.use_afe_can_watchdog = True
        self.afe_can_watchdog_timestamp_ms = 0
        self.afe_can_watchdog_timeout_ms = 20*1000
        
        self.init_timestamp_ms = 0
        self.init_wait_ms = 5000

        self.current_status_last_data = [
            {"timestamp_ms": None, "value": None} for x in range(self.total_channels)]
        self.current_status_average_data = [
            {"timestamp_ms": None, "value": None} for x in range(self.total_channels)]

        self.init_after_restart()

    def default_log_dict(self, extra_fields=None, timestamp_ms=None, unix_timestamp=None):
        toReturn = {
            "device_id": self.device_id,
            "timestamp_ms": timestamp_ms or millis(),
            "rtc_timestamp": unix_timestamp or rtc_unix_timestamp()
        }
        if extra_fields:
            for k, v in extra_fields.items():
                toReturn[k] = v
        return toReturn

    def trim_dict_for_logger(self, executing):
        trimmed = executing.copy()
        keys_to_trim = ["frame", "callback", "callback_error"]
        for key in keys_to_trim:
            trimmed.pop(key, None)
        return trimmed

    async def begin_configuration(self, timeout_ms=10000):
        await self.logger.log(
            VerbosityLevel["INFO"],
            self.default_log_dict({"info": "begin_configuration"}))
        self.is_configuration_started = True
        self.configuration_timeout_ms = timeout_ms
        self.configuration_start_timestamp_ms = millis()
        self.is_configured = False

    def end_configuration(self, success=True):
        self.is_configured = success

    async def callback_is_configured(self, kwargs=None): # Changed to async def
        self.afe_first_configured = {
            "AFE_timestamp_ms": self.last_sync_afe_timestamp_ms,
            "HUB_timestamp_ms": millis()
        }
        self.end_configuration(success=True)
        await self.logger.log( # Added await
            VerbosityLevel["INFO"],
            self.default_log_dict({"info": "configured"}))

    def init_after_restart(self):
        self.channels = [SensorChannel(x) for x in range(self.total_channels)]
        self.is_configured = False
        self.is_configuration_started = False
        self.executing = None
        self.to_execute = []
        self.version_checked = False
        self.periodic_measurement_download_is_enabled = False
        self.blink_is_enabled = False
        self.temperatureLoop_master_is_enabled = False
        self.temperatureLoop_slave_is_enabled = False
        self.periodic_data = {}
        self.debug_machine_control_msg = [{}, {}]
        self.afe_first_configured = None

    def update_output(self, output, value_name, value, channel=None):
        if channel is None:
            # Store the value outside if no channel is provided
            output[value_name] = value
        else:
            # Find the correct channel and append the value
            for entry in output.get("data", []):
                if entry["channel"] == channel:
                    entry[value_name] = value
                    break
            else:
                # If channel is not found, add a new entry
                output.setdefault("data", []).append(
                    {"channel": channel, value_name: value})
        return output

    async def restart_device(self): # Changed to async def
        self.current_command = None
        self.last_command_time = 0
        await self.can_interface.send( # Added await
            bytearray([AFECommand.resetAll]), self.can_address, timeout_ms=1000)
        self.init_after_restart()

    async def print_all_channel_settings(self): # Changed to async def
        tmp = "AFE{}:\n".format(self.device_id)
        for ch in self.channels:
            tmp += "\t{}->{}\n".format(ch.name, ch.config)
        await p.print(tmp) # Added await

    async def start_periodic_measurement_for_channels(self, report_every_ms, channels=0xFF): # Changed to async def
        await self.enqueue_u32_for_channel( # Added await
            AFECommand.setSensorDataSi_periodic_average, channels, report_every_ms)

    async def start_periodic_measurement_by_config(self): # Changed to async def
        report_every_ms = {}
        for g in ["M", "S"]:
            for k, v in self.configuration[g].items():
                ks = k.split(" ")[0]
                if not ks == "report_every":
                    continue
                unit = None
                if len(k.split(" ")) > 1:
                    unit = k.split(" ")[1]
                    unit = extract_bracketed(unit)
                    if len(unit):
                        unit = unit[0]
                    else:
                        unit = None
                time_sample_ms = v
                if v is '':
                    time_sample_ms = 1000
                else:
                    if unit == "s":
                        time_sample_ms = v*1000
                    elif unit == "ms":
                        time_sample_ms = v
                    elif unit == "us":
                        time_sample_ms = v/1000
                    elif unit == "ns":
                        time_sample_ms = v/1000000
                    elif unit == "h":
                        time_sample_ms = v*1000*3600
                    elif unit == "min":
                        time_sample_ms = v*1000*60
                    elif unit == "d":
                        time_sample_ms = v*1000*3600*24
                    else:  # assume value is in SI [s]
                        time_sample_ms = v*1000
                time_sample_ms = int(round(time_sample_ms))
                report_every_ms[g] = time_sample_ms
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True,
                         "timeout_start_on_send_ms": 3000,
                         "callback_error": self.start_periodic_measurement_by_config}
        if report_every_ms.get("M") == report_every_ms.get("S"):
            await self.enqueue_u32_for_channel( # Added await
                AFECommand.setChannel_period_ms_byMask,
                0xFF, report_every_ms.get("M"), **commandKwargs)
        else:
            await self.enqueue_u32_for_channel( # Added await
                AFECommand.setChannel_period_ms_byMask,
                AFECommandChannelMask.master, report_every_ms.get("M"),
                **commandKwargs)
            await self.enqueue_u32_for_channel( # Added await
                AFECommand.setChannel_period_ms_byMask,
                AFECommandChannelMask.slave, report_every_ms.get("S"),
                **commandKwargs)


    def bytes_to_u16(self, data):
        l = len(data)
        if l == 2:
            return struct.unpack('<H', bytes(data))[0]
        elif l > 2:
            return struct.unpack('<H', bytes(data[0:2]))[0]
        else:
            return None

    # Convert byte list to 32-bit unsigned integer
    def bytes_to_u32(self, data):
        l = len(data)
        if l == 4:
            return struct.unpack('<I', bytes(data))[0]
        elif l > 4:
            return struct.unpack('<I', bytes(data[0:4]))[0]
        else:
            return None

    # Convert byte list to float
    def bytes_to_float(self, data):
        l = len(data)
        if l == 4:
            return struct.unpack('<f', bytes(data))[0]
        elif l > 4:
            return struct.unpack('<f', bytes(data[0:4]))[0]
        else:
            return None

    def unmask_channel(self, masked_channel):
        if masked_channel == 0:  # often as timestamp
            return [1 << 8]
        else:
            channels = []
            for i in range(8):
                if 0x01 & (masked_channel >> i):
                    channels.append(i)
            return channels

    def getChannelName(self, number: int) -> str:
        return e_ADC_CHANNEL.get(number, "Unknown")

    # Prepare frame payload for the AFE

    def prepare_command(self, command, data=None, chunk=1, max_chunks=1, timeout_ms=None,
                        preserve=False,
                        # startKeepOutput=False, outputRestart=False,
                        can_timeout_ms=None, callback=None, callback_error=None,
                        print=False, **kwargs):
        if data is None:
            data = []
        elif isinstance(data, int):
            data = [data]
        elif not (isinstance(data, list) and all(isinstance(i, int) for i in data)):
            data = list(map(int, data))
        timestamp_ms = millis()
        chunk_info = (max_chunks << 4) | chunk
        frame = bytearray([command, chunk_info] + data[:6])
        return {
            "command": command,  # command
            "frame": frame,  # payload
            "device_id": self.device_id,  # AFE ID
            "can_address": self.can_address,  # can address
            "timeout_ms": self.default_command_timeout_ms if timeout_ms is None else timeout_ms,
            "timestamp_ms": timestamp_ms,
            "timestamp_ms_enqueued": timestamp_ms,
            "can_timeout_ms": self.default_can_timeout_ms if can_timeout_ms is None else can_timeout_ms,
            "status": CommandStatus.NONE,
            "preserve": preserve,
            "timeout_start_on_send_ms": None,  # if not None then timestamp_ms is restarted
            "retval": None,
            "callback": callback,
            "callback_error": callback_error
        }

    async def _enqueue_command(self, command, data=None, **kwargs):
        if len(self.to_execute) > self.executed_max_len:
            self.to_execute.pop(0)
        self.to_execute.append(
            self.prepare_command(command, data, **kwargs)
        )
        return None

    async def enqueue_command(self, command, data=None, **kwargs):
        return await self._enqueue_command(command, data, **kwargs)

    async def enqueue_gpio_set(self, gpio, state, **kwargs):
        return await self.enqueue_command(AFECommand.writeGPIO,
                                    [gpio.port, gpio.pin, state], **kwargs)

    async def enqueue_float_for_channel(self, command, channel, value, **kwargs):
        return await self.enqueue_command(
            command, [channel] + list(struct.pack('<f', value)), **kwargs)

    async def enqueue_u16_for_channel(self, command, channel, value, **kwargs):
        return await self.enqueue_command(
            command, [channel] + list(struct.pack('<H', value)), **kwargs)

    async def enqueue_u32_for_channel(self, command, channel, value, **kwargs):
        return await self.enqueue_command(
            command, [channel] + list(struct.pack('<I', value)), **kwargs)

    async def executing_error_handler(self):
        self.executing["status"] = CommandStatus.ERROR
        await self.logger.log(VerbosityLevel["ERROR"],
                        self.default_log_dict(
                            {"error": "TIMEOUT", "executing": self.trim_dict_for_logger(self.executing)}))
        if "callback_error" in self.executing:
            try:
                if self.executing["callback_error"] is not None and callable(self.executing["callback_error"]):
                    await p.print("Creating task for callback_error: {}".format( # Added await
                        self.executing["callback_error"]))
                    # If callback_error can be async, create a task for it
                    uasyncio.create_task(self.executing["callback_error"](
                        {"afe": self, "afe_id": self.device_id, "executing": self.executing}))
            except Exception as e:
                await p.print("AFE executing_error_handler error invoking callback_error: {}".format(e)) # Added await
        self.executing = None

    def request_new_file(self):
        self.logger.requestNewFile()

    def request_rename_file(self, new_name_suffix):
        self.logger.requestRenameFile(new_name_suffix)

    async def execute(self, _):
        if self.to_execute and self.executing is None:
            self.execute_timestamp = millis()
            cmd = self.to_execute.pop(0)
            self.executing = cmd
            self.executing["status"] = CommandStatus.IDLE
            if self.executing["timeout_start_on_send_ms"] is not None:
                self.executing["timestamp_ms"] = millis()
                self.executing["timeout_ms"] = self.executing["timeout_start_on_send_ms"]
            try:
                if await self.can_interface.send( # Added await
                    cmd.get("frame"), cmd.get("can_address"), cmd.get("can_timeout_ms",self.default_command_timeout_ms)):
                    await self.executing_error_handler() # Added await
            except Exception as e:
                await p.print("Error executing command {} -> {} : {}".format(e,type(cmd),cmd)) # Changed to await p.print
            else:
                await self.logger.log(VerbosityLevel["DEBUG"], # Added await
                                self.default_log_dict(
                                    {"debug": "Sending {}".format(cmd)}))
    async def process_received_data(self, received_data): # Changed to async def
        command = None
        chunk_id = None
        max_chunks = None
        chunk_payload = []
        parsed_data = {}
        if True:
            data_bytes = list(bytes(received_data[3]))
            device_id = (received_data[0] >> 2) & 0xFF
            msg_from_slave = (received_data[0] >> 10) & 0x001
            if msg_from_slave != 1:
                await self.logger.log(VerbosityLevel["WARNING"], # Added await
                                self.default_log_dict({"debug": "Not from slave"}))
                return
            if device_id != self.device_id:
                return

            command = data_bytes[0]
            chunk_id = data_bytes[1] & 0x0F
            max_chunks = (data_bytes[1] >> 4) & 0x0F
            chunk_payload = data_bytes[2:]
            await self.logger.log(VerbosityLevel["DEBUG"],  # Added await
                            self.default_log_dict({"debug": "R: ID:{}; Command: 0x{:02X}: {}".format(
                device_id, command, data_bytes)}))

            if command == AFECommand.getSerialNumber:
                await self.logger.log(VerbosityLevel["WARNING"],  # Added await
                                self.default_log_dict({"debug": "R: ID:{}; Command: 0x{:02X}: {}".format(
                    device_id, command, data_bytes)}))
                chunk_data = self.bytes_to_u32(chunk_payload)
                if chunk_id == 0:
                    self.unique_id_str = None
                    self.unique_id = []
                    self.output = {}
                self.unique_id[chunk_id-1] = chunk_data
                if chunk_id == max_chunks:
                    self.is_online = True
                    self.current_command = None
                    uid0 = 0x001E0028
                    uid1 = 0x46415716
                    uid2 = 0x20353634
                    self.unique_id_str = "".join(
                        "{:08X}".format(b) for b in self.unique_id)
                    await self.logger.log(VerbosityLevel["INFO"], # Added await
                                    self.default_log_dict(
                                        {"info": {"UID": self.unique_id_str}}))
                    parsed_data["unique_id_str"] = self.unique_id_str
                    self.configuration["UID"] = self.unique_id_str

            elif command == AFECommand.getVersion:
                self.firmware_version = int("".join(map(str, chunk_payload)))
                self.version_checked = True
                parsed_data["version"] = self.firmware_version

            elif command == AFECommand.resetAll:
                self.init_after_restart()
                await self.logger.log(VerbosityLevel["ERROR"], # Added await
                                self.default_log_dict(
                                    {"error": "AFE {} was restared! Reason {}".format(device_id, ResetReason[chunk_payload[0]])}))
                await self.logger.sync() # Added await

            elif command == AFECommand.startADC:
                pass

            elif command == AFECommand.getTimestamp:
                HUB_timestamp_ms = millis()
                AFE_timestamp_ms = self.bytes_to_u32(
                    chunk_payload[1:])
                self.last_sync_afe_timestamp_ms = AFE_timestamp_ms
                parsed_data["AFE_timestamp_ms"] = AFE_timestamp_ms
                parsed_data["HUB_timestamp_ms"] = HUB_timestamp_ms

            elif command == AFECommand.getSyncTimestamp:
                if chunk_id == 1:
                    HUB_timestamp_ms = millis()
                    AFE_timestamp_ms = self.bytes_to_u32(
                        chunk_payload[1:])
                    self.last_sync_afe_timestamp_ms = AFE_timestamp_ms
                    parsed_data["AFE_timestamp_ms"] = AFE_timestamp_ms
                    parsed_data["HUB_timestamp_ms"] = HUB_timestamp_ms
                elif chunk_id == 2:
                    parsed_data["msg_recieved_by_AFE_timestamp_ms"] = self.bytes_to_u32(
                        chunk_payload[1:])
                    
            elif command == AFECommand.resetCAN:
                retval = {"reason":"AFE CAN Error","timestamp_ms":millis()}
                await self.logger.log(VerbosityLevel["ERROR"], # Added await
                    self.default_log_dict({
                        "error": "AFE {} CAN bus reset".format(device_id),
                        "retval": self.trim_dict_for_logger(retval)
                    }))

            elif command == AFECommand.setTemperatureLoopForChannelState_byMask_asStatus:
                pass

            elif command == AFECommand.getSensorDataSi_last_byMask:
                unmasked_channels = self.unmask_channel(chunk_payload[0])
                if not "last_data" in parsed_data:
                    parsed_data["last_data"] = {}
                if chunk_id == max_chunks:
                    parsed_data["last_data"].update(
                        {"timestamp_ms": self.bytes_to_u32(chunk_payload[1:])})
                else:
                    for uch in unmasked_channels:
                        parsed_data["last_data"].update(
                            {"{}".format(e_ADC_CHANNEL.get(uch)): self.bytes_to_float(chunk_payload[1:])})

            elif command == AFECommand.getSensorDataSi_average_byMask:
                unmasked_channels = self.unmask_channel(chunk_payload[0])
                if not "average_data" in parsed_data:
                    parsed_data["average_data"] = {}
                if chunk_id == max_chunks:
                    parsed_data["average_data"].update(
                        {"timestamp_ms": self.bytes_to_u32(chunk_payload[1:])})
                else:
                    for uch in unmasked_channels:
                        parsed_data["average_data"].update(
                            {"{}".format(e_ADC_CHANNEL.get(uch)): self.bytes_to_float(chunk_payload[1:])})

            elif command == AFECommand.setAD8402Value_byte_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.configuration["M" if uch == 0 else "S"]["offset [bit]"] = self.bytes_to_u16(
                        chunk_payload[1:])
                    if 0x01 & (chunk_payload[2] >> uch):
                        await self.logger.log(VerbosityLevel["ERROR"],  # Added await
                                        self.default_log_dict({"error": "AFE {}: ERROR setAD8402Value_byte_byMask for CH{}".format(
                            device_id, uch
                        )}))
                        # Error
                        self.configuration["M" if uch ==
                                           0 else "S"]["offset [bit]"] = None

            elif command == AFECommand.setAveragingMode_byMask:
                unmasked_channels = self.unmask_channel(chunk_payload[0])
                for uch in unmasked_channels:
                    averaging_mode = ''
                    for a, v in AFECommandAverage.items():
                        if v == chunk_payload[1]:
                            averaging_mode = a
                            break
                    self.channels[uch].config["averaging_mode"] = averaging_mode

            elif command == AFECommand.setAveragingAlpha_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].config["alpha"] = self.bytes_to_float(
                        chunk_payload[1:])

            elif command == AFECommand.setChannel_dt_ms_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].config["time_interval_ms"] = self.bytes_to_u32(
                        chunk_payload[1:])

            elif command == AFECommand.setChannel_a_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].config["a"] = self.bytes_to_float(
                        chunk_payload[1:])

            elif command == AFECommand.setChannel_b_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].config["b"] = self.bytes_to_float(
                        chunk_payload[1:])

            elif command == AFECommand.setChannel_multiplicator_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].config["multiplicator"] = self.bytes_to_float(
                        chunk_payload[1:])

            elif command == AFECommand.setRegulator_a_dac_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].config["a"] = self.bytes_to_float(
                        chunk_payload[1:])
                pass

            elif command == AFECommand.setRegulator_b_dac_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].config["b"] = self.bytes_to_float(
                        chunk_payload[1:])
                pass

            elif command == AFECommand.setRegulator_dV_dT_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].config["dV_dT"] = self.bytes_to_float(
                        chunk_payload[1:])
                pass

            elif command == AFECommand.setRegulator_V_opt_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].config["V_opt"] = self.bytes_to_float(
                        chunk_payload[1:])
                pass

            elif command == AFECommand.setRegulator_V_offset_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].config["V_offset"] = self.bytes_to_float(
                        chunk_payload[1:])
                pass

            elif command == AFECommand.setChannel_period_ms_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].config["period_ms"] = self.bytes_to_u32(
                        chunk_payload[1:])
                pass

            elif command == AFECommand.getSensorDataSi_periodic:
                try:
                    unmasked_channels = self.unmask_channel(chunk_payload[0])
                    if not "last_data" in self.periodic_data:
                        self.periodic_data["last_data"] = {}
                    if not "average_data" in self.periodic_data:
                        self.periodic_data["average_data"] = {}

                    if chunk_id == 1:  # Last data: data
                        self.periodic_data = {}  # Clear periodic data if new chunk set arrived
                        self.periodic_data["last_data"] = {}
                        self.periodic_data["average_data"] = {}
                        self.periodic_data["timestamp_ms"] = millis()
                        for uch in unmasked_channels:
                            self.periodic_data["last_data"].update(
                                {"{}".format(e_ADC_CHANNEL.get(uch)): self.bytes_to_float(chunk_payload[1:])})
                    elif chunk_id == 2:  # Last data: data timestamp
                        self.periodic_data["last_data"].update(
                            {"timestamp_ms": self.bytes_to_u32(chunk_payload[1:])})
                    elif chunk_id == 3:  # Average data: data
                        for uch in unmasked_channels:
                            self.periodic_data["average_data"].update(
                                {"{}".format(e_ADC_CHANNEL.get(uch)): self.bytes_to_float(chunk_payload[1:])})
                    elif chunk_id == 4:  # Average data: calculation timestamp
                        self.periodic_data["average_data"].update(
                            {"timestamp_ms": self.bytes_to_u32(chunk_payload[1:])})

                except Exception as e:
                    await p.print("Error getSensorDataSi_periodic: {}: ".format(e)) # Added await

            elif command == AFECommand.getSensorDataSiAndTimestamp_average_byMask:
                channel = chunk_payload[0]
                if chunk_id == 1:
                    value = self.bytes_to_float(chunk_payload[1:])  # value
                elif chunk_id == 2:
                    value = self.bytes_to_u32(chunk_payload[1:])  # timestamp

            elif command == AFECommand.writeGPIO:
                pass
            elif command == AFECommand.setCanMsgBurstDelay_ms:
                await self.logger.log(VerbosityLevel["INFO"], # Added await
                                self.default_log_dict({
                                    "info": "Changed CanMsgBurstDelay_ms on AFE to {}".format(
                                        self.bytes_to_u32(chunk_payload[1:]))
                                }))
                pass
            elif command == AFECommand.setAfe_can_watchdog_timeout_ms:
                self.afe_can_watchdog_timeout_ms = self.bytes_to_u32(
                    chunk_payload[1:])

            elif command == AFECommand.setAveraging_max_dt_ms_byMask:
                pass

            elif command == AFECommand.setDACValueRaw_bySubdeviceMask:
                pass

            elif command == AFECommand.setDAC_bySubdeviceMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    if chunk_payload[2] & (1 << uch):
                        pass
                    else:
                        pass

            elif command == AFECommand.debug_machine_control:
                channel = chunk_payload[0]
                value = None
                if chunk_id == 1:
                    self.debug_machine_control_msg[channel] = {}  # Clear msg
                    self.debug_machine_control_msg[channel]["channel"] = "master" if channel == 0 else "slave"
                    value = self.bytes_to_float(chunk_payload[1:])
                    self.debug_machine_control_msg[channel]["voltage"] = value
                elif chunk_id == 2:
                    value = self.bytes_to_float(chunk_payload[1:])
                    self.debug_machine_control_msg[channel]["temperature_avg"] = value
                elif chunk_id == 3:
                    value = self.bytes_to_float(chunk_payload[1:])
                    self.debug_machine_control_msg[channel]["temperature_old"] = value
                elif chunk_id == 4:
                    value = self.bytes_to_u32(chunk_payload[1:])
                    self.debug_machine_control_msg[channel]["timestamp_ms"] = value

            else:
                await p.print("Unknow command: 0x{:02X}: {}".format( # Added await
                    command, data_bytes))
                return
            
            if self.executing is not None:
                if command == self.executing["command"]:
                    if self.executing["preserve"] == True:
                        if self.executing.get("retval") is None:
                            self.executing["retval"] = {}
                        for key, value in parsed_data.items():
                            if key not in self.executing["retval"]:
                                self.executing["retval"][key] = value
                            elif isinstance(self.executing["retval"][key], dict) and isinstance(value, dict):
                                self.executing["retval"][key].update(value)
                            else:
                                self.executing["retval"][key] = value
            if chunk_id == max_chunks:
                if self.executing is not None:
                    if command == self.executing["command"]:
                        self.executing["status"] = CommandStatus.RECIEVED
                        await self.logger.log( # Added await
                            VerbosityLevel["DEBUG"], self.default_log_dict({
                                "debug": "END 0x{:02X}".format(command)}))
                        try:
                            if "callback" in self.executing and callable(self.executing["callback"]):
                                # If callback can be async, create a task for it
                                # Assuming callback_is_configured is now async
                                uasyncio.create_task(self.executing["callback"](self.executing))
                        except Exception as e_cb:
                            await self.logger.log( # Added await
                                VerbosityLevel["ERROR"],
                                self.default_log_dict({
                                    "info": self.trim_dict_for_logger(self.executing),
                                    "error": "callback error: {}".format(e_cb)}))
                        toLog = None

                        if self.executing.get("preserve") == True:
                            toLog = self.default_log_dict({
                                "request_timestamp_ms": self.executing.get("timestamp_ms"),
                                "command": command,
                                "retval": self.trim_dict_for_logger(self.executing.get("retval")),
                            })
                            await self.logger.log( # Added await
                                VerbosityLevel["MEASUREMENT"], toLog)
                        self.executing = None
                    else:
                        pass
                if self.save_periodic_data is True:
                    # if "timestamp_ms" in self.periodic_data:
                    try:
                        toLog = self.default_log_dict({
                            "command": AFECommand.getSensorDataSi_periodic,
                            "retval": self.trim_dict_for_logger(self.periodic_data),
                        })
                        channel_timestamp = self.periodic_data.get("timestamp_ms",None)
                        last_data = self.periodic_data.get("last_data",None)
                        average_data = self.periodic_data.get("average_data",None)
                        for ch in self.channels:
                            if last_data:
                                if ch.name in last_data:
                                    ch.last_recieved_data["last"] = {"value":last_data[ch.name], "timestamp_ms": channel_timestamp}
                            if average_data:
                                if ch.name in average_data:
                                    ch.last_recieved_data["average"] = {"value":average_data[ch.name], "timestamp_ms": channel_timestamp}
                        await self.logger.log( # Added await
                            VerbosityLevel["MEASUREMENT"], toLog)
                    except Exception as e:
                        await p.print("ERROR during save_periodic_data:", e, toLog) # Added await
                    finally:
                        self.periodic_data = {}
                if command == AFECommand.debug_machine_control:
                    for subdev in [0, 1]:
                        if self.debug_machine_control_msg[subdev].get("timestamp_ms"):
                            try:
                                toLog = self.default_log_dict({
                                    "command": AFECommand.debug_machine_control,
                                    "retval": self.trim_dict_for_logger(self.debug_machine_control_msg[subdev]),
                                })
                                await self.logger.log(VerbosityLevel["INFO"], toLog) # Added await
                            except Exception as e:
                                await p.print( # Added await
                                    "ERROR during debug_machine_control_msg:", e, toLog)
                            finally:
                                self.debug_machine_control_msg[subdev] = {}

            received_data = None

    async def start_periodic_measurement_download(self, interval_ms=2500): # Changed to async def
        await self.enqueue_command( # Added await
            AFECommand.setSensorDataSi_all_periodic_average,
            list(struct.pack('<I', interval_ms)))
        self.periodic_measurement_download_is_enabled = True

    async def stop_periodic_measurement_download(self): # Changed to async def
        await p.print("STOP") # Added await
        await self.enqueue_command( # Changed to await, and send_command to enqueue_command
            AFECommand.setSensorDataSi_all_periodic_average,
            list(struct.pack('<I', 0)))
        self.periodic_measurement_download_is_enabled = False

    async def set_offset(self, offset_master=200, offset_slave=200): # Changed to async def
        r = await self.enqueue_command(AFECommand.setOffset, [1, offset_master]) # Added await
        if r is not None:
            return r
        return await self.enqueue_command(AFECommand.setOffset, [2, offset_slave]) # Added await

    async def manage_state(self):
        if self.use_afe_can_watchdog:
            if is_timeout(self.afe_can_watchdog_timestamp_ms,int(round(self.afe_can_watchdog_timeout_ms/10.0))):
                self.afe_can_watchdog_timestamp_ms = millis()
                commandKwargs = {"timeout_ms": 10220,
                                 "preserve": True,
                                 "timeout_start_on_send_ms": 2000,
                                 "error_callback": None,
                                 "callback": None}
                await self.enqueue_command( # Added await
                    AFECommand.getTimestamp, None, **commandKwargs) # This is already async due to enqueue_command

        if not self.is_configured:
            if self.is_configuration_started is True:
                timestamp_ms = millis()
                if is_timeout(self.configuration_start_timestamp_ms,self.configuration_timeout_ms):
                    await self.logger.log(VerbosityLevel["ERROR"],  # Added await
                                    self.default_log_dict({"error": "configuration timeout", "timestamp_ms": millis()}))
                    await self.restart_device() # Added await

        if self.executing is not None:
            if is_timeout(self.executing["timestamp_ms"],self.executing["timeout_ms"]):
                self.executing["status"] = CommandStatus.ERROR
                await self.logger.log(VerbosityLevel["ERROR"], # Added await
                                self.default_log_dict(
                                {
                                    "error": "TIMEOUT",
                                    "executing": self.trim_dict_for_logger(self.executing)
                                }))
                if "callback_error" in self.executing:
                    try:
                        if self.executing["callback_error"] is not None and callable(self.executing["callback_error"]):
                            await p.print("Creating task for callback_error in manage_state: {}".format( # Added await
                                self.executing["callback_error"]))
                            # If callback_error can be async, create a task for it
                            uasyncio.create_task(self.executing["callback_error"](
                                {"afe": self, "afe_id": self.device_id, "executing": self.executing}))
                    except Exception as e:
                        await p.print("AFE manage_state error invoking callback_error: {}".format(e)) # Added await
                self.executing = None

        # Try send commands
        if self.use_tx_delay:
            if is_delay(self.execute_timestamp, self.tx_timeout_ms):
                pass
            else:
                await self.execute(0) # Changed to await
        else:
            await self.execute(0) # Changed to await
