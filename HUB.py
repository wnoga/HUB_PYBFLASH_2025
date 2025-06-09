import json
import time
import machine
import pyb
import struct
import random
import _thread
import micropython
import uasyncio

from AFE import AFEDevice, AFECommand, millis
from my_utilities import JSONLogger, AFECommandChannel, AFECommandSubdevice, AFECommandGPIO, AFECommandAverage, read_callibration_csv
from my_utilities import channel_name_xxx, e_ADC_CHANNEL
from my_utilities import wdt
from my_utilities import p
from my_utilities import VerbosityLevel
from my_utilities import AFECommandChannelMask
from my_utilities import lock, unlock
from my_utilities import extract_bracketed


class RxDeviceCAN:
    def __init__(self,can_bus: pyb.CAN, use_rxcallback=True):
        # self.lock = print_lock
        self.can_bus = can_bus
        self.use_rxcallback = use_rxcallback
        self.rx_timeout_ms = 5000
        self.rx_buffer = bytearray(8)  # Pre-allocate memory
        # Use memoryview to reduce heap allocations
        # self.rx_message = [0, 0, 0, memoryview(self.rx_buffer)]
        self.rx_message_buffer_max_len = 32
        self.rx_message_buffer_head = 0
        self.rx_message_buffer_tail = 0
        # self.rx_buffer_arr = [bytearray(8) for x in range(
        #     self.rx_message_buffer_max_len)]  # Pre-allocate memory
        # self.rx_message_buffer = [
        #     [0, 0, 0, memoryview(self.rx_buffer_arr[x])] for x in range(self.rx_message_buffer_max_len)]
        self.rx_message_buffer = [
            # [0, 0, 0, bytearray(8)] for x in range(self.rx_message_buffer_max_len)]
            [0, 0, 0, memoryview(bytearray(8))] for x in range(self.rx_message_buffer_max_len)]
        try:
            while self.handle_can_rx_polling():
                pass
        except:
            pass
        self.running = True
        self.use_rxcallback = use_rxcallback
        if self.use_rxcallback:
            # Trigger every new CAN message
            self.can_bus.rxcallback(0, self.handle_can_rx)
        
    def get(self):
        # with self.lock:
        
        if self.rx_message_buffer_head == self.rx_message_buffer_tail:
            return None
        irq_state = pyb.disable_irq() # Start of critical section
        # tmp = self.rx_message_buffer[self.rx_message_buffer_tail].copy()
        tmp = [self.rx_message_buffer[self.rx_message_buffer_tail][0],
                self.rx_message_buffer[self.rx_message_buffer_tail][1],
                self.rx_message_buffer[self.rx_message_buffer_tail][2],
                bytearray(self.rx_message_buffer[self.rx_message_buffer_tail][3])]
        self.rx_message_buffer_tail += 1
        if self.rx_message_buffer_tail >= self.rx_message_buffer_max_len:
            self.rx_message_buffer_tail = 0
        pyb.enable_irq(irq_state) # End of critical section
        return tmp

    # def inc(self):
    #     self.rx_message_buffer_head += 1
    #     if self.rx_message_buffer_head >= self.rx_message_buffer_max_len:
    #         self.rx_message_buffer_head = 0
    #     if self.rx_message_buffer_head == self.rx_message_buffer_tail:
    #         self.rx_message_buffer_tail += 1
    #         if self.rx_message_buffer_tail >= self.rx_message_buffer_max_len:
    #             self.rx_message_buffer_tail = 0

    def handle_can_rx(self, bus: pyb.CAN, reason=None):
        # with self.lock:
        # lock()
        try:
            while bus.any(0):
                bus.recv(0, self.rx_message_buffer[self.rx_message_buffer_head], timeout=self.rx_timeout_ms)
                self.rx_message_buffer_head += 1
                if self.rx_message_buffer_head >= self.rx_message_buffer_max_len:
                    self.rx_message_buffer_head = 0
                if self.rx_message_buffer_head == self.rx_message_buffer_tail: # Buffer full
                    self.rx_message_buffer_tail += 1 # Overwrite oldest message
                    if self.rx_message_buffer_tail >= self.rx_message_buffer_max_len:
                        self.rx_message_buffer_tail = 0
        except:
            pass
        # """ Callback function to handle received CAN messages. """
        # # If use_rxcallback is True, this is called from ISR context or similar,
        # # a message should be ready. timeout=0 means non-blocking read.
        # # If use_rxcallback is False, it's called from polling after can_bus.any(0),
        # # so a message is likely ready, but self.rx_timeout_ms can be used.
        # current_recv_timeout = 0 if self.use_rxcallback else self.rx_timeout_ms
        
        # received_successfully = False
        # try:
        #     # The fourth element of the list item is the memoryview/bytearray for data
        #     bus.recv(0, self.rx_message_buffer[self.rx_message_buffer_head], timeout=current_recv_timeout)
        #     received_successfully = True
        # except OSError as e:
        #     if e.args[0] == 11: # EAGAIN for non-blocking read with no data (micropython.const(MP_EAGAIN))
        #         # This might happen if timeout=0 and message was read by another context,
        #         # or if can.any() was true but message disappeared before recv in polling.
        #         # p.print(f"RxDeviceCAN.handle_can_rx: EAGAIN on recv with timeout {current_recv_timeout}")
        #         pass # Do not advance buffer head if no message was actually received
        #     else:
        #         # Log other OSErrors, but don't advance buffer head on error
        #         p.print("RxDeviceCAN.handle_can_rx: recv OSError:",e)

        # if received_successfully:
        #     self.rx_message_buffer_head += 1
        #     if self.rx_message_buffer_head >= self.rx_message_buffer_max_len:
        #         self.rx_message_buffer_head = 0
        #     if self.rx_message_buffer_head == self.rx_message_buffer_tail: # Buffer full
        #         self.rx_message_buffer_tail += 1 # Overwrite oldest message
        #         if self.rx_message_buffer_tail >= self.rx_message_buffer_max_len:
        #             self.rx_message_buffer_tail = 0
        # # unlock()
                
    def handle_can_rx_polling(self):
        try:
            if self.can_bus.any(0):
                self.handle_can_rx(self.can_bus)
                return True
        except Exception as e:
            p.print("handle_can_rx_polling: {}".format(e))
            return None
    
    def handle_can_rx_polling_schedule(self, _):
        self.handle_can_rx_polling()
                
    def main_process(self):
        self.handle_can_rx_polling_schedule(0)
        
    async def main_loop(self,reason=None):
        while self.running:
            try:
                state = self.can_bus.state()
                if state == pyb.CAN.STOPPED:
                    p.print("CAN BUS STOPPED")
                elif state > 0: # pyb.CAN.ERROR_ACTIVE: # 1
                    # CAN.ERROR_WARNING, # 2
                    # CAN.ERROR_PASSIVE, # 3
                    # CAN.BUS_OFF,       # 4
                    p.print("CAN BUS ERROR {}".format(state))
                    return
                    # print("X  ")
                # print_lock.acquire()
                self.handle_can_rx_polling()
                # time.sleep_us(1)
                # print_lock.release()
                # except Exception as e:
            except Exception as e:
                pass
            uasyncio.sleep_ms(1)
            # time.sleep_ms(1)


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

    def __init__(self, can_bus: pyb.CAN, logger:JSONLogger, rxDeviceCAN:RxDeviceCAN, use_rxcallback=True, use_automatic_restart=False):
        self.can_bus = can_bus
        self.afe_devices: list[AFEDevice] = []
        self.afe_devices_max = 8
        self.use_automatic_restart = use_automatic_restart

        self.rx_timeout_ms = 1000
        self.run = True

        self.logger = logger
        self.use_rxcallback = use_rxcallback
        self.rxDeviceCAN = rxDeviceCAN

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

    def powerOn(self):
        self.logger.log(VerbosityLevel["INFO"],
                        {
            "device_id": 0,
            "timestamp_ms": millis(),
            "info": "powerOn"
        })
        pyb.Pin.cpu.E12.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
        pyb.Pin.cpu.E12.value(1)

    def powerOff(self):
        self.logger.log(VerbosityLevel["INFO"],
                        {
            "device_id": 0,
            "timestamp_ms": millis(),
            "info": "powerOff"
        })
        pyb.Pin.cpu.E12.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
        pyb.Pin.cpu.E12.value(0)

    def reset_all(self):
        self.stop_discovery()
        self.afe_devices = []
        self.message_queue = []
        self.current_discovery_id = 1

    def close_all(self):
        self.logger.log(VerbosityLevel["INFO"], {
            "device_id": 0,
                        "info": "CLOSE ALL", "timestamp_ms": millis()})
        self.logger.request_new_file()
        self.logger.machine()
        self.use_automatic_restart = False
        for afe in self.afe_devices:
            afe.restart_device()
        self.powerOff()

    def clear_all_logs(self):
        try:
            import os
            for filename in os.listdir("/sd/logs"):
                os.remove("/sd/logs/" + filename)
        except Exception as e:
            p.print("Error clearing logs: {}".format(e))

    def _dequeue_message_copy(self, _):
        self.msg_to_process = self.rxDeviceCAN.get()
        return self.msg_to_process
    
    # async def _dequeue_message(self):
    #     self.msg_to_process = self.rxDeviceCAN.get()
    #     await uasyncio.sleep_ms(10)


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

    def process_received_messages(self, timer=None):
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
        # print(message)
        self.msg_to_process = None
        # try:
        if message is None:
            return
        afe_id = (message[0] >> 2) & 0xFF  # unmask the AFE ID
        afe = self.get_afe_by_id(afe_id)
        if afe is None:  # Add new discovered AFE
            # Create a new AFE device instance with the discovered ID
            afe = AFEDevice(self.can_bus, afe_id, logger=self.logger)
            self.logger.log(VerbosityLevel["INFO"],
                            {
                                "device_id": 0,
                                "timestamp_ms": millis(),
                                "info": "found new AFE {}".format(afe_id)
            })
            # Add the new AFE device to the list of known devices
            self.afe_devices.append(afe)
            # if afe_id == 35:
            if not self.afe0:
                self.afe0 = afe
        # Process the received data using the AFE device's method
        afe.process_received_data(message)
        # except Exception as e:
        #     p.print("process_received_messages: {}".format(e))
        
    async def process_received_messages_async(self):
        self.process_received_messages(None)
        
    def discover_devices(self, timer=None):
        """ Periodically discover AFEs on the CAN bus. """
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
            # self.logger.log(VerbosityLevel["ERROR"],"CAN BUS ERROR {}".format(self.can_bus.state()))
            if self.can_bus.state() > 2:
                self.can_bus.restart()
            return
        try:
            # Check if AFE with current ID is already discovered or if we've exceeded the maximum ID
            if self.current_discovery_id > self.afe_id_max:
                self.current_discovery_id = self.afe_id_min  # Reset to the minimum ID

            if not any(afe.is_online and afe.device_id == self.current_discovery_id for afe in self.afe_devices):
                # Send get ID msg to discover new AFE
                self.can_bus.send(
                    b"\x00\x11", self.current_discovery_id << 2, timeout=self.tx_timeout_ms)
                self.last_tx_time = millis()
                self.logger.log(VerbosityLevel["DEBUG"], "Sending discovery message to ID: {}".format(
                    self.current_discovery_id))
            else:
                self.logger.log(VerbosityLevel["DEBUG"], "AFE with ID {} already discovered".format(
                    self.current_discovery_id))

            self.current_discovery_id += 1  # Increment ID for the next iteration

        except Exception as e:
            self.logger.log(
                VerbosityLevel["ERROR"], "discover_devices: HUB Error sending: {}".format(e))

    def start_discovery(self):
        """ Start the device discovery process. """
        self.discovery_active = True

    def stop_discovery(self):
        """ Stop the device discovery process. """
        self.discovery_active = False
        p.print("STOP DISCOVERY")

    def get_afe_by_id(self, afe_id) -> AFEDevice:
        if len(self.afe_devices) == 0:
            return None
        for afe in self.afe_devices:
            if afe.device_id == afe_id:
                return afe
        return None

    def get_configuration_from_files(self, afe_id, callibration_data_file_csv="dane_kalibracyjne.csv", TempLoop_file_csv="TempLoop.csv", UID=None):
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
        TempLoop_data, TempLoop_data_mean = read_callibration_csv(
            TempLoop_file_csv)
        callib_data, callib_data_mean = read_callibration_csv(
            callibration_data_file_csv)

        callibration = {'ID': afe_id}
        for c0 in [callib_data, TempLoop_data]:
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
        for c0 in [callib_data_mean, TempLoop_data_mean]:
            for g in ['M', 'S']:
                for k, v in c0[g].items():
                    if k not in callibration[g]:  # no key
                        self.logger.log(
                            VerbosityLevel["WARNING"], "Calibration data: AFE {}: No key: {}".format(afe_id, k))
                        callibration[g][k] = ''
                    elif len(str(callibration[g][k])) == 0:  # empty string:
                        self.logger.log(
                            VerbosityLevel["WARNING"], "Calibration data: AFE {}: No value {}, set to {}".format(afe_id, k, v))
                        callibration[g][k] = v  # set default value
        return callibration

    def _get_subdevice_ch_id(self, g):
        return AFECommandSubdevice.AFECommandSubdevice_master if g == 'M' else AFECommandSubdevice.AFECommandSubdevice_slave

    def default_get_measurement(self, afe_id=35, callback=None):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True, "timeout_start_on": 10000}
        if callback is not None:
            commandKwargs["callback"] = callback
        afe.enqueue_command(AFECommand.getSensorDataSi_last_byMask, [
                            0xFF], **commandKwargs)
        afe.enqueue_command(AFECommand.getSensorDataSi_average_byMask, [
                            0xFF], **commandKwargs)

    def default_callback_return(self, msg=None):
        return msg

    def default_get_measurement_last(self, afe_id=35, callback=None):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 20220,
                         "preserve": True, "timeout_start_on": 5000}
        if callback is not None:
            commandKwargs["callback"] = callback
        afe.enqueue_command(AFECommand.getSensorDataSi_last_byMask, [
                            0xFF], **commandKwargs)

    def callback_1(self, msg=None):
        msg["callback"] = None
        msg = json.dumps(msg)
        p.print("callback:", msg)

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
        afe.logger.log(VerbosityLevel["INFO"],
                       {
                           "device_id": afe.device_id,
                           "timestamp_ms": millis(),
                           "info": "default_set_dac"
        })
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": False, "timeout_start_on_send_ms": 2000}

        for g in ["M", "S"]:
            afe.enqueue_u16_for_channel(AFECommand.setDACValueRaw_bySubdeviceMask, self._get_subdevice_ch_id(
                g), dac_master if g == 'M' else dac_slave, **commandKwargs)
            afe.enqueue_command(AFECommand.setDAC_bySubdeviceMask, [
                                self._get_subdevice_ch_id(g), 1], **commandKwargs)
            afe.enqueue_gpio_set(afe.AFEGPIO_EN_HV0 if g ==
                                 'M' else afe.AFEGPIO_EN_HV1, 1, **commandKwargs)

    def default_start_temperature_loop(self, afe_id=35):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        afe.logger.log(VerbosityLevel["INFO"],
                       {
                           "device_id": afe.device_id,
                           "timestamp_ms": millis(),
                           "info": "default_start_temperature_loop"
        })
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True, "timeout_start_on_send_ms": 2000}

        for g in ["M", "S"]:
            afe.enqueue_command(AFECommand.setTemperatureLoopForChannelState_byMask_asStatus, [
                                self._get_subdevice_ch_id(g), 1], **commandKwargs)

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
        afe.logger.log(VerbosityLevel["INFO"],
                       {
                           "device_id": afe.device_id,
                           "timestamp_ms": millis(),
                           "info": "default_periodic_measurement_download_all"
        })
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True, "timeout_start_on_send_ms": 2000}
        afe.enqueue_u32_for_channel(
            AFECommand.setChannel_period_ms_byMask, 0xFF, ms, **commandKwargs)

    def default_setCanMsgBurstDelay_ms(self, afe_id=35, ms=10):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        afe.logger.log(VerbosityLevel["INFO"],
                       {
                           "device_id": afe.device_id,
                           "timestamp_ms": millis(),
                           "info": "default_setCanMsgBurstDelay_ms"
        })
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True,
                         "timeout_start_on_send_ms": 2000,
                         "error_callback": self.callback_afe_error}
        afe.enqueue_u32_for_channel(
            AFECommand.setCanMsgBurstDelay_ms, 0x00, ms, **commandKwargs)

    def default_setAfe_can_watchdog_timeout_ms(self, afe_id=35, ms=60000):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        afe.logger.log(VerbosityLevel["INFO"],
                       {
                           "device_id": afe.device_id,
                           "timestamp_ms": millis(),
                           "info": "default_setAfe_can_watchdog_timeout_ms"
        })
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True,
                         "timeout_start_on_send_ms": 2000,
                         "error_callback": self.callback_afe_error}
        afe.enqueue_u32_for_channel(
            AFECommand.setAfe_can_watchdog_timeout_ms, 0x00, ms, **commandKwargs)

    def default_accept(self, afe_id=35):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True,
                         "timeout_start_on_send_ms": 2000,
                         "error_callback": self.callback_afe_error,
                         "callback": afe.callback_is_configured}
        afe.enqueue_command(AFECommand.getTimestamp, None, **commandKwargs)

    def default_get_UID(self, afe_id=35):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True,
                         "error_callback": self.callback_afe_error
                         }
        afe.enqueue_command(AFECommand.getSerialNumber, None, **commandKwargs)

    def defualt_getSyncTimestamp(self, afe_id=35):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": True,
                         "error_callback": self.callback_afe_error
                         }
        afe.enqueue_command(AFECommand.getSyncTimestamp, None, **commandKwargs)

    def default_afe_pause(self, afe_id=35):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        commandKwargs = {"timeout_ms": 10220,
                    "preserve": True,
                    "error_callback": None
                    }
        afe.enqueue_u32_for_channel(
            AFECommand.setChannel_period_ms_byMask,
            0xFF, 0, **commandKwargs)

    def default_full(self, afe_id=35):
        self.powerOn()
        self.default_afe_pause(afe_id)
        self.default_setCanMsgBurstDelay_ms(afe_id, 0)
        self.default_setAfe_can_watchdog_timeout_ms(afe_id, 1000000)
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        afe.begin_configuration(timeout_ms=20000)
        self.default_get_UID(afe_id)
        self.default_procedure(afe_id)
        self.default_set_dac(afe_id)
        self.default_start_temperature_loop(afe_id)
        self.default_setCanMsgBurstDelay_ms(afe_id, 50)
        self.default_accept(afe_id)
        self.defualt_getSyncTimestamp(afe_id)


    def reset(self, afe_id=35):
        for afe in self.afe_devices:
            if afe.device_id == afe_id:
                afe.enqueue_command(0x03)

    def test1(self, afe_id=35, command=0xF8):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        afe.enqueue_command(command)

    def test2(self, afe_id=35, command=0xF9):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        afe.enqueue_command(command, preserve=True)

    def test3(self, afe_id=35, command=0xF7, mask=0xFF):
        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        afe.enqueue_command(command, [mask], preserve=True)

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

    def d(self, cmd, data=None):
        afe = self.get_afe_by_id(35)
        if afe is None:
            return
        afe.enqueue_command(cmd, data, preserve=True)

    def callback_afe_error(self, kwargs=None):
        p.print("callback_afe_error: {}".format(kwargs))
        afe: AFEDevice = kwargs["afe"]
        afe.restart_device()

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

        def get_T_measured_ch_id(g):
            return AFECommandChannel.AFECommandChannel_7 if g == 'M' else AFECommandChannel.AFECommandChannel_6

        def get_U_measured_ch_id(g):
            return AFECommandChannel.AFECommandChannel_2 if g == 'M' else AFECommandChannel.AFECommandChannel_3

        def get_I_measured_ch_id(g):
            return AFECommandChannel.AFECommandChannel_4 if g == 'M' else AFECommandChannel.AFECommandChannel_5

        def get_general_ch_id_mask(g):
            return AFECommandChannelMask.master if g == 'M' else AFECommandChannelMask.slave

        afe = self.get_afe_by_id(afe_id)
        if afe is None:
            return
        configuration = self.get_configuration_from_files(afe_id)
        afe.configuration = configuration.copy()
        afe.logger.log(VerbosityLevel["INFO"],
                       {
                           "device_id": afe.device_id,
                           "timestamp_ms": millis(),
                           "info": "default_procedure",
                           "msg": configuration
        })
        afe.callback_1 = self.callback_1
        commandKwargs = {"timeout_ms": 10220,
                         "preserve": False,
                         "timeout_start_on_send_ms": 3000,
                         "callback_error": self.callback_afe_error}

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

                if ks == "T_measured_a":
                    ch_id = get_T_measured_ch_id(g)
                    afe.enqueue_float_for_channel(
                        AFECommand.setChannel_a_byMask, ch_id, v, **commandKwargs)
                elif ks == "T_measured_b":
                    ch_id = get_T_measured_ch_id(g)
                    afe.enqueue_float_for_channel(
                        AFECommand.setChannel_b_byMask, ch_id, v, **commandKwargs)
                elif ks == "offset":
                    ch_id = self._get_subdevice_ch_id(g)
                    afe.enqueue_u16_for_channel(
                        AFECommand.setAD8402Value_byte_byMask, ch_id, int(v), **commandKwargs)
                elif ks == "U_measured_a":
                    ch_id = get_U_measured_ch_id(g)
                    afe.enqueue_float_for_channel(
                        AFECommand.setChannel_a_byMask, ch_id, v, **commandKwargs)
                elif ks == "U_measured_b":
                    ch_id = get_U_measured_ch_id(g)
                    afe.enqueue_float_for_channel(
                        AFECommand.setChannel_b_byMask, ch_id, v, **commandKwargs)
                elif ks == "I_measured_a":
                    ch_id = get_I_measured_ch_id(g)
                    afe.enqueue_float_for_channel(
                        AFECommand.setChannel_a_byMask, ch_id, v, **commandKwargs)
                elif ks == "I_measured_b":
                    ch_id = get_I_measured_ch_id(g)
                    afe.enqueue_float_for_channel(
                        AFECommand.setChannel_b_byMask, ch_id, v, **commandKwargs)
                elif ks == "U_set_a":
                    ch_id = self._get_subdevice_ch_id(g)
                    afe.enqueue_float_for_channel(
                        AFECommand.setRegulator_a_dac_byMask, ch_id, v, **commandKwargs)
                elif ks == "U_set_b":
                    ch_id = self._get_subdevice_ch_id(g)
                    afe.enqueue_float_for_channel(
                        AFECommand.setRegulator_b_dac_byMask, ch_id, v, **commandKwargs)
                elif ks == "V_opt":
                    ch_id = self._get_subdevice_ch_id(g)
                    afe.enqueue_float_for_channel(
                        AFECommand.setRegulator_V_opt_byMask, ch_id, v, **commandKwargs)
                elif ks == "dV/dT":
                    ch_id = self._get_subdevice_ch_id(g)
                    afe.enqueue_float_for_channel(
                        AFECommand.setRegulator_dV_dT_byMask, ch_id, v, **commandKwargs)
                elif ks == "avg_number":  # Maximum nuber of samples used in averaging
                    avg_number = v
                    if v is '':
                        avg_number = 256
                    else:
                        avg_number = v
                    avg_number = int(round(avg_number))
                    continue
                elif ks == "avg_mode":
                    if not v:
                        v = "NONE"
                    avg_mode = AFECommandAverage[v]
                    ch_id = self._get_subdevice_ch_id(g)
                    afe.enqueue_command(AFECommand.setAveragingMode_byMask, [ch_id,
                                                                             avg_mode
                                                                             ], **commandKwargs)
                elif ks == "avg_alpha":  # Average parameter, usually weight
                    ch_id = get_general_ch_id_mask(g)
                    if v is '':
                        afe.enqueue_float_for_channel(
                            AFECommand.setAveragingAlpha_byMask, ch_id, 1.0/(10000*100.0), **commandKwargs)
                    else:
                        afe.enqueue_float_for_channel(
                            AFECommand.setAveragingAlpha_byMask, ch_id, v, **commandKwargs)
                elif ks == "time_sample":  # time sample
                    ch_id = get_general_ch_id_mask(g)
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
                    afe.enqueue_u32_for_channel(
                        AFECommand.setChannel_dt_ms_byMask, ch_id, time_sample_ms, **commandKwargs)
                else:
                    continue
                for uch in afe.unmask_channel(ch_id):
                    self.logger.log(VerbosityLevel["DEBUG"], {
                                    "device_id": afe.device_id,
                                    "timestamp_ms": millis(),
                                    "debug": "AFE {} {} Loading {} (CH{} ? {}) value {}".format(
                                        afe_id, g, k, uch, e_ADC_CHANNEL[uch], v)
                                    })
                
            afe.enqueue_u32_for_channel(
                AFECommand.setAveraging_max_dt_ms_byMask, get_general_ch_id_mask(g), int(round(time_sample_ms * avg_number)), **commandKwargs)
            afe.enqueue_float_for_channel(
                AFECommand.setChannel_multiplicator_byMask, get_general_ch_id_mask(g), 1.0, **commandKwargs)
        afe.enqueue_command(AFECommand.startADC, [
            0xFF, 0xFF], **commandKwargs)

    def parse(self, msg):
        p.print("Parsed: {}".format(msg))

    def send_back_data(self, afe_id: int):
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
        p.print("Send back: {}".format(json.dumps(toSend)))

    def main_process(self, timer=None):
        # if self.use_rxcallback:
        #     micropython.schedule(self._dequeue_message_copy, 0)
        # #     micropython.schedule(self.handle_can_rx_polling_schedule, 0)
        # else:
        #     self._dequeue_message_copy(0)
        # #     self.handle_can_rx_polling_schedule(0)
        # micropython.schedule(self._dequeue_message_copy, 0)
        # self.rxDeviceCAN.main_process()
        self._dequeue_message_copy(0)
        
        self.discover_devices()
        # if self.rx_process_active:
        #     micropython.schedule(self.process_received_messages, 0)
        # self.process_received_messages_async()
        self.process_received_messages(0)
        # uasyncio.create_task(self.process_received_messages_async())
        if self.afe_manage_active:
            for afe in self.afe_devices:
                afe.manage_state()
                if self.use_automatic_restart:
                    if not afe.is_configuration_started:
                        self.default_full(afe_id=afe.device_id)
                        p.print("AFE {} was restarted".format(
                            afe.device_id))
                    if afe.is_configured and afe.periodic_measurement_download_is_enabled is False:
                        afe.periodic_measurement_download_is_enabled = True
                        afe.start_periodic_measurement_by_config()
                        # report_every_ms = afe.configuration.get()

                        # self.default_periodic_measurement_download_all(
                        #     afe_id=afe.device_id, ms=afe.configuration.get)

        if self.curent_function is not None:  # check if function is running
            if (millis() - self.curent_function_timestamp_ms) > self.curent_function_timeout_ms:
                self.curent_function = None
                self.curent_function_retval = "timeout"

    async def main_loop(self):
        while self.run:
            # print("  H")
            # print_lock.acquire()
            self.main_process()
            # p.process_queue()
            # self.logger.machine()
            wdt.feed()
            # time.sleep_us(10)
            await uasyncio.sleep_ms(0)
            # print_lock.release()
            # time.sleep_ms(1)
            # time.sleep(0.01)
            # time.sleep_us(1)


def initialize_can_hub(can_bus: pyb.CAN, logger, use_rxcallback=True, **kwargs):
    """ Initialize the CAN bus and HUB. """
    can_bus.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
                 sjw=1, bs1=7, bs2=2, auto_restart=True)
    # can_bus.setfilter(0, can_bus.MASK32, 0, (0, 0))
    can_bus.setfilter(0, can_bus.MASK16, 0, (0, 0, 0, 0))

    p.print("CAN Bus Initialized")
    logger.verbosity_level = VerbosityLevel["INFO"]
    # logger.verbosity_level = VerbosityLevel["DEBUG"]
    # logger.print_verbosity_level = VerbosityLevel["DEBUG"]
    logger.print_verbosity_level = VerbosityLevel["CRITICAL"]
    rxDeviceCAN = RxDeviceCAN(can_bus, use_rxcallback)
    hub = HUBDevice(can_bus, logger=logger,
                    rxDeviceCAN=rxDeviceCAN,
                    use_rxcallback=use_rxcallback, **kwargs)

    return can_bus, hub, rxDeviceCAN
