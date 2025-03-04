import json
import time
import machine
import pyb
import struct
import random

from AFE import AFEDevice, AFECommand, millis, SensorChannel, SensorReading
# from machine import machine

class HUBDevice:
    def __init__(self, can_bus):
        self.can_bus = can_bus
        self.afe_devices = []
        self.afe_devices_max = 1
        
        self.rx_buffer = bytearray(8)  # Pre-allocate memory
        self.rx_message = [0, 0, 0, memoryview(self.rx_buffer)]  # Use memoryview to reduce heap allocations
        self.can_bus.rxcallback(0, self.handle_can_rx)
        
        self.message_queue = []
        
        self.discovery_active = False
        # self.discovery_timer = machine.Timer()
        # self.rx_processing_timer = machine.Timer()
        self.rx_process_active = False
        self.current_discovery_id = 7
        
        self.tx_timeout_ms = 1000
        self.last_tx_time = 0
        
        self.use_tx_delay = True
        
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
                # print("R:",self.rx_message)
                self.message_queue.append(self.rx_message)
        except Exception as e:
            print("handle_can_rx: HUB RX Error: {e}".format(e=e))
            
    def find_afe_by_id(self, afe_id):
        """ Find an AFE by its short ID. """
        for afe in self.afe_devices:
            if (afe.device_id == afe_id):
                return afe
        return None
        # return next((afe for afe in self.afe_devices if afe.device_id == afe_id), None)
    
    def process_received_messages(self, timer=None):
        """ Process messages from the queue periodically. """
        for afe in self.afe_devices:
            if afe.is_online:
                afe.manage_state()
        while self.message_queue:
            message = self.message_queue.pop(0)
            afe_id = (message[0] >> 2) & 0xFF
            
            afe = self.find_afe_by_id(afe_id)
            if afe is None:
                afe = AFEDevice(self.can_bus, afe_id, 0)
                self.afe_devices.append(afe)
            # print("uuuu",pyb.millis(), len(self.message_queue))
            afe.process_received_data(message)
        # print("end", pyb.millis())
    
    def discover_devices(self, timer=None):
        """ Periodically discover AFEs on the CAN bus. """
        if len(self.afe_devices) == self.afe_devices_max:
            try:
                return 0
            finally:
                self.stop_discovery()
        
        if self.current_discovery_id > 8:
            self.current_discovery_id = 7
            return
        
        if self.use_tx_delay:
            if (pyb.millis() - self.last_tx_time) < self.tx_timeout_ms:
                return
        # print("x")
        try:
            if any(afe.is_online and afe.device_id == self.current_discovery_id for afe in self.afe_devices):
                self.current_discovery_id += 1
                return
            # print("X",self.current_discovery_id)
            self.can_bus.send(b"\x00\x11", self.current_discovery_id << 2)
            self.last_tx_time = pyb.millis()
            self.current_discovery_id += 1
        except Exception as e:
            print("discover_devices: HUB Error sending: {e}".format(e=e))
    
    def start_discovery(self, interval=0.1):
        """ Start the device discovery process. """
        raise "Not implemented"
        self.discovery_active = True
        self.discovery_timer.init(period=int(interval * 1000), mode=machine.Timer.PERIODIC, callback=self.discover_devices)
        self.rx_processing_timer.init(period=int(interval * 1000), mode=machine.Timer.PERIODIC, callback=self.process_received_messages)

    def stop_discovery(self):
        """ Stop the device discovery process. """
        self.discovery_active = False
        # self.discovery_timer.deinit()
        # self.rx_processing_timer.deinit()
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
        
    # def start_data_logging(self, interval=5):
    #     """ Start logging data at a specified interval in seconds. """
    #     self.data_logging_active = True
    #     self.data_logging_timer = machine.Timer()
    #     self.data_logging_timer.init(period=interval * 1000, mode=machine.Timer.PERIODIC, callback=self.data_logger)

    # def stop_data_logging(self):
    #     """ Stop logging data. """
    #     self.data_logging_active = False
    #     self.data_logging_timer.deinit()

    def main_process(self,timer=None):
        if self.discovery_active:
            self.discover_devices()
        if self.rx_process_active:
            self.process_received_messages()
        # print(millis(),"@", "HUB: main_process")
def initialize_can_hub():
    """ Initialize the CAN bus and HUB. """
    can_bus = pyb.CAN(1)
    can_bus.init(pyb.CAN.NORMAL, extframe=False, prescaler=54, sjw=1, bs1=7, bs2=2, auto_restart=True)
    # can_bus.setfilter(0, can_bus.MASK32, 0, (0, 0))
    can_bus.setfilter(0, can_bus.MASK16, 0, (0, 0, 0, 0))
    
    print("CAN Bus Initialized")
    hub = HUBDevice(can_bus)
    return can_bus, hub
