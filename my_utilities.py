try:
    import pyb
except:
    pass
import json
import os

try:
    class DummyLock:
        def acquire(self, blocking=True, timeout=-1): return True # pragma: no cover
        def release(self): pass # pragma: no cover
        def locked(self): return False # pragma: no cover
    class DummyRTC: # pragma: no cover
        def datetime(self): return (2000,1,1,1,0,0,0,0) # year, month, day, weekday, hour, minute, second, subsecond
    import _thread
    import micropython
    import machine
    # import uasyncio # Added for async operations
    import gc
    import pyb
    # Create a lock for safe printing, initialized once when the module is imported.
    # print_lock = _thread.allocate_lock()
    rtc = machine.RTC()
except:
    # Fallback for environments where _thread or machine might not be available (e.g., PC testing)
    rtc = DummyRTC()
    pass
try:
    import uasyncio
except ImportError:
    import asyncio
    # --- Mock uasyncio module (using Python's asyncio) ---
    class UasyncioShim:
        def __init__(self):
            self._loop = None

        def get_event_loop(self):
            if self._loop is None:
                try:
                    self._loop = asyncio.get_running_loop()
                except RuntimeError:
                    self._loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(self._loop)
            return self # In MicroPython, get_event_loop() often returns the loop itself which has create_task etc.

        def create_task(self, coro):
            if self._loop is None: # Ensure loop exists if get_event_loop wasn't called explicitly before create_task
                self.get_event_loop()
            return self._loop.create_task(coro)

        async def sleep(self, t_s):
            await asyncio.sleep(t_s)

        async def sleep_ms(self, t_ms):
            await asyncio.sleep(t_ms / 1000.0)

        def run_forever(self): # This method is on the loop object
            if self._loop:
                try:
                    print("SIM: Starting asyncio event loop run_forever().")
                    self._loop.run_forever()
                except KeyboardInterrupt:
                    print("SIM: asyncio loop interrupted.")
                finally:
                    if self._loop.is_running():
                        self._loop.stop()
                    # self._loop.close() # Closing should be handled carefully, esp. if run in a thread
                    print("SIM: asyncio loop finished run_forever().")
            else:
                print("SIM: uasyncio.run_forever() called but no loop was properly retrieved/set.")

        def run(self, coro): # For uasyncio.run(main())
            return asyncio.run(coro)
        # import asyncio as uasyncio
    uasyncio = UasyncioShim()


import time

print_lock = DummyLock()

def is_timeout(timestamp_ms, timeout_ms):
    if timeout_ms == 0:
        return False
    return time.ticks_diff(time.ticks_ms(), timestamp_ms) > timeout_ms

def is_delay(timestamp_ms, delay_ms):
    if delay_ms == 0:
        return False
    return time.ticks_diff(time.ticks_ms(), timestamp_ms) < delay_ms

class wdt_x:
    def __init__(self, timeout=2000):
        try:
            # from machine import WDT
            # self.wdt = WDT(timeout=timeout)
            self.wdt = None
        except:
            self.wdt = None

    def feed(self):
        if self.wdt is not None:
            self.wdt.feed()


try:
    if False:
        wdt = wdt_x()
    else:
        from machine import WDT
        wdt = WDT(timeout=20*1000)
        wdt.feed()

except:
    pass

rtc_synced = False

def rtc_unix_timestamp():
    dt = rtc.datetime()  # (year, month, day, weekday, hours, minutes, seconds, subseconds)
    # Rearrange to (year, month, day, hour, minute, second, weekday, yearday)
    tm = (dt[0], dt[1], dt[2], dt[4], dt[5], dt[6], dt[3], 0)
    # Add seconds from 1970-01-01 to 2000-01-01
    return time.mktime(tm) + 946684800

def rtc_datetime_pretty():
    dt = rtc.datetime()
    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
        dt[0], dt[1], dt[2], dt[4], dt[5], dt[6])


# Custom print function with lock


class Print:
    def __init__(self):
        pass

    def print(self, *args, **kwargs):
        return
        # with print_lock:
        #     print(*args, **kwargs)

    def debug(self, *args, **kwargs):
        # with print_lock:
        print(*args, **kwargs)


class PrintButLouder:
    def __init__(self):
        self.queue = []
        # print_lock is globally initialized when this module is imported.

    def _lock(self, blocking=True, sleep_ms=1):
        """Acquires the global print_lock."""
        global print_lock
        if blocking:
            while not print_lock.acquire(True, -1): # blocking acquire
                try:
                    time.sleep_ms(sleep_ms)
                except AttributeError:  # pragma: no cover
                    time.sleep(sleep_ms / 1000.0) # Fallback for PC
        else:
            return print_lock.acquire(False) # non-blocking acquire
        return True

    def _unlock(self):
        """Releases the global print_lock."""
        global print_lock
        if print_lock.locked():
            print_lock.release()

    async def print(self, *args, **kwargs):
        if self._lock(): # Acquire lock
            try:
                self.queue.append((args, kwargs))
                if len(self.queue) > 50: # TODO: Make this configurable
                    self.queue.pop(0)
            finally:
                self._unlock() # Ensure lock is released

    async def machine(self):
        if not self.queue:
            return
        
        item = None
        if self._lock(): # Acquire lock
            try:
                if self.queue: # Re-check queue is not empty after acquiring lock
                    item = self.queue.pop(0)
            finally:
                self._unlock() # Ensure lock is released
        
        if item:
            print(*(item[0]), **item[1])
        await uasyncio.sleep_ms(0) # Yield to the event loop after printing one item
 

