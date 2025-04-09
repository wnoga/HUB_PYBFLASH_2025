import json
import time
import struct
import random
import pyb
import machine

from my_utilities import AFECommand, AFECommandGPIO, AFECommandChannel, AFECommandSubdevice
from my_utilities import millis, SensorChannel, SensorReading, AFE_Config, EmptyLogger
from my_utilities import e_ADC_CHANNEL, CommandStatus, ResetReason

# class AFESendCommandQueue:
#     def __init__(self,can_interface,device_id,verbose=0):
#         self.can_interface = can_interface
#         self.device_id = device_id
#         self.to_send = []
#         self.verbose = verbose
#         self.is_sending = False
#         self.tx_delay_ms = 0
#         self.tx_send_old = 
        
#     def clear(self):
#         self.to_send = []
    
#     def send_command(self, command, data=None, chunk=1, max_chunks=1,id_priority=0,timeout_ms=5000):
#         if data is None:
#             data = []
#         elif isinstance(data, int):
#             data = [data]
#         elif not (isinstance(data, list) and all(isinstance(i, int) for i in data)):
#             raise ValueError("Data must be an integer or a list of integers (bytes).")

#         chunk_info = (max_chunks << 4) | chunk
#         frame = bytearray([command, chunk_info] + data[:6])
#         if(self.verbose >= 4):
#             print("Transmitting {} bytes: 0x{:02X}: {}/{} : {} = {}".format(
#                 len(frame), command, chunk, max_chunks, data, frame
#             ))
#         self.to_send.append({
#             "frame":frame,
#             "id":(self.device_id << 2) | (id_priority & 0x003),
#             "timestamp_ms":millis(),
#             "timeout_ms":timeout_ms})

#     def manage(self):
#         for c in self.to_send:
#             if (c["timestamp_ms"] - millis()) < c["timeout_ms"]:
#                 c.remove_first()
#                 continue
#             if self.is_sending:
#                 continue
#             self.is_sending = True
#             self.can_interface.send(c["frame"], c["id"])

