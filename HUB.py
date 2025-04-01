import json
import time
import machine
import pyb
import struct
import random

from AFE import AFEDevice, AFECommand, millis, SensorChannel, SensorReading
from my_utilities import JSONLogger, EmptyLogger, AFECommandChannel, AFECommandSubdevice, AFECommandGPIO, AFECommandAverage, AFE_Config, read_callibration_csv

logger = EmptyLogger()
# logger = JSONLogger()
# logger.log("INFO",{"test":"test"})

# afe = AFEDevice() # only for autocomplete

class HUBDevice:
    def __init__(self, can_bus, logger = EmptyLogger()):
        self.can_bus = can_bus
        self.afe_devices = []
        self.afe_devices_max = 8
        
        self.logger = logger
        
        self.rx_buffer = bytearray(8)  # Pre-allocate memory
        self.rx_message = [0, 0, 0, memoryview(self.rx_buffer)]  # Use memoryview to reduce heap allocations
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
        
        self.curent_function = None
        self.curent_function_timestamp_ms = 0
        self.curent_function_timeout_ms = 2500
        self.curent_function_afe_id = None
        self.curent_function_retval = None
        
        self.afecmd = AFECommand()
        
        
    def reset_all(self):
        self.stop_discovery()
        self.afe_devices = []
        self.message_queue = []
        self.current_discovery_id = 1
        
    def handle_can_rx(self, bus, reason):
        """ Callback function to handle received CAN messages. """
        try:
            while self.can_bus.any(0):  # Check FIFO 0 for messages
                self.can_bus.recv(0, self.rx_message)  # Read message from FIFO 0
                if len(self.message_queue) >= 255:
                    self.message_queue.pop(0)
                self.message_queue.append(self.rx_message)
        except Exception as e:
            print("handle_can_rx: HUB RX Error: {e}".format(e=e))
            
    def get_afe_by_id(self, afe_id) -> AFEDevice:
        """ Find an AFE by its short ID. """
        for afe in self.afe_devices:
            if (afe.device_id == afe_id):
                return afe
        return None
    
    def process_received_messages(self, timer=None):
        """ Process messages from the queue periodically. """
        if len(self.message_queue): # Process only if anythong is in the queue
            message = self.message_queue.pop(0) # get from FIFO
            afe_id = (message[0] >> 2) & 0xFF # unmask the AFE ID
            afe = self.get_afe_by_id(afe_id)
            if afe is None: # Add new discovered AFE
                afe = AFEDevice(self.can_bus, afe_id, logger=self.logger)
                self.afe_devices.append(afe)
            afe.process_received_data(message)
    
    def discover_devices(self, timer=None):
        """ Periodically discover AFEs on the CAN bus. """
        
        # Check if all devices are discovered
        if len(self.afe_devices) == self.afe_devices_max:
            self.stop_discovery()
            return
        
        if self.use_tx_delay:
            if (millis() - self.last_tx_time) < self.tx_timeout_ms:
                return
        if self.can_bus.state() > 1:
            # self.logger.log("ERROR","CAN BUS ERROR {}".format(self.can_bus.state()))
            if self.can_bus.state() > 2:
                self.can_bus.restart()
            return
        try:
            # Check if AFE with current ID is already discovered
            while True:
                if self.current_discovery_id > self.afe_id_max:
                    self.current_discovery_id = self.afe_id_min
                if not any(afe.is_online and afe.device_id == self.current_discovery_id for afe in self.afe_devices):
                    # Send get ID msg
                    self.can_bus.send(b"\x00\x11", self.current_discovery_id << 2,timeout=self.tx_timeout_ms)
                    self.last_tx_time = millis()
                    # print("Sending",self.current_discovery_id)
                self.current_discovery_id += 1 # increment ID
                break
                
        except Exception as e:
            print("discover_devices: HUB Error sending: {e}".format(e=e))
    
    def start_discovery(self):
        """ Start the device discovery process. """
        self.discovery_active = True

    def stop_discovery(self):
        """ Stop the device discovery process. """
        self.discovery_active = False
        print("STOP DISCOVERY")
        
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
            afe.commands.setAD8402Value_byte, subdevice.AFECommandSubdevice_master, offset_master)
        afe.enqueue_u32_for_channel(
            afe.commands.setAD8402Value_byte, subdevice.AFECommandSubdevice_slave, offset_slave)

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
        afe.enqueue_command(afe.commands.setAveragingMode,
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

    def default_get_measurement(self, afe_id=35):
        channels = AFECommandChannel()
        averages = AFECommandAverage()
        subdevices = AFECommandSubdevice()
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        if True:
            # afe.enqueue_command(afe.commands.getSensorDataSi_average_byMask,[channels.AFECommandChannel_7], timeout_ms=2500)
            # afe.enqueue_command(afe.commands.getSensorDataSi_average_byMask,[channels.AFECommandChannel_7 | channels.AFECommandChannel_6], timeout_ms=2500)
            afe.enqueue_command(afe.commands.getSensorDataSi_last_byMask,[0xFF], timeout_ms=2500)
            afe.enqueue_command(afe.commands.getSensorDataSi_average_byMask,[0xFF], timeout_ms=2500)
        else:
            afe.enqueue_command(afe.commands.getSensorDataSi_last_byMask,
                            [
                                channels.AFECommandChannel_0 |
                                channels.AFECommandChannel_6 |
                                channels.AFECommandChannel_7
                            ],
                            timeout_ms=2500)

                        
    def default_procedure(self, afe_id=35):
        channels = AFECommandChannel()
        averages = AFECommandAverage()
        subdevices = AFECommandSubdevice()
        
        def get_controller_ch_id(g):
            return channels.AFECommandChannel_7 if g=='M' else channels.AFECommandChannel_6
        def get_U_measured_ch_id(g):
            return channels.AFECommandChannel_2 if g=='M' else channels.AFECommandChannel_3
        def get_I_measured_ch_id(g):
            return channels.AFECommandChannel_4 if g=='M' else channels.AFECommandChannel_5
        
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        callib = self.get_configuration_from_files(afe_id)
        print("### DEFAULT PROCEDURE ###")
        print(str(callib).replace("'",'"'))
        print("#########################")
        # afe.enqueue_float_for_channel(afe.commands.setAveragingAlpha_byMask,channels.)
        # afe.enqueue_command(afe.commands.setAveragingMode_byMask,[channels.AFECommandChannel_6,averages.STANDARD],timeout_ms=5000)
        # return
        for g in ["M","S"]: 
            subdevice = subdevices.AFECommandSubdevice_master if g=='M' else subdevices.AFECommandSubdevice_slave
            # ch_id = channels.AFECommandChannel_6 if g=='M' else channels.AFECommandChannel_7 # select proper channel id
            ch_id = None
            for k,v in callib[g].items():
                ch_id = 0x00
                ks = k.split(" ")[0]
                if ks == "T_measured_a":
                    ch_id = get_controller_ch_id(g)
                    afe.enqueue_float_for_channel(afe.commands.setChannel_a_byMask,ch_id,v)
                elif ks == "T_measured_b":
                    ch_id = get_controller_ch_id(g)
                    afe.enqueue_float_for_channel(afe.commands.setChannel_b_byMask,ch_id,v)
                # elif ks == "U_measured_a":
                #     ch_id = channels.AFECommandChannel_6 if g=='M' else channels.AFECommandChannel_7
                #     afe.enqueue_float_for_channel(afe.commands.setChannel_b,ch_id,v)
                # elif ks == "Time_sample":
                #     ch_id = 0xFF # all channels
                elif ks == "offset":
                    ch_id = get_controller_ch_id(g)
                    afe.enqueue_u32_for_channel(afe.commands.setAD8402Value_byte_byMask,ch_id,int(v))
                elif ks == "U_measured_a":
                    ch_id = get_U_measured_ch_id(g)
                    afe.enqueue_float_for_channel(afe.commands.setChannel_a_byMask,ch_id,v)
                elif ks == "U_measured_b":
                    ch_id = get_U_measured_ch_id(g)
                    afe.enqueue_float_for_channel(afe.commands.setChannel_b_byMask,ch_id,v)
                elif ks == "I_measured_a":
                    ch_id = get_I_measured_ch_id(g)
                    afe.enqueue_float_for_channel(afe.commands.setChannel_a_byMask,ch_id,v)
                elif ks == "I_measured_b":
                    ch_id = get_I_measured_ch_id(g)
                    afe.enqueue_float_for_channel(afe.commands.setChannel_b_byMask,ch_id,v)
                else:
                    continue
                for uch in afe.unmask_channel(ch_id):
                    print("Loaded for AFE {}:{}:CH{}: {} = {}".format(afe_id,g,uch,k,v))
            afe.enqueue_u32_for_channel(afe.commands.setAveraging_max_dt_ms_byMask,0xFF,100*200)
            afe.enqueue_command(afe.commands.setAveragingMode_byMask,[0xFF,averages.STANDARD])
            afe.enqueue_float_for_channel(afe.commands.setAveragingAlpha_byMask,0xFF,1.0/100.0)

        # print("Start periodic measurement report for AFE {}".format(afe_id))
        # afe.enqueue_command(afe.commands.getSensorDataSi_last_byMask,[0xFF],timeout_ms=2500)
        return
        # afe.enqueue_command(afe.commands.getVersion)  # get Version
        afeConfig = None
        for a in AFE_Config:
            if a["afe_id"] == afe_id:
                afeConfig = a
                break
        # print(afeConfig["afe_id"])
        for ch in afeConfig["channel"]:
            afe.enqueue_command(afe.commands.setAveragingMode,[ch.channel_id,ch.averaging_mode])
            afe.enqueue_float_for_channel(afe.commands.setChannel_a,ch.channel_id,ch.a)
            afe.enqueue_float_for_channel(afe.commands.setChannel_b,ch.channel_id,ch.b)
            afe.enqueue_u32_for_channel(afe.commands.setChannel_dt_ms,ch.channel_id,ch.time_interval_ms)
            afe.enqueue_float_for_channel(afe.commands.setAveragingAlpha,ch.channel_id,ch.alpha)

    def parse(self,msg):
        print("Parsed: {}".format(msg))
        
    def send_back_data(self,afe_id: int):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        toSend = afe.last_msg.copy()
        if toSend == {}:
            return
        afe.last_msg = {}
        print("Send back: {}".format(toSend))

    def main_process(self,timer=None):
        if self.discovery_active:
            self.discover_devices()
        if self.afe_manage_active:
            for afe in self.afe_devices:
                if afe.is_online:
                    afe.manage_state()
        if self.rx_process_active:
            self.process_received_messages()
        if self.curent_function is not None:
            if (millis() - self.curent_function_timestamp_ms) > self.curent_function_timeout_ms:
                self.curent_function = None
                self.curent_function_retval = "timeout"
            
def initialize_can_hub():
    """ Initialize the CAN bus and HUB. """
    can_bus = pyb.CAN(1)
    can_bus.init(pyb.CAN.NORMAL, extframe=False, prescaler=54, sjw=1, bs1=7, bs2=2, auto_restart=True)
    # can_bus.setfilter(0, can_bus.MASK32, 0, (0, 0))
    can_bus.setfilter(0, can_bus.MASK16, 0, (0, 0, 0, 0)) 
    
    # print(str(callib_data[0]).replace("'",'"'))
    # return
    
    print("CAN Bus Initialized")
    hub = HUBDevice(can_bus,logger=logger)
    
    
    return can_bus, hub