p = PrintButLouder()
# P = PrintButLouder()


class AFECommand:
    getSerialNumber = 0x0
    getVersion = 0x1
    resetAll = 0x3
    startADC = 0x4
    getTimestamp = 0x5
    getSyncTimestamp = 0x6
    resetCAN = 0x7
    getSensorDataSi_last_byMask = 0x30
    getSensorDataSi_average_byMask = 0x31
    getSensorDataSiAndTimestamp_average_byMask = 0x3b
    getSensorDataSi_periodic = 0x3f
    setSensorDataSi_periodic_last = 0x40
    setSensorDataSiAndTimestamp_periodic_last = 0x41
    setSensorDataSi_periodic_average = 0x42
    setSensorDataSiAndTimestamp_periodic_average = 0x43
    transmitSPIData = 0xa0
    setAD8402Value_byte_byMask = 0xa1
    writeGPIO = 0xa2
    setCanMsgBurstDelay_ms = 0xa3
    setAfe_can_watchdog_timeout_ms = 0xa4
    setTemperatureLoopForChannelState_byMask_asStatus = 0xc1
    setDACValueRaw_bySubdeviceMask = 0xc2
    setDACValueSi_bySubdeviceMask = 0xc3
    stopTemperatureLoopForAllChannels = 0xc4
    setDAC_bySubdeviceMask = 0xc5
    setDACRampOneBytePerMillisecond_ms = 0xc6
    setAveragingMode_byMask = 0xd0
    setAveragingAlpha_byMask = 0xd1
    setAveragingBufferSize_byMask = 0xd2
    setChannel_dt_ms_byMask = 0xd3
    setAveraging_max_dt_ms_byMask = 0xd4
    setChannel_multiplicator_byMask = 0xd5
    setAveragingSubdevice = 0xd6
    setChannel_a_byMask = 0xd7
    setChannel_b_byMask = 0xd8
    setChannel_period_ms_byMask = 0xD9

    setRegulator_a_dac_byMask = 0xE5
    setRegulator_b_dac_byMask = 0xE6
    setRegulator_dV_dT_byMask = 0xE7
    setRegulator_V_opt_byMask = 0xE8
    setRegulator_V_offset_byMask = 0xE9

    debug_machine_control = 0xf1


class AFECommandChannel:
    AFECommandChannel_0 = 0x1
    AFECommandChannel_1 = 0x2
    AFECommandChannel_2 = 0x4
    AFECommandChannel_3 = 0x8
    AFECommandChannel_4 = 0x10
    AFECommandChannel_5 = 0x20
    AFECommandChannel_6 = 0x40
    AFECommandChannel_7 = 0x80


class AFECommandChannelMask:
    master = AFECommandChannel.AFECommandChannel_0 | AFECommandChannel.AFECommandChannel_2 | AFECommandChannel.AFECommandChannel_4 | AFECommandChannel.AFECommandChannel_7
    slave = AFECommandChannel.AFECommandChannel_1 | AFECommandChannel.AFECommandChannel_3 | AFECommandChannel.AFECommandChannel_5 | AFECommandChannel.AFECommandChannel_6


class AFECommandSubdevice:
    AFECommandSubdevice_master = 0x1
    AFECommandSubdevice_slave = 0x2
    AFECommandSubdevice_both = 0x3


AFECommandAverage = {
    "NONE": 0x00,
    "STANDARD": 0x01,
    "EXPONENTIAL": 0x02,
    "MEDIAN": 0x03,
    "RMS": 0x04,
    "HARMONIC": 0x05,
    "GEOMETRIC": 0x06,
    "TRIMMED": 0x07,
    "WEIGHTED_EXPONENTIAL": 0x08,
    "ARIMA": 0x09
}


e_ADC_CHANNEL: dict[int, str] = {
    0: "DC_LEVEL_MEAS0",
    1: "DC_LEVEL_MEAS1",
    2: "U_SIPM_MEAS0",
    3: "U_SIPM_MEAS1",
    4: "I_SIPM_MEAS0",
    5: "I_SIPM_MEAS1",
    6: "TEMP_EXT",
    7: "TEMP_LOCAL"
}

def get_e_ADC_CHANNEL(channel_name):
    for k, v in e_ADC_CHANNEL:
        if v == channel_name:
            return k
    return None