# Device (AFE) Class
class AFEDevice:
    def __init__(self, can_interface: pyb.CAN, device_id, logger=EmptyLogger(), config_path=None):
        self.can_interface = can_interface
        self.device_id = device_id  # Channel number
        self.unique_id = [0,0,0] # 3*32-bit = 96-bit STM32 Unique ID
        self.unique_id_str = ""
        self.logger = logger
        self.config_path = config_path # Path to the config file
        
        # Use this if communication is faster than the AFE
        self.use_tx_delay = True
        self.tx_timeout_ms = 10
        
        self.can_address = device_id << 2
        self.configuration = {}
        self.is_online = False
        self.total_channels = 8
        self.firmware_version = None
        self.version_checked = False
        self.channels = [SensorChannel(x) for x in range(self.total_channels)]
        self.is_configured = False
        # self.current_command = 0x00
        # self.last_command_time = millis()
        # self.last_command_status = 0 # 0: None, 1: idle, 2: recieved, 3: error
        self.default_command_timeout_ms = 1000
        # self.command_timeout = self.default_command_timeout
        self.default_can_timeout_ms = 1000
        self.verbose = 2
        self.commands = AFECommand()
        self.blink_status = 0
        
        self.periodic_measurement_download_is_enabled = False
        self.blink_is_enabled = False
        self.temperatureLoop_master_is_enabled = False
        self.temperatureLoop_slave_is_enabled = False
        
        self.to_execute = []
        self.execute_timestamp = 0
        # self.keep_output = False
        # self.output = {}
        self.executing = None
        self.executed = []
        
        # self.last_msg = {}
        
        self.debug_machine_control_msg = [{},{}]
        
        self.AFEGPIO_EN_HV0 = AFECommandGPIO(port="PORTB",pin=10)
        self.AFEGPIO_EN_HV1 = AFECommandGPIO(port="PORTB",pin=11)
        self.AFEGPIO_blink = AFECommandGPIO(port="PORTA",pin=9)
        
        self.afe_config = None
        for c in AFE_Config:
            if c["afe_id"] == self.device_id:
                self.afe_config = c
                
        self.logger.log("DEBUG","Found config for AFE {}".format(self.afe_config["afe_id"]))
    
    def update_output(self, output, value_name, value, channel = None):
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
                output.setdefault("data", []).append({"channel": channel, value_name: value})
        return output

    def update_last_msg(self,value_name, value, channel = None):
        return
        # if self.keep_output is True:
        #     self.output = self.update_output(self.output,value_name,value,channel)
        # self.last_msg = self.update_output(self.last_msg,value_name,value,channel)
        
    def restart_device(self):
        self.current_command = None
        self.last_command_time = 0

    # Print AFE information
    def display_info(self):
        print("AFE Device: {}; ID: {}".format(self.device_id, self.unique_id))
        for channel in self.channels:
            try:
                print("Channel {}, Averaging: {}, alpha: {}, Interval: {} ms".format(
                    channel.channel_id, channel.averaging_mode, channel.alpha, channel.time_interval_ms
                ))
            except:
                pass

    # Send configuration settings to AFE
    def transmit_settings(self, settings):
        self.configuration = settings
        message = json.dumps({"id": self.device_id, "settings": settings})
        try:
            self.can_interface.send(message.encode(), self.device_id)
        except Exception as error:
            print("Error transmitting settings to AFE {}: {}".format(self.device_id, error))
    
    # Convert byte list to 32-bit unsigned integer
    def bytes_to_u32(self, data):
        return struct.unpack('<I', bytes(data))[0]
    
    # Convert byte list to float
    def bytes_to_float(self, data):
        return struct.unpack('<f', bytes(data))[0]
    
    def unmask_channel(self, masked_channel):
        """
        Unmasks a channel mask to determine which channels are active.

        This function takes a masked channel value and returns a list of
        channel numbers that are active based on the mask. If the masked
        channel is 0, it returns a special value indicating a timestamp.

        Args:
            masked_channel (int): The masked channel value.

        Returns:
            list: A list of active channel numbers or a special value for timestamp.
        """
        if masked_channel == 0: # often as timestamp
            return [1<<8]
        else:
            channels = []
            for i in range(8):
                if 0x01 & (masked_channel >> i):
                    channels.append(i)
            return channels
        
    def getChannelName(self,number: int) -> str:
        return e_ADC_CHANNEL.get(number, "Unknown")
                    
    
    # Prepare frame payload for the AFE
    def prepare_command(self, command, data=None, chunk=1, max_chunks=1, timeout_ms = None, preserve=False, startKeepOutput=False, outputRestart=False, can_timeout_ms=None, callback=None):
        """
        Prepares a command to be sent to the AFE device.

        This function constructs a command frame with the given parameters,
        including command code, chunk information, and data payload. It also
        sets up metadata for command execution, such as timeouts, timestamps,
        and status.

        Args:
            command (int): The command code to be sent to the AFE.
            data (list or int, optional): The data payload to be included in the
                command. Can be an integer or a list of integers. Defaults to None.
            chunk (int, optional): The current chunk number for multi-chunk
                commands. Defaults to 1.
            max_chunks (int, optional): The total number of chunks for multi-chunk
                commands. Defaults to 1.
            timeout_ms (int, optional): The timeout in milliseconds for the
                command execution. Defaults to None.
            preserve (bool, optional): Whether to preserve the command's return
                value. Defaults to False.
            startKeepOutput (bool, optional): Whether to start keeping output.
                Defaults to False.
            outputRestart (bool, optional): Whether to restart output. Defaults to False.
            can_timeout_ms (int, optional): The timeout for CAN communication. Defaults to None.
        """
        if data is None:
            data = []
        elif isinstance(data, int):
            data = [data]
        elif not (isinstance(data, list) and all(isinstance(i, int) for i in data)):
            raise ValueError("Data must be an integer or a list of integers (bytes). {data}".format())

        chunk_info = (max_chunks << 4) | chunk
        frame = bytearray([command, chunk_info] + data[:6])
        if(self.verbose >= 4):
            print("Transmitting {} bytes: 0x{:02X}: {}/{} : {} = {}".format(
                len(frame), command, chunk, max_chunks, data, frame
            ))
        return {
            "command": command, # command
            "frame": frame,  # payload
            "can_address": self.can_address,  # can address
            "timeout_ms": self.default_command_timeout_ms if timeout_ms is None else timeout_ms, # override timeout
            "timestamp_ms": millis(),  # insert timestamp or None if start timeout when sending
            "startKeepOutput": startKeepOutput,
            "outputRestart": outputRestart,
            "can_timeout_ms": self.default_can_timeout_ms if can_timeout_ms is None else can_timeout_ms,
            "status": CommandStatus.NONE,
            "preserve":preserve,
            "retval": None,
            "callback": callback
        }
    
    def enqueue_command(self, command, data=None, chunk=1, max_chunks=1, timeout_ms = None, preserve = False, startKeepOutput=False, outputRestart=False, can_timeout_ms=None, callback=None):
        """
        Enqueues a command to be executed by the AFE device.

        This function adds a command to the execution queue, preparing it with
        the specified parameters. It uses the `prepare_command` method to
        construct the command frame and metadata.

        Args:
            command (int): The command code to be sent to the AFE.
            data (list or int, optional): The data payload for the command.
                Defaults to None.
            chunk (int, optional): The current chunk number for multi-chunk
                commands. Defaults to 1.
            max_chunks (int, optional): The total number of chunks for multi-chunk
                commands. Defaults to 1.
            timeout_ms (int, optional): The timeout in milliseconds for the
                command execution. Defaults to None.
            preserve (bool, optional): Whether to preserve the command's return
                value. Defaults to False.
            can_timeout_ms (int, optional): The timeout for CAN communication. Defaults to None.
        """
        self.to_execute.append(
            self.prepare_command(command, data, chunk, max_chunks, timeout_ms, preserve, startKeepOutput, outputRestart, can_timeout_ms, callback)
        )

    def enqueue_gpio_set(self,gpio,state):
        self.enqueue_command(self.commands.writeGPIO,
                             [gpio.port,gpio.pin,state])

    def enqueue_float_for_channel(self, command, channel, value,**kwargs):
        """
        Enqueues a command with a float value for a specific channel.

        This function prepares a command that includes a float value, which is
        packed into bytes and associated with a specific channel.

        Args:
            command (int): The command code to be sent to the AFE.
            channel (int): The channel number to which the command applies.
            value (float): The float value to be sent with the command.
        """
        self.enqueue_command(
            command, [channel] + list(struct.pack('<f', value)),**kwargs)
    
    def enqueue_u16_for_channel(self, command, channel, value,**kwargs):
        """
        Enqueues a command with a 16-bit unsigned integer value for a specific channel.

        This function prepares a command that includes a 16-bit unsigned integer
        value, which is packed into bytes and associated with a specific channel.

        Args:
            command (int): The command code to be sent to the AFE.
            channel (int): The channel number to which the command applies.
            value (int): The 16-bit unsigned integer value to be sent with the command.
        """
        self.enqueue_command(
            command, [channel] + list(struct.pack('<H', value)),**kwargs)
    
    def enqueue_u32_for_channel(self, command, channel, value):
        """
        Enqueues a command with a 32-bit unsigned integer value for a specific channel.

        Args:
            command (int): The command code to be sent to the AFE.
            channel (int): The channel number to which the command applies.
            value (int): The 32-bit unsigned integer value to be sent with the command.
        """
        self.enqueue_command(
            command, [channel] + list(struct.pack('<I', value)))

    def start_periodic_command_download(self, channel_mask: int, data_type: int, interval_ms: int = 2500):
        """
        Starts periodic data download for specified channels.

        This function configures the AFE device to periodically send data for
        the specified channels. It sets the sending period and data type
        (average or last) for each channel.

        Args:
            channel_mask (int): A bitmask indicating which channels to enable
                periodic data download for. Each bit corresponds to a channel.
            data_type (int): An integer indicating the type of data to send.
                This could represent whether to send average or last data.
            interval_ms (int, optional): The interval in milliseconds between
                data transmissions. Defaults to 2500.
        """
        # Iterate over the unmasked channels and enable periodic sending
        for channel in self.unmask_channel(channel_mask):
            self.channels[channel].periodic_sending_is_enabled = True

        # Set the data type for the specified channels
        self.enqueue_u32_for_channel(AFECommand.setChannel_send_period_type_byMask, channel_mask, data_type)
        # Set the sending period for the specified channels
        self.enqueue_u32_for_channel(AFECommand.setChannel_send_period_ms_byMask, channel_mask, interval_ms)

    """
    Executes commands from the execution queue.

    This function checks if there are commands in the `to_execute` queue and if
    no command is currently being executed. If both conditions are met, it
    retrieves the next command from the queue, marks it as `IDLE`, sends it
    over the CAN interface, and logs the action.

    Args:
        timeout_ms (int, optional): The timeout in milliseconds for the
            command execution. Defaults to 5000.
        **kwargs: Additional keyword arguments.

    """
    def execute(self,timeout_ms=5000,**kwargs):
        if len(self.to_execute) and self.executing is None:
            self.execute_timestamp = millis()
            cmd = self.to_execute.pop(0)
            self.executing = cmd
            self.executing["status"] = CommandStatus.IDLE
            self.can_interface.send(cmd["frame"], cmd["can_address"],timeout=cmd["can_timeout_ms"])
            self.logger.log("DEBUG","Sending {}".format(cmd))
    
    def process_received_data(self, received_data):
        """
        Processes received data from the AFE device.

        This function handles incoming data packets from the AFE, parses them
        based on the command and chunk information, and updates the device's
        state accordingly. It supports various commands, including reading
        sensor data, managing GPIO, setting DAC values, and handling
        temperature loop configurations.

        Args:
            received_data (list): A list containing the received data packet,
                typically including the CAN ID, RTR, FMI, and the data payload.

        Raises:
            Exception: If there is an error processing the received data, such
                as incorrect data format or unexpected command codes.

        Notes:
            This function is designed to be robust against various types of
            errors, logging warnings and errors as needed.
        """
        command = None
        chunk_id = None
        max_chunks = None
        chunk_payload = []
        parsed_data = {}
        try:
            data_bytes = list(bytes(received_data[3]))
            device_id = (received_data[0] >> 2) & 0xFF
            msg_from_slave = (received_data[0] >> 10) & 0x001
            if msg_from_slave != 1:
                self.logger.log("WARNING","Not from slave")
                return
            if device_id != self.device_id:
                return

            command = data_bytes[0]
            chunk_id = data_bytes[1] & 0x0F
            max_chunks = (data_bytes[1] >> 4) & 0x0F
            chunk_payload = data_bytes[2:]
            self.logger.log("DEBUG","R: ID:{}; Command: 0x{:02X}: {}".format(device_id, command, data_bytes))
            
            # New message arrived
            # if chunk_id == 1:
            #     # self.last_msg = {
            #     #     "command":command,
            #     #     "msg_timestamp":millis()
            #     #     }
            
            if command == self.commands.getSerialNumber:
                # print(command, self.commands.getSerialNumber)
                chunk_data = self.bytes_to_u32(chunk_payload)
                if chunk_id == 0:
                    self.unique_id = []
                    self.output = {}
                self.unique_id[chunk_id-1] = chunk_data
                if chunk_id == max_chunks:
                    self.is_online = True
                    self.current_command = None
                    uid0 = 0x001E0028
                    uid1 = 0x46415716
                    uid2 = 0x20353634
                    self.unique_id_str = "".join("{:08X}".format(b) for b in self.unique_id)
                    # self.update_last_msg("UID",self.unique_id_str)
                    self.logger.log("INFO", {"AFE": self.device_id, "UID": self.unique_id_str})
                    parsed_data.update({"unique_id_str":self.unique_id_str})
            
            elif command == self.commands.getVersion:
                self.firmware_version = int("".join(map(str, chunk_payload)))
                self.version_checked = True
                self.update_last_msg("version",self.firmware_version)
                parsed_data.update({"version":self.firmware_version})
                
            elif command == self.commands.resetAll:
                self.logger.log("ERROR","AFE {} was restared! Reason {}".format(device_id, ResetReason[chunk_payload[0]]))
                self.logger.sync()
                
            elif command == self.commands.getSensorDataSi_last_byMask:
                unmasked_channels = self.unmask_channel(chunk_payload[0])
                if not "last_data" in parsed_data:
                    parsed_data["last_data"] = {}
                for uch in unmasked_channels:
                    if uch == (1<<8):
                        parsed_data["last_data"].update({"timestamp_ms".format(uch):self.bytes_to_u32(chunk_payload[1:])})
                    else:
                        parsed_data["last_data"].update({"ch{}".format(uch):self.bytes_to_float(chunk_payload[1:])})

            elif command == self.commands.getSensorDataSi_average_byMask:
                unmasked_channels = self.unmask_channel(chunk_payload[0])
                if not "average_data" in parsed_data:
                    parsed_data["average_data"] = {}
                for uch in unmasked_channels:
                    if uch == (1<<8):
                        parsed_data["average_data"].update({"timestamp_ms".format(uch):self.bytes_to_u32(chunk_payload[1:])})
                    else:
                        parsed_data["average_data"].update({"ch{}".format(uch):self.bytes_to_float(chunk_payload[1:])})
            elif command == self.commands.setAD8402Value_byte_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    if 0x01 & (chunk_payload[2] >> uch):
                        self.logger.log("ERROR", "AFE {}: ERROR setAD8402Value_byte_byMask for CH{}".format(
                            device_id, uch
                        ))
            
            elif command == self.commands.setAveragingMode_byMask:
                unmasked_channels = self.unmask_channel(chunk_payload[0])
                for uch in unmasked_channels:
                    self.channels[uch].averaging_mode = chunk_payload[1]

            elif command == self.commands.setAveragingAlpha_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].alpha = self.bytes_to_float(chunk_payload[1:])
            
            elif command == self.commands.setChannel_dt_ms_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].time_interval_ms = self.bytes_to_u32(chunk_payload[1:])
            
            elif command == self.commands.setChannel_a_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].a = self.bytes_to_float(chunk_payload[1:])
            
            elif command == self.commands.setChannel_b_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].b = self.bytes_to_float(chunk_payload[1:])
            
            elif command == self.commands.setChannel_multiplicator_byMask:
                for uch in self.unmask_channel(chunk_payload[0]):
                    self.channels[uch].multiplicator = self.bytes_to_float(chunk_payload[1:])
            
            # elif command == self.commands.setSensorDataSi_all_periodic_average:
            #     flag = chunk_payload[0]
            #     for i,b in enumerate([(flag >> x) & 0x01 for x in range(8)]):
            #         self.channels[i].periodic_sending_is_enabled = True if b else False
            
            elif command == self.commands.getSensorDataSi_all_periodic_average:
                channel = chunk_payload[0]
                # print("xxxx {} {} {}".format(chunk_id, channel,value))
                if chunk_id == 1:
                    value = self.bytes_to_float(chunk_payload[1:])
                    self.channels[channel].latest_reading.timestamp_ms = 0
                    self.channels[channel].latest_reading.value = value
                    self.update_last_msg("periodic_average_value_si",value,channel)
                elif chunk_id == 2:
                    value = self.bytes_to_u32(chunk_payload[1:])
                    self.channels[channel].latest_reading.timestamp_ms = value
                    self.update_last_msg("periodic_average_timestamp_ms",value,channel)
                    print("{}: {}".format(channel, self.channels[channel].latest_reading))
                    
            
            elif command == self.commands.getSensorDataSiAndTimestamp_average_byMask:
                channel = chunk_payload[0]
                if chunk_id == 1:
                    value = self.bytes_to_float(chunk_payload[1:]) # value
                    self.update_last_msg("average_value_si",value,channel)
                elif chunk_id == 2:
                    value = self.bytes_to_u32(chunk_payload[1:]) # timestamp
                    self.update_last_msg("average_timestamp_ms",value,channel)

            
            elif command == self.commands.writeGPIO:
                # self.blink_is_enabled = True
                print("GPIO {}".format(chunk_payload))
            
            # elif command == self.commands.setTemperatureLoopForChannelState_bySubdevice:
            #     # print("Subdevice")
            #     channel = chunk_payload[0]
            #     status = chunk_payload[1]
            #     channel_name = "?"
            #     status_status = True if status == 1 else False
            #     if channel == 1:
            #         self.temperatureLoop_master_is_enabled = status_status
            #         channel_name = "master"
            #     elif channel == 2:
            #         self.temperatureLoop_slave_is_enabled = status_status
            #         channel_name = "slave"
            #     elif channel == 3:
            #         self.temperatureLoop_master_is_enabled = status_status
            #         self.temperatureLoop_slave_is_enabled = status_status
            #         channel_name = "master+slave"
            #     print("TemperatureLoop for {} is {}".format(
            #         channel_name, "enabled" if status == 1 else "disabled"
            #     ))
            elif command == 0xD4:
                pass
            
            elif command == AFECommand.setDACValueRaw_bySubdeviceMask:
                channel = chunk_payload[0]

            elif command == AFECommand.setDAC_bySubdeviceMask_asMask:
                print(chunk_payload)
                # unmasked_channels = self.unmask_channel(chunk_payload[0])
                # for uch in unmasked_channels:
                #     print("DAC{} is {}".format(uch, "ON" if chunk_payload[1] & (1 << uch) else "OFF"))


            elif command == self.commands.debug_machine_control:
                channel = chunk_payload[0]
                if chunk_id == 1:
                    self.debug_machine_control_msg[channel] = {} # Clear msg
                    self.debug_machine_control_msg[channel]["channel"] = "master" if channel == 0 else "slave"
                    value = self.bytes_to_float(chunk_payload[1:])
                    self.debug_machine_control_msg[channel]["voltage"] = value
                if chunk_id == 2:
                    value = self.bytes_to_float(chunk_payload[1:])
                    self.debug_machine_control_msg[channel]["temperature_avg"] = value
                if chunk_id == 3:
                    value = self.bytes_to_float(chunk_payload[1:])
                    self.debug_machine_control_msg[channel]["temperature_old"] = value
                if chunk_id == 4:
                    value = self.bytes_to_u32(chunk_payload[1:])
                    self.debug_machine_control_msg[channel]["timestamp_ms"] = value
                    print(self.debug_machine_control_msg[channel])
            
            else:
                print("Unknow command: 0x{:02X}: {}".format(command,data_bytes))
                return

            # if command == self.commands.e_can_function_getSensorDataSi and len(chunk_payload) == 5:
            #     channel = self.channels[chunk_payload[0]]
            #     channel.latest_reading.timestamp_ms = millis()
            #     channel.latest_reading.value = self.bytes_to_float(chunk_payload[1:])
            #     if(self.verbose >= 2):
            #         print("{} @ Channel: {} = {:0.4f}".format(
            #             channel.latest_reading.timestamp_ms, chunk_payload[0], channel.latest_reading.value
            #         ))
            
            if self.executing is not None:
                if command == self.executing["command"]:
                    if self.executing["preserve"] == True:
                        if self.executing["retval"] is None:
                            self.executing["retval"] = {}
                        for key, value in parsed_data.items():
                            if key not in self.executing["retval"]:
                                self.executing["retval"][key] = value
                            elif isinstance(self.executing["retval"][key], dict) and isinstance(value, dict):
                                self.executing["retval"][key].update(value)
                            else:
                                self.executing["retval"][key] = value
                            
                                

            if chunk_id == max_chunks:
                if(self.verbose >= 3):
                    print("0x{:02X} END".format(command))
                if self.executing is not None:
                    if command == self.executing["command"]:
                        self.executing["status"] = CommandStatus.RECIEVED
                        self.logger.log("DEBUG","END: {}".format(self.executing))
                        if "callback" in self.executing and callable(self.executing["callback"]):
                            self.executing["callback"](self.executing)
                        if self.executing["preserve"] == True:
                            self.executed.append(self.executing.copy())
                            # self.logger.log("DEBUG",self.executing)
                            print(self.executing)
                        self.executing = None
            received_data = None
        except Exception as error:
            print("Error processing received AFE data: {}: {}".format(error,command))
    
    def start_periodic_measurement_download(self,interval_ms=2500):
        self.enqueue_command(
            self.commands.setSensorDataSi_all_periodic_average,
            list(struct.pack('<I', interval_ms)))
        self.periodic_measurement_download_is_enabled = True
        
    def stop_periodic_measurement_download(self):
        print("STOP")
        self.send_command(
            self.commands.setSensorDataSi_all_periodic_average,
            list(struct.pack('<I', 0)))
        self.periodic_measurement_download_is_enabled = False
        
    def start_periodic_measurement_download_for_channel(self, channel, interval_ms=2500):
        self.send_command(
            self.commands.setSens
        )
        
    def set_offset(self,offset_master=200,offset_slave=200):
        self.enqueue_command(self.commands.setOffset,[1,offset_master])
        self.enqueue_command(self.commands.setOffset,[2,offset_slave])
    
    # AFE state management
    def manage_state(self):
        if self.executing is not None:
            # print(self.executing)
            if (millis() - self.executing["timestamp_ms"]) > self.executing["timeout_ms"]:
                self.logger.log("DEBUG","AFE:manage_state:0x{:02X} TIMEOUT".format(self.executing["command"]))
                self.executing["status"] = CommandStatus.ERROR
                if self.executing["preserve"] == True:
                    self.executed.append(self.executing.copy())
                self.executing = None
            return
        
        # Try send commands
        if self.use_tx_delay:
            if (millis() - self.execute_timestamp) < self.tx_timeout_ms:
                return

        if not self.is_configured:
            pass
        else:
            if self.version_checked != True:
                self.send_command(0x01)
                return
            # # return
            # # pyb.delay(10000)
            for channel in self.channels:
                try:
                    if channel.averaging_mode == None:
                        self.send_command(self.commands.setAveragingMode, [channel.channel_id, 1])
                        return
                    if channel.alpha is None:
                        self.send_command(self.commands.setAveragingAlpha, [channel.channel_id] + list(struct.pack('<f', random.randint(1000, 2000))))
                        return
                    # if channel.time_interval_ms is None:
                    #     self.send_command(self.commands.setAveragingDt_ms, [channel.channel_id] + list(struct.pack('<I', random.randint(1000, 2000))))
                    #     return
                    # if channel.multiplicator is None:
                    #     self.send_command(self.commands.setChannel_multiplicator, [channel.channel_id] + list(struct.pack('<f', 1.0)))
                    #     return
                    if channel.time_interval_ms is None:
                        value = self.afe_config["channel"][channel.channel_id].time_interval_ms
                        self.send_command(self.commands.setChannel_a,[channel.channel_id] + list(struct.pack('<f', value)))
                    if channel.a is None:
                        value = self.afe_config["channel"][channel.channel_id].a
                        self.send_command(self.commands.setChannel_a,[channel.channel_id] + list(struct.pack('<f', value)))
                    if channel.b is None:
                        value = self.afe_config["channel"][channel.channel_id].b
                        self.send_command(self.commands.setChannel_a,[channel.channel_id] + list(struct.pack('<f', value)))
                except Exception as error:
                    print("set channel averaging_mode: {}".format(error))
            if self.blink_is_enabled is False:
                gpio = AFECommandGPIO()
                self.send_command(
                    self.commands.writeGPIO,
                    [gpio.blink_Port,gpio.blink_Pin,1])
                return

            if self.temperatureLoop_master_is_enabled is False:
                subdevice = AFECommandSubdevice()
                self.send_command(
                    self.commands.setTemperatureLoopForChannelState_bySubdevice,
                    [subdevice.AFECommandSubdevice_master, 1])
                return
                
            if self.temperatureLoop_slave_is_enabled is False:
                subdevice = AFECommandSubdevice()
                self.send_command(
                    self.commands.setTemperatureLoopForChannelState_bySubdevice,
                    [subdevice.AFECommandSubdevice_slave, 1])
                return
            

            # if self.periodic_measurement_download_is_enabled is False:
            #     self.start_periodic_measurement_download(10000)
            #     return
            
            # Stop initialization process
            self.is_configured = True
            if self.verbose >= 1:
                self.display_info()
            return

        self.execute()
        
        return
        # Here you can you can add something to execute post-init commands
        gpioCommand = AFECommandGPIO()
        self.send_command(self.commands.writeGPIO,
                          [gpioCommand.blink_Port,gpioCommand.blink_Pin,self.blink_status])
        if self.blink_status is 0:
            self.blink_status = 1
        else:
            self.blink_status = 0
        # print("blink")
        # pyb.delay(10)
        # for channel in self.channels:
        #     if (millis() - channel.latest_reading.timestamp_ms) >= (channel.time_interval_ms * 4):
        #         self.send_command(self.commands.get_sensor_data, [channel.channel_id])
        #         return
