import json
import time
import struct
import random
import pyb
import machine

from my_utilities import AFECommand, AFECommandGPIO, AFECommandChannel, AFECommandSubdevice
from my_utilities import millis, SensorChannel, SensorReading, AFE_Config, EmptyLogger

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
    def __init__(self, can_interface, device_id, logger=EmptyLogger(), config_path=None):
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
        self.current_command = 0x00
        self.last_command_time = millis()
        self.last_command_status = 0 # 0: None, 1: idle, 2: recieved, 3: error
        self.default_command_timeout = 1000
        self.command_timeout = self.default_command_timeout
        self.verbose = 2
        self.commands = AFECommand()
        self.blink_status = 0
        
        self.periodic_measurement_download_is_enabled = False
        self.blink_is_enabled = False
        self.temperatureLoop_master_is_enabled = False
        self.temperatureLoop_slave_is_enabled = False
        
        self.to_execute = []
        self.keep_output = False
        self.output = {}
        
        self.last_msg = {}
        
        self.debug_machine_control_msg = [{},{}]
        
        self.AFEGPIO_EN_HV0 = AFECommandGPIO("PORTB",10)
        self.AFEGPIO_EN_HV1 = AFECommandGPIO("PORTB",11)
        self.AFEGPIO_blink = AFECommandGPIO("PORTA",9)
        
        self.afe_config = None
        for c in AFE_Config:
            if c["afe_id"] == self.device_id:
                self.afe_config = c
                
        print("Found config for AFE {}".format(self.afe_config["afe_id"]))
        
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
        if self.keep_output is True:
            self.output = self.update_output(self.output,value_name,value,channel)
        self.last_msg = self.update_output(self.last_msg,value_name,value,channel)
        
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
    
    # Convert byte list to 32-bit integer
    def bytes_to_u32(self, data):
        return sum(data[i] << (8 * i) for i in range(len(data)))
    
    # Convert byte list to float
    def bytes_to_float(self, data):
        return struct.unpack('<f', bytes(data))[0]
    
    # Prepare frame payload for the AFE
    def prepare_command(self, command, data=None, chunk=1, max_chunks=1, timeout_ms = None, startKeepOutput=False, outputRestart=False):
        if data is None:
            data = []
        elif isinstance(data, int):
            data = [data]
        elif not (isinstance(data, list) and all(isinstance(i, int) for i in data)):
            raise ValueError("Data must be an integer or a list of integers (bytes).")

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
            "timeout_ms": self.default_command_timeout if timeout_ms is None else timeout_ms, # override timeout
            "timestamp_ms": millis(),  # insert timestamp or None if start timeout when sending
            "startKeepOutput": startKeepOutput,
            "outputRestart": outputRestart,
            
        }
    
    def enqueue_command(self, command, data=None, chunk=1, max_chunks=1, timeout_ms = None, startKeepOutput=False, outputRestart=False):
        self.to_execute.append(
            self.prepare_command(command, data, chunk, max_chunks, timeout_ms, startKeepOutput, outputRestart)
        )

    def enqueue_gpio_set(self,gpio,state):
        self.enqueue_command(self.commands.writeGPIO,
                             [gpio.port,gpio.pin,state])

    def enqueue_float_for_channel(self, command, channel, value):
        self.enqueue_command(
            command, [channel] + list(struct.pack('<f', value)))

    def enqueue_u32_for_channel(self, command, channel, value):
        self.enqueue_command(
            command, [channel] + list(struct.pack('<I', value)))

    # Execute commands from the buffer
    def execute(self,command,timeout_ms=5000,**kwargs):
        for command in self.to_execute:
            self.last_command_time = millis() if command["timestamp_ms"] is None else command["timestamp_ms"]
            self.current_command = command["command"]
            self.command_timeout = command["timeout_ms"]
            if command["startKeepOutput"] is True:
                self.keep_output = True
            if command["outputRestart"]:
                self.output = {}
            self.can_interface.send(command["frame"], command["can_address"])
            return
    
    # Receive and process data from AFE
    def process_received_data(self, received_data):
        command = None
        chunk_id = None
        max_chunks = None
        chunk_payload = []
        try:
            data_bytes = list(bytes(received_data[3]))
            device_id = (received_data[0] >> 2) & 0xFF
            msg_from_slave = (received_data[0] >> 10) & 0x001
            # print("{} {}".format(msg_from_slave,received_data))
            # pyb.delay(1000)
            if msg_from_slave != 1:
                print("Not from slave")
                return
            if device_id != self.device_id:
                return

            command = data_bytes[0]
            chunk_id = data_bytes[1] & 0x0F
            max_chunks = (data_bytes[1] >> 4) & 0x0F
            chunk_payload = data_bytes[2:]
            if(self.verbose >= 4):
                print("R: ID:{}; Command: 0x{:02X}: {}".format(device_id, command, data_bytes))
            # print(command, chunk_id,max_chunks, chunk_payload)
            
            # New message arrived
            if chunk_id == 1:
                self.last_msg = {
                    "command":command,
                    "msg_timestamp":millis()
                    }
            
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
                    self.update_last_msg("UID",self.unique_id_str)
                    self.logger.log("INFO", {"AFE": self.device_id, "UID": self.unique_id_str})
            
            elif command == self.commands.getVersion:
                self.firmware_version = int("".join(map(str, chunk_payload)))
                self.version_checked = True
                self.update_last_msg("version",self.firmware_version)
                self.ifKeepOutput("version",self.firmware_version)
   
            elif command == self.commands.setAveragingMode:
                self.channels[chunk_payload[0]].averaging_mode = chunk_payload[1]

            elif command == self.commands.setAveragingAlpha:
                self.channels[chunk_payload[0]].alpha = self.bytes_to_float(chunk_payload[1:])
            
            elif command == self.commands.setChannel_dt_ms:
                self.channels[chunk_payload[0]].time_interval_ms = self.bytes_to_u32(chunk_payload[1:])
            
            elif command == self.commands.setChannel_a:
                self.channels[chunk_payload[0]].a = self.bytes_to_float(chunk_payload[1:])
                print(self.channels[chunk_payload[0]].a)
            
            elif command == self.commands.setChannel_b:
                self.channels[chunk_payload[0]].b = self.bytes_to_float(chunk_payload[1:])
            
            elif command == self.commands.setChannel_multiplicator:
                self.channels[chunk_payload[0]].multiplicator = self.bytes_to_float(chunk_payload[1:])
            
            elif command == self.commands.setSensorDataSi_all_periodic_average:
                flag = chunk_payload[0]
                for i,b in enumerate([(flag >> x) & 0x01 for x in range(8)]):
                    self.channels[i].periodic_sending_is_enabled = True if b else False
            
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
                    
            
            elif command == self.commands.getSensorDataSiAndTimestamp_average:
                channel = chunk_payload[0]
                if chunk_id == 1:
                    value = self.bytes_to_float(chunk_payload[1:]) # value
                    self.update_last_msg("average_value_si",value,channel)
                elif chunk_id == 2:
                    value = self.bytes_to_u32(chunk_payload[1:]) # timestamp
                    self.update_last_msg("average_timestamp_ms",value,channel)

            
            elif command == self.commands.writeGPIO:
                self.blink_is_enabled = True
            
            elif command == self.commands.setTemperatureLoopForChannelState_bySubdevice:
                # print("Subdevice")
                channel = chunk_payload[0]
                status = chunk_payload[1]
                channel_name = "?"
                status_status = True if status == 1 else False
                if channel == 1:
                    self.temperatureLoop_master_is_enabled = status_status
                    channel_name = "master"
                elif channel == 2:
                    self.temperatureLoop_slave_is_enabled = status_status
                    channel_name = "slave"
                elif channel == 3:
                    self.temperatureLoop_master_is_enabled = status_status
                    self.temperatureLoop_slave_is_enabled = status_status
                    channel_name = "master+slave"
                print("TemperatureLoop for {} is {}".format(
                    channel_name, "enabled" if status == 1 else "disabled"
                ))
            
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
            
            
            if command == self.current_command and chunk_id == max_chunks:
                if(self.verbose >= 3):
                    print("0x{:02X} END".format(command))
                self.current_command = None
                
                # Resolve external commands
                if self.is_busy:
                    self.external_command_done = True
            received_data = None
        except Exception as error:
            print("Error processing received AFE data: {}".format(error))
    
    def start_periodic_measurement_download(self,interval_ms=2500):
        self.send_command(
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
        if self.current_command is not None:
            # print("0x{:02X} : {} > {} ? {}".format(
            #     self.current_command, 
            #     millis() - self.last_command_time,self.command_timeout, 
            #     (millis() - self.last_command_time) > self.command_timeout))
            if (millis() - self.last_command_time) > self.command_timeout:
                if(self.verbose >= 1):
                    print("0x{:02X} TIMEOUT".format(self.current_command))
                self.current_command = None
            return
        
        # Try send commands
        if self.use_tx_delay:
            if (millis() - self.last_command_time) < self.tx_timeout_ms:
                return

        if not self.is_configured:
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