channel_name_xxx: dict[str, int] = {
    "MASTER_VOLTAGE": AFECommandChannel.AFECommandChannel_2,
    "SLAVE_VOLTAGE": AFECommandChannel.AFECommandChannel_3,
    "MASTER_CURRENT": AFECommandChannel.AFECommandChannel_4,
    "SLAVE_CURRENT": AFECommandChannel.AFECommandChannel_5,
    "MASTER_TEMPERATURE": AFECommandChannel.AFECommandChannel_6,
    "SLAVE_TEMPERATURE": AFECommandChannel.AFECommandChannel_7,
}

ResetReason: dict[int, str] = {
    0: "RESET_UNKNOWN",
    1: "RESET_POWER_ON",
    2: "RESET_PIN",
    3: "RESET_BROWN_OUT",
    4: "RESET_SOFTWARE",
    5: "RESET_WATCHDOG",
    6: "RESET_WINDOW_WATCHDOG",
    7: "RESET_LOW_POWER"
}

VerbosityLevel = {
    "DEBUG": 4,
    "INFO": 3,
    "WARNING": 2,
    "ERROR": 1,
    "CRITICAL": 0,
    "MEASUREMENT": -1
}


class CommandStatus:
    NONE = 0x000
    IDLE = 0x001
    RECIEVED = 0x010
    ERROR = 0x100


class AFECommandGPIO:
    def __init__(self, pin=None, port=None):
        self.port = {
            None: None,
            "A": 0, "PORTA": 0,
            "B": 1, "PORTB": 1,
            "C": 2, "PORTC": 2
        }
        self.pin = pin
        self.port = self.port[port]
        # self.EN_HV0_Pin = 10
        # self.EN_HV0_Port = self.port["B"]
        # self.EN_HV1_Pin = 11
        # self.EN_HV1_Port = self.port["B"]
        # self.blink_Pin = 9
        # self.blink_Port = self.port["A"]

# Function to get current time in milliseconds


def millis():
    return time.ticks_ms()


def extract_bracketed(text):
    results = []
    temp = ''
    inside = False

    for char in text:
        if char == '[':
            inside = True
            temp = ''
        elif char == ']':
            if inside:
                results.append(temp)
                inside = False
        elif inside:
            temp += char

    return results

# # Measurement structure
# class SensorReading:
#     def __init__(self, timestamp_ms=0, value=0.0):
#         self.timestamp_ms = timestamp_ms
#         self.value = None
#     def __str__(self):
#         return "{{\"timestamp_ms\":{},\"value\":{}}}".format(self.timestamp_ms, self.value)

# Channel structure


class SensorChannel:
    def __init__(self, channel_id):
        self.channel_id = channel_id
        self.config = {}
        self.name = e_ADC_CHANNEL[channel_id]
        self.last_recieved_data = {"last":{},"average":{}}
        # # Use .get() to avoid KeyError if a key is missing
        # self.time_interval_ms = kwargs.get("time_interval_ms", 0)
        # self.alpha = kwargs.get("alpha", 0)
        # self.multiplicator = kwargs.get("multiplicator", 1)
        # self.a = kwargs.get("a", 0)
        # self.b = kwargs.get("b", 0)
        # averagingmodes = AFECommandAverage()
        # self.averaging_mode = kwargs.get("averaging_mode", averagingmodes.NONE)
        # self.latest_reading = SensorReading()

        # # Measurement download settings
        # self.periodic_interval_ms = kwargs.get("periodic_interval_ms", 0)
        # self.periodic_sending_is_enabled = False

# class EmptyLogger:
#     def __init__(self,verbosity_level: int = VerbosityLevel["INFO"],**kwargs):
#         self.verbosity_level = verbosity_level
#         p.print("EMPTY LOGGER")
#     def _should_log(self, level: int):
#         return self.verbosity_level >= level
#     def new_file(self):
#         pass
#     def log(self, level: int, message):
#         if self._should_log(level):
#             p.print("{} @ {}: {}".format(millis(), level, message))
#     def sync(self):
#         pass
#     def close(self):
#         pass
#     def read_logs(self):
#         pass
#     def clear_logs(self):
#         pass
#     def print_lines(self):
#         pass


