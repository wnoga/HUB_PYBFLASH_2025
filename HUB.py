import json
import time
import machine
import pyb
import struct
import random
import _thread

from AFE import AFEDevice, AFECommand, millis, SensorChannel, SensorReading
from my_utilities import JSONLogger, EmptyLogger, AFECommandChannel, AFECommandSubdevice, AFECommandGPIO, AFECommandAverage, AFE_Config, read_callibration_csv
from my_utilities import channel_name_xxx, e_ADC_CHANNEL
from my_utilities import wdt
from my_utilities import p
# from my_utilities import lock

# logger = EmptyLogger()
logger = JSONLogger(use_csv=False)
# logger.log("INFO",{"test":"test"})

# afe = AFEDevice() # only for autocomplete

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
    def __init__(self, can_bus, logger = EmptyLogger(), use_rxcallback=True, use_automatic_restart=False):
        self.can_bus = can_bus
        self.afe_devices: list[AFEDevice] = []
        self.afe_devices_max = 8
        self.use_automatic_restart = use_automatic_restart
        
        self.t = None
        self.run = True
        
        self.logger = logger
        
        self.rx_buffer = bytearray(8)  # Pre-allocate memory
        self.rx_message = [0, 0, 0, memoryview(self.rx_buffer)]  # Use memoryview to reduce heap allocations
        self.use_rxcallback = use_rxcallback
        if self.use_rxcallback:
            self.can_bus.rxcallback(0, self.handle_can_rx) # Trigger every new CAN message
        
        self.message_queue = []
        
        self.discovery_active = False # enable discovery subprocess
        self.afe_manage_active = False # enable management of the AFEs
        self.rx_process_active = False
        
        self.afe_id_min = 35
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
    
    def powerOn(self):
        pyb.Pin.cpu.E12.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
        pyb.Pin.cpu.E12.value(1)
        p.print("HV is on")

    def powerOff(self):
        pyb.Pin.cpu.E12.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
        pyb.Pin.cpu.E12.value(0)
        p.print("HV is off")
        
    def reset_all(self):
        self.stop_discovery()
        self.afe_devices = []
        self.message_queue = []
        self.current_discovery_id = 1
        
    def clear_all_logs(self):
        try:
            import os
            for filename in os.listdir("/sd/logs"):
                os.remove("/sd/logs/" + filename)
        except Exception as e:
            p.print("Error clearing logs: {}".format(e))
        
    def handle_can_rx(self, bus, reason):
        """ Callback function to handle received CAN messages. """
        try:
            while self.can_bus.any(0):  # Check FIFO 0 for messages
                self.can_bus.recv(0, self.rx_message)  # Read message from FIFO 0
                if len(self.message_queue) >= 255:
                    p.print("Poped message: {}".format(self.message_queue[0]))
                    self.message_queue.pop(0)
                self.message_queue.append(self.rx_message)
        except Exception as e:
            p.print("handle_can_rx: HUB RX Error: {e}".format(e=e))
    
    def handle_can_rx_polling(self):
        if self.can_bus.any(0):
            self.handle_can_rx(self.can_bus,0)
            
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
    
    def process_received_messages(self, timer=None):
        """
        Process messages received from the CAN bus.

        This method retrieves messages from the message queue, identifies the
        AFE device associated with each message, and processes the received data.
        If a message is from a new AFE, it creates a new AFEDevice instance.
        """
        message = None
        # Check if message queue is empty
        if not self.message_queue:
            return  # Exit early if there are no messages to process

        # Check if message processing is active
        if not self.rx_process_active:
            return  # Exit early if message processing is not active
        
        if len(self.message_queue): # Process only if anythong is in the queue
            message = self.message_queue.pop(0) # get from FIFO
        try:
            if message is None:
                return
            afe_id = (message[0] >> 2) & 0xFF # unmask the AFE ID
            afe = self.get_afe_by_id(afe_id)
            if afe is None: # Add new discovered AFE
                # Create a new AFE device instance with the discovered ID
                afe = AFEDevice(self.can_bus, afe_id, logger=self.logger)
                # Add the new AFE device to the list of known devices
                self.afe_devices.append(afe)
            # Process the received data using the AFE device's method
            afe.process_received_data(message)
        except Exception as e:
            p.print("process_received_messages: {}".format(e))
    
    def discover_devices(self, timer=None):
        """ Periodically discover AFEs on the CAN bus. """
        
        # Check if there are any AFE devices
        if not self.discovery_active:
            return
        
        # Check if discovery is active
        if not self.discovery_active:
            return  # Exit early if discovery is not active
        
        # Check if all devices are discovered
        if len(self.afe_devices) == self.afe_devices_max:
            self.stop_discovery()
            return
        if self.use_tx_delay:
            if (millis() - self.last_tx_time) < self.tx_delay_ms:
                return
        if self.can_bus.state() > 1:
            # self.logger.log("ERROR","CAN BUS ERROR {}".format(self.can_bus.state()))
            if self.can_bus.state() > 2:
                self.can_bus.restart()
            return
        try:
            # Check if AFE with current ID is already discovered or if we've exceeded the maximum ID
            if self.current_discovery_id > self.afe_id_max:
                self.current_discovery_id = self.afe_id_min  # Reset to the minimum ID
            
            if not any(afe.is_online and afe.device_id == self.current_discovery_id for afe in self.afe_devices):
                # Send get ID msg to discover new AFE
                self.can_bus.send(b"\x00\x11", self.current_discovery_id << 2, timeout=self.tx_timeout_ms)
                self.last_tx_time = millis()
                self.logger.log("DEBUG", "Sending discovery message to ID: {}".format(self.current_discovery_id))
            else:
                self.logger.log("DEBUG", "AFE with ID {} already discovered".format(self.current_discovery_id))

            self.current_discovery_id += 1  # Increment ID for the next iteration

        except Exception as e:
            self.logger.log("ERROR", "discover_devices: HUB Error sending: {}".format(e))
            
    
    def start_discovery(self):
        """ Start the device discovery process. """
        self.discovery_active = True

    def stop_discovery(self):
        """ Stop the device discovery process. """
        self.discovery_active = False
        p.print("STOP DISCOVERY")
        
    def start_periodic_measurement_download(self, interval_ms=2500):
        A = list(self.afe_devices)  # Directly use the list of devices
        timestamp_old = millis()
        
        while A:  # Loop while A is not empty
            for afe in A[:]:  # Iterate over a copy of A to allow safe removal
                if (millis() - timestamp_old) > 5000:
                    A.remove(afe)
                elif afe.is_online and afe.current_command is None:
                    if not afe.enabled_periodic_measurement_download:
                        afe.start_periodic_measurement_download(interval_ms)
                    else:
                        A.remove(afe)
             
    def get_afe_by_id(self,afe_id) -> AFEDevice:
        if len(self.afe_devices) == 0:
            return None
        for afe in self.afe_devices:
            if afe.device_id == afe_id:
                return afe
        return None
    
    def set_offset_for_afe(self, afe_id,offset_master=200,offset_slave=200):
        afe = self.get_afe_by_id(afe_id)
        # afe.set_offset(offset_master,offset_slave)
        subdevice = AFECommandSubdevice()
        afe.enqueue_u32_for_channel(
            AFECommand.setAD8402Value_byte, subdevice.AFECommandSubdevice_master, offset_master)
        afe.enqueue_u32_for_channel(
            AFECommand.setAD8402Value_byte, subdevice.AFECommandSubdevice_slave, offset_slave)

    def set_hv_on(self,afe_id):
        afe = self.get_afe_by_id(afe_id)
        afe.enqueue_gpio_set(afe.AFEGPIO_EN_HV0,1)
        afe.enqueue_gpio_set(afe.AFEGPIO_EN_HV1,1)
        
    def set_hv_off(self,afe_id):
        afe = self.get_afe_by_id(afe_id)
        afe.enqueue_gpio_set(afe.AFEGPIO_EN_HV0,0)
        afe.enqueue_gpio_set(afe.AFEGPIO_EN_HV1,0)
        
    def test_start_measurement_record_in_ram(self,afe_id):
        # afe.enqueue_gpio_set(afe.AFEGPIO_blink,0)
        afe.enqueue_command(AFECommand.setAveragingMode,
                            [6,3],outputRestart=True,startKeepOutput=True)
    
    def start_all(self):
        self.set_offset_for_afe(1,200,210)
        self.set_gv_on()
    
    def abort_execution(self):
        self.curent_function = None
        self.curent_function_afe_id = None
        self.curent_function_timestamp_ms = millis()
        self.curent_function_retval = None
    
    def execute_for_id(self,afe_id,function,**kwargs):
        self.curent_function = function
        self.curent_function_afe_id = afe_id
        self.curent_function_timestamp_ms = millis()
        self.curent_function_retval = None
        return
                
    def stop_periodic_measurement_download(self):
        A = list(self.afe_devices)  # Directly use the list of devices
        timestamp_old = millis()
        
        while A:  # Loop while A is not empty
            for afe in A[:]:  # Iterate over a copy of A to allow safe removal
                if (millis() - timestamp_old) > 5000:
                    A.remove(afe)
                elif afe.is_online and afe.current_command is None:
                    if afe.enabled_periodic_measurement_download:
                        afe.stop_periodic_measurement_download()
                    else:
                        A.remove(afe)

    def get_configuration_from_files(self, afe_id, callibration_data_file_csv = "dane_kalibracyjne.csv", TempLoop_file_csv = "TempLoop.csv",UID=None):
        """
        Retrieves calibration data for a specific AFE from CSV files.

        This function reads calibration data from two CSV files: one for general
        calibration data and another for temperature loop data. It then filters
        this data to find entries that match the specified AFE ID and optional UID.
        The function also checks for missing or empty calibration values and
        provides warnings if such issues are found.

        Args:
            afe_id (int): The ID of the AFE for which to retrieve calibration data.
            callibration_data_file_csv (str, optional): The path to the CSV file
                containing general calibration data. Defaults to "dane_kalibracyjne.csv".
            TempLoop_file_csv (str, optional): The path to the CSV file containing
                temperature loop data. Defaults to "TempLoop.csv".
            UID (str, optional): The unique identifier of the AFE. If provided,
                only data matching this UID will be considered. Defaults to None.
        Returns:
            dict: A dictionary containing the calibration data for the specified AFE.
        """
        TempLoop_data, TempLoop_data_mean = read_callibration_csv(TempLoop_file_csv)
        callib_data, callib_data_mean = read_callibration_csv(callibration_data_file_csv)
        
        callibration = {'ID':afe_id}
        for c0 in [callib_data,TempLoop_data]:
            for c in c0:
                if c['ID'] != afe_id:
                    continue
                if UID is not None:
                    if c['SN_AFE'] != UID:
                        continue
                g = c['M/S']
                if g not in callibration:
                    callibration[g] = {}
                callibration[g].update(c)
        
        for c0 in [callib_data_mean,TempLoop_data_mean]:
            for g in ['M','S']:
                for k,v in c0[g].items():
                    if k not in callibration[g]: # no key
                        self.logger.log("WARNING", "Calibration data: AFE {}: No key: {}".format(afe_id, k))
                        callibration[g][k] = ''
                    elif len(str(callibration[g][k])) == 0: # empty string:
                        self.logger.log("WARNING", "Calibration data: AFE {}: No value {}, set to {}".format(afe_id,k,v))
                        callibration[g][k] = v # set default value
        return callibration
    
    def default_get_measurement(self, afe_id=35,callback=None):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms":10220,"preserve":True,"timeout_start_on": 5000}
        if callback is not None:
            commandKwargs["callback"] = callback
        # afe.enqueue_command(AFECommand.getSensorDataSi_average_byMask,[AFECommandChannel.AFECommandChannel_7], timeout_ms=2500)
        # afe.enqueue_command(AFECommand.getSensorDataSi_average_byMask,[AFECommandChannel.AFECommandChannel_7 | AFECommandChannel.AFECommandChannel_6], timeout_ms=2500)
        afe.enqueue_command(AFECommand.getSensorDataSi_last_byMask,[0xFF], **commandKwargs)
        afe.enqueue_command(AFECommand.getSensorDataSi_average_byMask,[0xFF], **commandKwargs)
    
    def default_callback_return(self,msg=None):
        return msg
    
    def default_get_measurement_last(self, afe_id=35,callback=None):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms":10220,"preserve":True,"timeout_start_on": 5000}
        if callback is not None:
            commandKwargs["callback"] = callback
        afe.enqueue_command(AFECommand.getSensorDataSi_last_byMask,[0xFF], **commandKwargs)
    
     
    def callback_1(self,msg=None):
        msg["callback"] = None
        msg = json.dumps(msg)
        p.print("callback:",msg)
        
    def default_start_measurement(self, afe_id=35,
                                  enable_temperature_loop=True,
                                  enable_offset_for_sipm_from_file=False,
                                  refresh_rate_ms=5000,
                                  **args):
        pass
    
    def default_hv_set(self, afe_id=35, enable=False):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        for g in ["M", "S"]:
            afe.enqueue_gpio_set(afe.AFEGPIO_EN_HV0 if g ==
                                'M' else afe.AFEGPIO_EN_HV1, 1 if enable else 0)
            
    def default_cal_in_set(self, afe_id=35, enable=False):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        for g in ["M", "S"]:
            afe.enqueue_gpio_set(afe.AFEGPIO_EN_CAL_IN0 if g ==
                                'M' else afe.AFEGPIO_EN_CAL_IN1, 1 if enable else 0)
                                
        
    def default_set_dac(self, afe_id=35, dac_master=2481, dac_slave=2481):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms":10220,"preserve":False,"timeout_start_on_send_ms":2000}
        def get_subdevice_ch_id(g):
            return AFECommandSubdevice.AFECommandSubdevice_master if g == 'M' else AFECommandSubdevice.AFECommandSubdevice_slave
        p.print("Set DAC for {}".format(afe_id))
        if True:
            for g in ["M", "S"]:
                afe.enqueue_u16_for_channel(AFECommand.setDACValueRaw_bySubdeviceMask, get_subdevice_ch_id(g), dac_master if g == 'M' else dac_slave,**commandKwargs)
                afe.enqueue_command(AFECommand.setDAC_bySubdeviceMask, [get_subdevice_ch_id(g),1],**commandKwargs)
                afe.enqueue_gpio_set(afe.AFEGPIO_EN_HV0 if g ==
                                    'M' else afe.AFEGPIO_EN_HV1, 1,**commandKwargs)
                # afe.enqueue_gpio_set(afe.AFEGPIO_EN_CAL_IN0 if g ==
                #                     'M' else afe.AFEGPIO_EN_CAL_IN1, 1,**commandKwargs)
                afe.enqueue_gpio_set(afe.AFEGPIO_blink,1,**commandKwargs)
                afe.enqueue_gpio_set(afe.AFEGPIO_blink,0,**commandKwargs)
        else:
            afe.enqueue_command(AFECommand.setDAC_bySubdeviceMask_asMask, [3,3])
            afe.enqueue_command(AFECommand.setDACValueRaw_bySubdeviceMask, dac_master)
            afe.enqueue_gpio_set(afe.AFEGPIO_EN_HV0, 0)
            afe.enqueue_gpio_set(afe.AFEGPIO_EN_HV1, 0)
            afe.enqueue_gpio_set(afe.AFEGPIO_blink,1)
            afe.enqueue_gpio_set(afe.AFEGPIO_blink,0)
            
    def default_start_temperature_loop(self, afe_id=35):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms":10220,"preserve":True,"timeout_start_on_send_ms":2000}
        def get_subdevice_ch_id(g):
            return AFECommandSubdevice.AFECommandSubdevice_master if g == 'M' else AFECommandSubdevice.AFECommandSubdevice_slave
        p.print("Set DAC for {}".format(afe_id))
        if True:
            for g in ["M", "S"]:
                afe.enqueue_command(AFECommand.setTemperatureLoopForChannelState_byMask_asStatus, [get_subdevice_ch_id(g),1],**commandKwargs)
        else:
            pass
        
    def default_manual_blocking_measurement_loop(self, afe_id=35):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        timestamp_ms = millis()
        while (millis() - timestamp_ms) <= 10000:
            self.default_get_measurement(afe_id)
            time.sleep(5.0)
       
    def default_periodic_measurement_download_all(self, afe_id=35, ms=10000):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms":10220,"preserve":True,"timeout_start_on_send_ms":2000}
        afe.enqueue_u32_for_channel(AFECommand.setChannel_period_ms_byMask,0xFF,ms,**commandKwargs) 
        
    def default_full(self, afe_id=35):
        self.powerOn()
        self.default_procedure(afe_id)
        self.default_set_dac(afe_id)
        self.default_start_temperature_loop(afe_id)
        self.default_get_measurement(afe_id)
        # self.default_periodic_measurement_download_all(afe_id)

    def reset(self, afe_id=35):
        for afe in self.afe_devices:
            if afe.device_id == afe_id:
                afe.enqueue_command(0x03)
                
    def test1(self, afe_id=35, command=0xF8):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        # afe.enqueue_float_for_channel(0xF8,0,21.37)
        afe.enqueue_command(command)
        
    def test2(self, afe_id=35, command=0xF9):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        # afe.enqueue_float_for_channel(0xF8,0,21.37)
        afe.enqueue_command(command,preserve=True)
        
    def test3(self, afe_id=35, command=0xF7,mask=0xFF):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        # afe.enqueue_float_for_channel(0xF8,0,21.37)
        afe.enqueue_command(command,[mask],preserve=True)
    
    def test4(self, afe_id=35):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        self.reset(afe_id)
        pyb.delay(500)
        self.default_procedure(afe_id)
        self.default_set_dac(afe_id)
        for i in range(10):
            p.print("get measurement")
            self.default_get_measurement(afe_id)
            pyb.delay(500)
    
    def d(self,cmd,data=None):
        afe = self.get_afe_by_id(35)
        if afe is None:
            return
        afe.enqueue_command(cmd,data,preserve=True)        

    def default_procedure(self, afe_id=35):
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
        
        def get_subdevice_ch_id(g):
            return AFECommandSubdevice.AFECommandSubdevice_master if g=='M' else AFECommandSubdevice.AFECommandSubdevice_slave
        def get_T_measured_ch_id(g):
            return AFECommandChannel.AFECommandChannel_7 if g=='M' else AFECommandChannel.AFECommandChannel_6
        def get_U_measured_ch_id(g):
            return AFECommandChannel.AFECommandChannel_2 if g=='M' else AFECommandChannel.AFECommandChannel_3
        def get_I_measured_ch_id(g):
            return AFECommandChannel.AFECommandChannel_4 if g=='M' else AFECommandChannel.AFECommandChannel_5
        
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        callib = self.get_configuration_from_files(afe_id)
        p.print("### DEFAULT PROCEDURE ###")
        p.print(str(callib).replace("'",'"'))
        p.print("#########################")
        afe.callback_1 = self.callback_1
        # timeoutForCommand_ms = 10000
        commandKwargs = {"timeout_ms":10220,"preserve":True,"timeout_start_on_send_ms":1000}
        # afe.enqueue_float_for_channel(AFECommand.setAveragingAlpha_byMask,AFECommandChannel.AFECommandChannel_7,1.0/100.0,timeout_ms=4135)
        # afe.enqueue_command(AFECommand.setAveragingMode_byMask,[channels.AFECommandChannel_6,averages.STANDARD],timeout_ms=5000)
        # afe.enqueue_command(AFECommand.getVersion,callback=self.callback_1)
        # afe.enqueue_command(AFECommand.getVersion,preserve=True)
        # afe.enqueue_command(AFECommand.setAveragingMode_byMask,[0x01,1],**commandKwargs)
        # return
        if True:
            for g in ["M","S"]: 
                subdevice = AFECommandSubdevice.AFECommandSubdevice_master if g=='M' else AFECommandSubdevice.AFECommandSubdevice_slave
                # ch_id = channels.AFECommandChannel_6 if g=='M' else channels.AFECommandChannel_7 # select proper channel id
                ch_id = None
                for k,v in callib[g].items():
                    ch_id = 0x00
                    ks = k.split(" ")[0]
                    if ks == "T_measured_a":
                        ch_id = get_T_measured_ch_id(g)
                        afe.enqueue_float_for_channel(AFECommand.setChannel_a_byMask,ch_id,v,**commandKwargs)
                    elif ks == "T_measured_b":
                        ch_id = get_T_measured_ch_id(g)
                        afe.enqueue_float_for_channel(AFECommand.setChannel_b_byMask,ch_id,v,**commandKwargs)
                    elif ks == "offset":
                        ch_id = get_subdevice_ch_id(g)
                        afe.enqueue_u16_for_channel(AFECommand.setAD8402Value_byte_byMask,ch_id,int(v),**commandKwargs)
                    elif ks == "U_measured_a":
                        ch_id = get_U_measured_ch_id(g)
                        afe.enqueue_float_for_channel(AFECommand.setChannel_a_byMask,ch_id,v,**commandKwargs)
                    elif ks == "U_measured_b":
                        ch_id = get_U_measured_ch_id(g)
                        afe.enqueue_float_for_channel(AFECommand.setChannel_b_byMask,ch_id,v,**commandKwargs)
                    elif ks == "I_measured_a":
                        ch_id = get_I_measured_ch_id(g)
                        afe.enqueue_float_for_channel(AFECommand.setChannel_a_byMask,ch_id,v,**commandKwargs)
                    elif ks == "I_measured_b":
                        ch_id = get_I_measured_ch_id(g)
                        afe.enqueue_float_for_channel(AFECommand.setChannel_b_byMask,ch_id,v,**commandKwargs)
                    elif ks == "U_set_a":
                        ch_id = get_subdevice_ch_id(g)
                        afe.enqueue_float_for_channel(AFECommand.setRegulator_a_dac_byMask,ch_id,v,**commandKwargs)
                    elif ks == "U_set_b":
                        ch_id = get_subdevice_ch_id(g)
                        afe.enqueue_float_for_channel(AFECommand.setRegulator_b_dac_byMask,ch_id,v,**commandKwargs)
                    elif ks == "V_opt":
                        ch_id = get_subdevice_ch_id(g)
                        afe.enqueue_float_for_channel(AFECommand.setRegulator_V_opt_byMask,ch_id,v,**commandKwargs)
                    elif ks == "dV/dT":
                        ch_id = get_subdevice_ch_id(g)
                        afe.enqueue_float_for_channel(AFECommand.setRegulator_dV_dT_byMask,ch_id,v,**commandKwargs)
                    else:
                        continue
                    for uch in afe.unmask_channel(ch_id):
                        p.print("AFE {} {} Loading {} (CH{} ? {}) value {}".format(afe_id,g,k,uch,e_ADC_CHANNEL[uch],v))
            
        else:
            afe.enqueue_u16_for_channel(AFECommand.setAD8402Value_byte_byMask,AFECommandSubdevice.AFECommandSubdevice_master | AFECommandSubdevice.AFECommandSubdevice_slave,int(200))
            afe.enqueue_float_for_channel(AFECommand.setChannel_a_byMask,0xFF,1.0)
            afe.enqueue_float_for_channel(AFECommand.setChannel_b_byMask,0xFF,0.0)
        afe.enqueue_u32_for_channel(AFECommand.setChannel_dt_ms_byMask,0xFF,1000,**commandKwargs)
        afe.enqueue_u32_for_channel(AFECommand.setAveraging_max_dt_ms_byMask,0xFF,100*200,**commandKwargs)
        afe.enqueue_command(AFECommand.setAveragingMode_byMask,[0xFF,AFECommandAverage.WEIGHTED_EXPONENTIAL],**commandKwargs)
        afe.enqueue_float_for_channel(AFECommand.setChannel_multiplicator_byMask,0xFF,1.0,**commandKwargs)
        afe.enqueue_float_for_channel(AFECommand.setAveragingAlpha_byMask,0xFF,1.0/100.0,**commandKwargs)
        
        afe.enqueue_command(AFECommand.startADC,[0xFF,0xFF],**{"timeout_ms":10000,"preserve":True})

        # p.print("Start periodic measurement report for AFE {}".format(afe_id))
        # afe.enqueue_command(AFECommand.getSensorDataSi_last_byMask,[0xFF],timeout_ms=2500)
        return
        # afe.enqueue_command(AFECommand.getVersion)  # get Version
        afeConfig = None
        for a in AFE_Config:
            if a["afe_id"] == afe_id:
                afeConfig = a
                break
        # p.print(afeConfig["afe_id"])
        for ch in afeConfig["channel"]:
            afe.enqueue_command(AFECommand.setAveragingMode,[ch.channel_id,ch.averaging_mode])
            afe.enqueue_float_for_channel(AFECommand.setChannel_a,ch.channel_id,ch.a)
            afe.enqueue_float_for_channel(AFECommand.setChannel_b,ch.channel_id,ch.b)
            afe.enqueue_u32_for_channel(AFECommand.setChannel_dt_ms,ch.channel_id,ch.time_interval_ms)
            afe.enqueue_float_for_channel(AFECommand.setAveragingAlpha,ch.channel_id,ch.alpha)

    def parse(self,msg):
        p.print("Parsed: {}".format(msg))
        
    def send_back_data(self,afe_id: int):
        """
        Sends back the last received message from a specific AFE.

        Args:
            afe_id (int): The ID of the AFE from which to send the last message.
        """
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        toSend = afe.executed.copy() # get all executed commands
        afe.executed = [] # clear executed commands
        p.print("Send back: {}".format(json.dumps(toSend)))

    def main_process(self,timer=None):
        if True:
            if not self.use_rxcallback: self.handle_can_rx_polling()
            self.discover_devices()
            if self.rx_process_active:
                self.process_received_messages()
            if self.afe_manage_active:
                for afe in self.afe_devices:
                    if afe.is_online:
                        afe.manage_state()
                    if self.use_automatic_restart:
                        # TODO Update this
                        if not afe.is_configured:
                            if not afe.is_fired:
                                self.default_full(afe_id=afe.device_id)
                                self.default_periodic_measurement_download_all(afe_id=afe.device_id)
                                afe.is_fired = True
            if self.curent_function is not None: # check if function is running
                if (millis() - self.curent_function_timestamp_ms) > self.curent_function_timeout_ms:
                    self.curent_function = None
                    self.curent_function_retval = "timeout"
        if False:
            pass
    
    def main_loop(self):
        while self.run:
            self.main_process()
            time.sleep_us(10)
        
            
def initialize_can_hub(use_rxcallback=True,**kwargs):
    """ Initialize the CAN bus and HUB. """
    can_bus = pyb.CAN(1)
    can_bus.init(pyb.CAN.NORMAL, extframe=False, prescaler=54, sjw=1, bs1=7, bs2=2, auto_restart=True)
    # can_bus.setfilter(0, can_bus.MASK32, 0, (0, 0))
    can_bus.setfilter(0, can_bus.MASK16, 0, (0, 0, 0, 0))
    
    
    # p.print(str(callib_data[0]).replace("'",'"'))
    # return
    
    p.print("CAN Bus Initialized")
    # logger.verbosity_level = "DEBUG"
    hub = HUBDevice(can_bus,logger=logger,use_rxcallback=use_rxcallback,**kwargs)
    # hub.t = _thread.start_new_thread(hub.main_loop,())
    
    
    return can_bus, hub