class JSONLogger:
    def __init__(self, filename="log.json", parent_dir="/sd/logs", verbosity_level=VerbosityLevel["INFO"], keep_file_open=True):
        self.parent_dir = parent_dir
        self.verbosity_level = verbosity_level
        self.filename = filename
        self.filename_org = filename
        self.file = None
        self.print_verbosity_level = VerbosityLevel["CRITICAL"]
        self.last_sync = 0
        self.sync_every_ms = 1000
        self.buffer = []
        self.burst_delay_ms = 1
        self.burst_timestamp_ms = 0
        self._requestNewFile = False
        self.log_queue = []  # Introduce a log queue
        self._requestRenameFile = False
        self.file_rows = 0
        self.cursor_position = 0
        self.cursor_position_last = 0
        self.rtc_synced = False
        self.keep_file_open = keep_file_open
        
        self.log_queue_max_len = 128
        # self.lock_process_log_queue = False
        self.writer_main_loop_yield_ms = 50
        
        self.request_print_last_lines = 0
        
        self.run = True
        
        self.divide_log_by_chunk_size = 0
        
        # self.json_buffer = bytearray(2**13)

        if self.keep_file_open:
            self.file = None # File will be opened by new_file or on first log
        else:
            self.file = None # File is never persistently kept open
        self.request_new_file()

    def _ensure_directory(self):
        try:
            if not self._path_exists(self.parent_dir):
                os.makedirs(self.parent_dir) # Use makedirs for parent_dir
        except OSError as e:
            # This might be an issue if p.print itself is not fully initialized or causes recursion
            # Consider a simpler print for critical bootstrap errors.
            print("CRITICAL: Failed to create log directory {}: {}".format(self.parent_dir,e))

    def _get_unique_filename(self, filename):
        base, ext = filename.rsplit(
            ".", 1) if "." in filename else (filename, "")
        counter = 1
        while self._path_exists(filename):
            filename = "{}_{}{}".format(
                base, counter, "." + ext if ext else "")
            counter += 1
        return filename

    def _path_exists(self, path):
        try:
            os.stat(path)
            return True
        except OSError:
            return False

    def _should_log(self, level):
        return level <= self.verbosity_level

    async def get_new_file_path(self):
        self._ensure_directory()
        filename_datetime = self.filename_org
        if self.rtc_synced:
            year, month, day, hour, minute, second = time.gmtime()[0:6]
            filename_datetime = "log_{:04d}{:02d}{:02d}_{:02d}{:02d}{:02d}.json".format(
                year, month, day, hour, minute, second
            )
        await uasyncio.sleep_ms(0) # Yield
        return self._get_unique_filename(
            "{}/{}".format(self.parent_dir, filename_datetime))

    async def new_file(self):
        if self.keep_file_open:
            try:
                if self.file is not None:
                    await self.sync() # Sync before closing
                    self.file.close()
                    await uasyncio.sleep_ms(0)
            except Exception as e:
                p.print("Error closing old file in new_file (keep_open=True): {}".format(e))
            
            self.filename = await self.get_new_file_path() # Ensures directory exists before open
            try:
                # Open in write mode, truncating for a new file
                self.file = open(self.filename, "w") 
                await uasyncio.sleep_ms(0)
            except Exception as e:
                p.print("ERROR new_file (keep_open=True): {}".format(e))
                self.file = None # Ensure file is None on error
        else: # not keep_file_open
            if self.file is not None: # Should be None, but as a safeguard
                try:
                    self.file.close()
                    await uasyncio.sleep_ms(0)
                except Exception as e:
                    p.print("Error closing file in new_file (keep_open=False): {}".format(e))
                self.file = None
            # Sets the filename, ensures dir. File is not opened here for this mode.
            self.filename = await self.get_new_file_path() 
        # self.filename was already set by the if/else block above
        self.cursor_position = 0
        self.cursor_position_last = 0
        self.file_rows = 0
        await p.print("New logger file target set to:", self.filename) # await p.print
        await uasyncio.sleep_ms(0) # Yield

    async def _log(self, level: int, message_chunk: str, chunk_id: int, chunk_id_max: int):
        if self.burst_delay_ms: # Rate limiting
            if is_delay(self.burst_timestamp_ms, self.burst_delay_ms):
                return 0  # Zero item saved
        self.burst_timestamp_ms = millis()

        if not self.filename: # If filename is not set (e.g. very first log)
            await self.new_file() # This will set self.filename
            if not self.filename: # Still no filename after new_file attempt
                await p.print("ERROR in _log: Could not determine filename.")
                return -1

        log_timestamp = millis()
        log_entry_dict = {
            "timestamp": log_timestamp,
            "rtc_timestamp": rtc_unix_timestamp(),
            "level": level,
            # "message_chunk": message_chunk,
            # "chunk_id": chunk_id,
            # "chunk_id_max": chunk_id_max
        }

        try:
            toLog = json.dumps(log_entry_dict) + "\n"
        except Exception as e_json:
            await p.print("ERROR in _log: JSON dump failed: {} for data: {}".format(e_json, log_entry_dict))
            return -1

        current_file_handle = None
        opened_in_scope = False

        if self.keep_file_open:
            if self.file is None: # Attempt to reopen if closed unexpectedly
                if not self.filename: # Should have been set by new_file
                    await p.print("ERROR in _log (keep_open=True): Filename not set.")
                    await self.request_new_file() # Try to recover
                    return -1
                try:
                    self.file = open(self.filename, "a")
                    await uasyncio.sleep_ms(0)
                except Exception as e_open:
                    await p.print("ERROR in _log: Failed to reopen file {} (keep_open=True): {}".format(self.filename,e_open))
                    await self.request_new_file()
                    return -1
            current_file_handle = self.file
        else: # Not keep_file_open, open/close per write
            if not self.filename: # Ensure filename is valid
                 await p.print("Error in _log (keep_file_open=False): Filename not set before open attempt.")
                 await self.new_file() # Try to set it
                 if not self.filename:
                     await p.print("Critical Error in _log (keep_file_open=False): Could not establish a valid log file after new_file().")
                     return -1
            try:
                current_file_handle = open(self.filename, "a")
                opened_in_scope = True
                await uasyncio.sleep_ms(0)
            except Exception as e_open:
                await p.print("ERROR in _log: Failed to open file {} (keep_file_open=False): {}".format(self.filename, e_open))
                return -1
            
        if current_file_handle is None:
            await p.print("ERROR in _log: File handle is None for {} before write attempt.".format(self.filename))
            return -1
        try:
            self.cursor_position_last = self.file.tell()
            toLog = ""
            # Ensure message is a string before concatenation
            if isinstance(message_chunk, dict):
                # message_str = json.dumps(message_chunk)
                log_entry_dict["message"] = message_chunk
                current_file_handle.write(json.dumps(log_entry_dict) + "\n")
            else:
                message_str = str(message_chunk) # if not isinstance(message_chunk, str) else message_chunk
                if chunk_id == 0:
                    toLog = "{" + json.dumps(log_entry_dict)
                toLog += message_str
                if chunk_id == chunk_id_max:
                    toLog += "}\n"
                    # print(toLog)
                # print(msg_id,msg_id_max, toLog)
                self.file.write(toLog)
                self.cursor_position_last = current_file_handle.tell()
                current_file_handle.write(toLog)
            await uasyncio.sleep_ms(0) # Yield after write
            self.file_rows += 1
            self.cursor_position = self.file.tell()

            # if not self.keep_file_open: # Close if opened in this scope
            #     current_file_handle.flush()
            #     await uasyncio.sleep_ms(0)
            #     current_file_handle.close()
            #     await uasyncio.sleep_ms(0)
            #     current_file_handle = None
            self.cursor_position = current_file_handle.tell()

            if opened_in_scope: # if not keep_file_open, flush and close
                current_file_handle.flush()
                await uasyncio.sleep_ms(0)
                current_file_handle.close()
                await uasyncio.sleep_ms(0)
                # Do not set self.file to None here if it wasn't self.file

        except Exception as e:
            p.print("ERROR in _log writing to {}: {} -> {}".format(self.filename, e, toLog[:512])) # Todo check toLog
            # if self.keep_file_open and self.file:
            #     try: self.file.close()
            #     except: pass
            #     self.file = None
            # elif not self.keep_file_open and current_file_handle:
            #     try: current_file_handle.close()
            #     except: pass
            # await self.request_new_file()
            await p.print("ERROR in _log writing to {}: {} -> {}".format(self.filename,e,toLog[:200]))
            if opened_in_scope and current_file_handle:
                try: current_file_handle.close()
                except: pass
            if self.keep_file_open and self.file: # If it was self.file and it errored
                try: self.file.close()
                except: pass
                self.file = None
                await self.request_new_file() # Request new file on write error
            return -1 # skip this row
        
        if level >= self.print_verbosity_level:
            p.print("LOG:", str(message_chunk)[:60], "...")
            # Consider what to print here; message_chunk is just a part.
            # For verbose console logging, one might want the full original message,
            # but that's not available directly in _log.
            # await p.print(f"LOG_CHUNK (L{level}): {message_chunk[:60]}...")
            pass
        return 1  # One item saved
    
    async def _process_log_queue(self):
        # while self.lock_process_log_queue:
        #     await uasyncio.sleep_ms(10)
        # self.lock_process_log_queue = True
        if self.log_queue:
            # lock() # Acquire lock for safe file writing
            level, message_chunk_data, chunk_id, chunk_id_max = self.log_queue.pop(0)
            # unlock()
            # print("Processing")
            # self.lock_process_log_queue = False
            await self._log(level, message_chunk_data, chunk_id, chunk_id_max)
        await uasyncio.sleep_ms(0) # Yield
        return len(self.log_queue)

    async def log(self, level: int, message): # Changed to async def
        if self._should_log(level):
            if self.divide_log_by_chunk_size:
                if not isinstance(message, str): # Ensure message is a string
                    if isinstance(message, dict):
                        message = json.dumps(message)
                    else: # Convert other types to string
                        message = str(message)
                message_length = len(message)
                chunk_id_max = int(message_length // self.divide_log_by_chunk_size)

                for i in range(0, message_length, self.divide_log_by_chunk_size):
                    chunk_id = int(i // self.divide_log_by_chunk_size)
                    i_plus = i + self.divide_log_by_chunk_size
                    if i_plus > message_length:
                        i_plus = message_length
                    
                    current_chunk_data = message[i:i_plus]

                    # Wait if the log queue is full.
                    while len(self.log_queue) >= self.log_queue_max_len:
                        # p.print(f"Log queue full ({len(self.log_queue)}), waiting...") # Optional: for debugging
                        await uasyncio.sleep_ms(10)  # Yield to other tasks/threads

                    # Directly append to the queue
                    self.log_queue.append((level, current_chunk_data, chunk_id, chunk_id_max))
            else:
                while len(self.log_queue) >= self.log_queue_max_len:
                    await uasyncio.sleep_ms(10)  # Yield to other tasks/threads
                await self._log(level, message, 0, 0)
                
    async def sync(self):
        if self.file is not None:
            if self.keep_file_open: # Only flush if we are keeping it open
                try:
                    self.file.flush()
                    await uasyncio.sleep_ms(0) # Yield after flush
                    self.last_sync = millis()
                except Exception as e:
                    await p.print("Error in sync (keep_open=True): {}".format(e)) # await p.print
        # If not keep_file_open, _log handles flush and close, so sync is a no-op.

    async def sync_process(self):
        if is_timeout(self.last_sync, self.sync_every_ms):
            await self.sync()
            await uasyncio.sleep_ms(0) # Yield

    async def close(self):
        if self.keep_file_open:
            await self.sync() # Ensure buffer is flushed if file was open
            if self.file is not None:
                try:
                    self.file.close()
                    await uasyncio.sleep_ms(0) # Yield after close
                except Exception as e:
                    await p.print("Error in close (keep_open=True): {}".format(e)) # await p.print
                self.file = None
        else: # not keep_file_open
            self.file = None # Should already be None

    # async def clear_logs(self):
    #     # This method is highly blocking and not easily made async without async os calls.
    #     # For now, it remains largely synchronous. Consider if it's called from async context.
    #     p.print("clear_logs is a blocking operation and not fully async.")
    #     file_was_managed_open = self.file is not None and self.keep_file_open
        
    #     if file_was_managed_open:
    #         try:
    #             self.file.close()
    #             await uasyncio.sleep_ms(0
    #                 self.log_queue.append((level, message[i:i_plus], chunk_id, chunk_id_max))
    #             except Exception as e:
    #                 # print("ERROR in JSONLogger.log:",e,":",len(self.log_queue))
            
                
    async def sync(self):
        if self.file is not None:
            if self.keep_file_open: # Only flush if we are keeping it open
                try:
                    self.file.flush() # Blocking
                    await uasyncio.sleep_ms(0) # Yield after flush
                    self.last_sync = millis()
                except Exception as e:
                    await p.print("Error in sync (keep_open=True): {}".format(e))
        # If not keep_file_open, _log handles flush and close, so sync is a no-op.

    async def sync_process(self):
        if is_timeout(self.last_sync, self.sync_every_ms):
            await self.sync()
            await uasyncio.sleep_ms(0) # Yield

    async def close(self):
        if self.keep_file_open:
            await self.sync() # Ensure buffer is flushed if file was open
            if self.file is not None:
                try:
                    self.file.close()
                    await uasyncio.sleep_ms(0) # Yield after close
                except Exception as e:
                    await p.print("Error in close (keep_open=True): {}".format(e)) # await p.print
                self.file = None
        else: # not keep_file_open
            self.file = None # Should already be None

    async def clear_logs(self):
        # This method is highly blocking and not easily made async without async os calls.
        # For now, it remains largely synchronous. Consider if it's called from async context.
        await p.print("clear_logs is a blocking operation and not fully async.") # await p.print
        file_was_managed_open = self.file is not None and self.keep_file_open
        
        if file_was_managed_open:
            try:
                self.file.close()
                await uasyncio.sleep_ms(0)
            except Exception as e:
                await p.print("Error closing file in clear_logs: {}".format(e)) # await p.print
            self.file = None

        if self.filename and self._path_exists(self.filename):
            try:
                os.unlink(self.filename)
                await uasyncio.sleep_ms(0) 
                await p.print("Log file {} unlinked.".format(self.filename)) # await p.print
            except Exception as e:
                await p.print("Error unlinking file {} in clear_logs: {}".format(self.filename, e)) # await p.print
        
        self.file_rows = 0
        self.cursor_position = 0
        self.cursor_position_last = 0
        # For keep_file_open=True, new_file or _log will handle re-creating/re-opening.
        # For keep_file_open=False, _log will create on next write.

    # def print_lines(self, path=None):
    #     try:
    #         if path is None:
    #             self.sync()
    #         with open(path or self.filename, "r") as file:
    #             for line in file:
    #                 await p.print(line.strip())  # Print each line
    #     except OSError:
    #         await p.print("Error: Cannot read JSON log file.")

    async def _print_last_line(self, path=None):
        try:
            file_to_read = path or self.filename
            if not file_to_read:
                await p.print("Error in _print_last_line: No file specified or set.") # await p.print
                return

            if path is None and self.keep_file_open:
                await self.sync()
            
            # For external files or when not using keep_file_open, rely on _print_last_lines
            if path or not self.keep_file_open:
                await self._print_last_lines(N=1, path=file_to_read)
                return

            # This part is for internal file with cursor position (keep_file_open=True)
            with open(file_to_read, "r") as file:
                await uasyncio.sleep_ms(0) # Yield after open
                file.seek(self.cursor_position_last)
                for line in file:
                    await p.print(line.strip()) # await p.print
                    await uasyncio.sleep_ms(0) # Yield after processing line
        except OSError as e:
            await p.print("Error reading log file in _print_last_line ({}): {}".format(file_to_read if 'file_to_read' in locals() else 'N/A', e)) # await p.print
        except Exception as e_gen:
            await p.print("Generic error in _print_last_line: {}".format(e_gen)) # await p.print

    async def _print_last_lines(self, N=10, path=None):
        try:
            file_to_read = path or self.filename
            if not file_to_read:
                await p.print("Error in _print_last_lines: No file specified or set.") # await p.print
                return

            if path is None and self.keep_file_open: # sync only if we were keeping file open
                await self.sync()

            with open(file_to_read, "r") as file: # BLOCKING
                await uasyncio.sleep_ms(0) # Yield after open
                lines_to_skip = max(0, self.file_rows - N)
                for _ in range(lines_to_skip):
                    if not file.readline(): break # EOF
                    await uasyncio.sleep_ms(0) # Yield
                while True:
                    line = file.readline()
                    if not line:
                        break
                    await p.print(line.strip()) # await p.print
                    await uasyncio.sleep_ms(0) # Yield
        except OSError as e:
            await p.print("Error reading log file in _print_last_lines ({}): {}".format(file_to_read if 'file_to_read' in locals() else 'N/A', e)) # await p.print
        except Exception as e_gen:
            await p.print("Generic error in _print_last_lines: {}".format(e_gen)) # await p.print
            
    def print_last_lines(self, N=1):
        while self.request_print_last_lines:
            pass
        self.request_print_last_lines = N

    async def rename_current_file(self, new_name_suffix):
        new_full_path = "{}/{}".format(self.parent_dir, new_name_suffix)
        
        if self.keep_file_open and self.file is not None:
            await self.sync()
            try:
                self.file.close()
                await uasyncio.sleep_ms(0)
            except Exception as e:
                await p.print("Error closing file in rename_current_file (keep_open=True): {}".format(e)) # await p.print
            self.file = None 
        
        if self.filename and self._path_exists(self.filename):
            try:
                os.rename(self.filename, new_full_path)
                await uasyncio.sleep_ms(0)
                await p.print("Renamed {} to {}".format(self.filename, new_full_path)) # await p.print
            except Exception as e:
                await p.print("Error renaming {} to {}: {}".format(self.filename, new_full_path, e)) # await p.print
        else:
            await p.print("Old filename {} does not exist for renaming.".format(self.filename)) # await p.print

        self.filename = new_full_path 
        
        if self.keep_file_open:
            try:
                self.file = open(self.filename, "a") # Reopen in append mode
                await uasyncio.sleep_ms(0)
            except Exception as e:
                await p.print("Error reopening renamed file {} (keep_open=True): {}".format(self.filename, e)) # await p.print
                self.file = None
        self.file_rows = 0 
        self.cursor_position = 0
        self.cursor_position_last = 0

    async def rename_current_filename(self, new_full_path):
        if self.keep_file_open and self.file is not None:
            await self.sync()
            try:
                self.file.close()
                await uasyncio.sleep_ms(0)
            except Exception as e:
                await p.print("Error closing file in rename_current_filename (keep_open=True): {}".format(e)) # await p.print
            self.file = None
            
        if self.filename and self._path_exists(self.filename) and self.filename != new_full_path:
            try:
                os.rename(self.filename, new_full_path)
                await uasyncio.sleep_ms(0)
                await p.print("Renamed {} to {}".format(self.filename, new_full_path)) # await p.print
            except Exception as e:
                await p.print("Error renaming {} to {}: {}".format(self.filename, new_full_path, e)) # await p.print
        elif self.filename == new_full_path:
            await p.print("Target filename {} is same as current; no rename needed.".format(new_full_path)) # await p.print
        else:
             await p.print("Old filename {} does not exist for renaming or no rename needed.".format(self.filename)) # await p.print

        self.filename = new_full_path
        
        if self.keep_file_open:
            try:
                self.file = open(self.filename, "a") # Reopen in append mode
                await uasyncio.sleep_ms(0)
            except Exception as e:
                await p.print("Error reopening renamed file {} (keep_open=True): {}".format(self.filename, e)) # await p.print
                self.file = None
        self.file_rows = 0
        self.cursor_position = 0
        self.cursor_position_last = 0

    def request_new_file(self):
        self._requestNewFile = True

    def request_rename_file(self):
        self._requestRenameFile = True
    
    async def wait_for_end_process_log_queue(self):
        while self.log_queue:
            await uasyncio.sleep_ms(10)

    async def writer_main_loop(self):
        while self.run:
            while await self._process_log_queue():
                await uasyncio.sleep_ms(0)
            await uasyncio.sleep_ms(self.writer_main_loop_yield_ms)


    async def machine(self):
        if self._requestNewFile or (not self.filename):
            self._requestNewFile = False
            await self.wait_for_end_process_log_queue()
            await self.sync()
            await self.new_file()
        if self._requestRenameFile:
            self._requestRenameFile = False
            await self.wait_for_end_process_log_queue()
            await self.sync()
            new_path = await self.get_new_file_path()
            await self.rename_current_filename(new_path)
        if not self.keep_file_open:
            # Ensure filename is valid before attempting to open
            if not self.filename or not self._path_exists(self.parent_dir): # Check parent dir too
                await p.print("Error in machine (keep_file_open=False): Invalid filename or directory. Attempting to create new file.") # await p.print
                await self.new_file() # This sets self.filename and ensures dir
                if not self.filename:
                    await p.print("Critical Error in machine (keep_file_open=False): Could not establish a valid log file. Skipping log cycle.") # await p.print
                    return
            try:
                self.file = open(self.filename, "a") # BLOCKING
                await uasyncio.sleep_ms(0) # YIELD after open
            except Exception as e:
                await p.print("Error opening log file {} in machine (keep_file_open=False): {}".format(self.filename, e)) # await p.print
                self.file = None # Ensure file is None so subsequent operations don't fail badly
                # Consider requesting a new file or logging an error and returning
                # await self.request_new_file() # This might be too aggressive, could loop if disk is full

        # if self.file: # Only process queue if file is open
        #     while await self._process_log_queue(): # Process one item from buffer
        #         await uasyncio.sleep_ms(0) # Yield after each item processed
        
        if self.keep_file_open:
            await self.sync_process() # Sync file to card periodically
        else:
            if self.file: # Only flush/close if file was successfully opened
                try:
                    self.file.flush() # BLOCKING
                    await uasyncio.sleep_ms(0) # YIELD after flush
                    self.file.close() # BLOCKING
                    await uasyncio.sleep_ms(0) # YIELD after close
                except Exception as e:
                    await p.print("Error flushing/closing log file {} (keep_file_open=False): {}".format(self.filename, e)) # await p.print
                finally:
                    self.file = None # Ensure self.file is None even if close fails
            
        if self.request_print_last_lines:
            await self._print_last_lines(self.request_print_last_lines)
            self.request_print_last_lines = 0
            


        await uasyncio.sleep_ms(0) # Yield is already in sync_process if sync happens


# cmndavrg = AFECommandAverage()
AFE_Config = [
    {
        "afe_id": 35,
        "afe_uid": "",
        "channel": [
            SensorChannel(0),
            SensorChannel(1),
            SensorChannel(2),
            SensorChannel(3),
            SensorChannel(4),
            SensorChannel(5),
            SensorChannel(6),
            SensorChannel(7),
        ],
    }
]


def callibration_reader_csv(csv_file):
    def convert_value(key, value):
        try:
            if key == 'ID':
                return int(value)
            elif key in ['SN_AFE', 'SN_SiPM', 'M/S']:
                # Skip these keys (keep them as strings)
                return value
            return float(value)
        except ValueError:
            try:
                if value == '':
                    return ''
                return value
            except:
                return value

    with open(csv_file, mode='r', encoding='utf-8') as file:
        lines = file.readlines()

    headers = lines[0].strip().split(',')

    rows = [
        {key: convert_value(key, value)
         for key, value in zip(headers, line.strip().split(','))}
        for line in lines[1:]
    ]

    return rows


def read_callibration_csv(file):
    callib_data = callibration_reader_csv(file)
    callib_data_mean = {}
    uniq_id = []
    groups = ['M', 'S']
    # Collect values for mean calculation and get unique ID
    for c in callib_data:
        g = c['M/S']
        if g not in callib_data_mean:
            callib_data_mean[g] = {}
        for k, v in c.items():
            if k == 'ID':
                if v not in uniq_id:
                    uniq_id.append(v)
                continue
            if isinstance(v, (float, int)):
                if k not in callib_data_mean[g]:
                    callib_data_mean[g][k] = []
                callib_data_mean[g][k].append(v)

    # Compute the mean for each key
    for g in groups:
        if g in callib_data_mean:
            for k in callib_data_mean[g]:
                values = callib_data_mean[g][k]
                callib_data_mean[g][k] = sum(
                    values) / len(values) if values else None
            callib_data_mean[g]['ID'] = 0  # append ID as default
            callib_data_mean[g]['M/S'] = g

    return callib_data, callib_data_mean


# if __name__ == "__main__":
#     import sys
#     callibration_data_file_csv = sys.argv[1]
#     TempLoop_file_csv = sys.argv[2]

#     callib_data, callib_data_mean = read_callibration_csv(
#         callibration_data_file_csv)
#     TempLoop_data, TempLoop_data_mean = read_callibration_csv(
#         TempLoop_file_csv)

#     await p.print("Callibration data:\n", json.dumps(callib_data, indent=4)) # await p.print
#     await p.print("TempLoop data:\n", json.dumps(TempLoop_data, indent=4)) # await p.print

#     await p.print("Callibration data mean:\n", # await p.print
#             json.dumps(callib_data_mean, indent=4))
#     await p.print("TempLoop data mean:\n", json.dumps(TempLoop_data_mean, indent=4)) # await p.print
