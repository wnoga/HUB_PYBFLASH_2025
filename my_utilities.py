try:
    import pyb
except:
    pass
import json
import os

try:
    import _thread
    import micropython
    import machine
    import uasyncio # Added for async operations
    import gc
    # # Create a lock for safe printing
    print_lock = _thread.allocate_lock()
    rtc = machine.RTC()
    # lock = _thread.allocate_lock()
except:
    pass
import time
rtc_synced = False

def lock(blocking=True, sleep_ms=1):
    return
    if blocking:
        while not print_lock.acquire():
            # time.sleep_us(sleep_us)
            time.sleep_ms(sleep_ms)
    else:
        print_lock.acquire()


def unlock():
    return
    if print_lock.locked():
        print_lock.release()


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

    def print(self, *args, **kwargs):
        lock()
    # with print_lock:
    # print(*args, **kwargs)
        self.queue.append((args, kwargs))
        if len(self.queue) > 50:
            self.queue.pop(0)
        unlock()

    async def process_queue(self):
        if not self.queue:
            return
        lock()
        item = self.queue.pop(0)
        unlock()
        print(*(item[0]), **item[1])
        await uasyncio.sleep_ms(0) # Yield to the event loop after printing one item
 

p = PrintButLouder()
# P = PrintButLouder()


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


class AFECommand:
    getSerialNumber = 0x0
    getVersion = 0x1
    resetAll = 0x3
    startADC = 0x4
    getTimestamp = 0x5
    getSyncTimestamp = 0x6
    getSensorDataSi_last_byMask = 0x30
    getSensorDataSi_average_byMask = 0x31
    getSensorDataSiAndTimestamp_average_byMask = 0x3b
    AFECommand_getSensorDataSi_periodic = 0x3f
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
    return pyb.millis()


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
    def __init__(self, filename="log.json", parent_dir="/sd/logs", verbosity_level=VerbosityLevel["INFO"]):
        self.parent_dir = parent_dir
        self.verbosity_level = verbosity_level
        self.filename = filename
        self.filename_org = filename
        self.file = None
        self.print_verbosity_level = VerbosityLevel["CRITICAL"]
        self.last_sync = 0
        self.sync_every_ms = 1000
        self.buffer = []
        self.burst_delay_ms = 0
        self.burst_timestamp_ms = 0
        self._requestNewFile = False
        self._requestRenameFile = False
        self.file_rows = 0
        self.cursor_position = 0
        self.cursor_position_last = 0
        self.rtc_synced = False
        self.file = None # Initialize file to None, will be opened on first log or machine call

    def _ensure_directory(self):
        if not self._path_exists(self.parent_dir):
            os.mkdir(self.parent_dir)

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
        try:
            if self.file is None:
                pass
            else:
                await self.sync()
                self.file.close()
                await uasyncio.sleep_ms(0) # Yield after close
        except:
            pass
        self.filename = await self.get_new_file_path()
        try:
            # Keep JSON log file open for appending
            self.file = open(self.filename, "w")
            await uasyncio.sleep_ms(0) # Yield after open
        except Exception as e:
            print("ERROR new_file", e)
        self.cursor_position = 0
        self.cursor_position_last = 0
        self.file_rows = 0
        p.print("New logger file", self.filename)
        await uasyncio.sleep_ms(0) # Yield

    async def _log(self, level: int, message):
        if self.burst_delay_ms:
            if (millis() - self.burst_timestamp_ms) < self.burst_delay_ms:
                return 0  # Zero item saved
        self.burst_timestamp_ms = millis()
        if self.file is None:
            await self.new_file()
        log_timestamp = millis()
        log_entry = {"timestamp": log_timestamp, "rtc_timestamp": rtc_unix_timestamp(), "level": level, "message": message}
        try:
            self.cursor_position_last = self.cursor_position
            toLog = json.dumps(log_entry)
            # await uasyncio.get_event_loop().run_in_executor(None, self.file.write, str(toLog) + "\n") # Append log as a new line
            self.file.write(str(toLog) + "\n")
            await uasyncio.sleep_ms(0) # Yield after write
            self.file_rows += 1
            self.cursor_position = self.file.tell()
        except Exception as e:
            print("ERROR in _log: {} -> {}".format(e, log_entry))
            self.request_new_file()
            self.new_file()
            return -1 # skip this row
        if level >= self.print_verbosity_level:
            p.print("LOG:", toLog)
        return 1  # One item saved

    async def process_log(self, _):
        if len(self.buffer) == 0:
            return False
        
        toLog = self.buffer[0]
        log_level = toLog[0]
        log_message = toLog[1]
        
        log_result = await self._log(log_level, log_message)
        
        if log_result == 1:  # Successfully written
            lock()
            self.buffer.pop(0)
            unlock()
            return True  # Item processed
        return False # Item not processed (burst delay or error)

    def log(self, level: int, message):
        if self._should_log(level):
            self.buffer.append([level, message])

    async def sync(self):
        if self.file is not None:
            try:
                self.file.flush()
                await uasyncio.sleep_ms(0) # Yield after flush
                self.last_sync = millis()
            except:
                pass

    async def sync_process(self):
        if (millis() - self.last_sync) > self.sync_every_ms:
            await self.sync()
            gc.collect()
            await uasyncio.sleep_ms(0) # Yield after gc.collect if it's potentially long

    async def close(self):
        await self.sync()
        if self.file:
            self.file.close()
            await uasyncio.sleep_ms(0) # Yield after close
        self.file = None

    def clear_logs(self):
        # This method is highly blocking and not easily made async without async os calls.
        # For now, it remains largely synchronous. Consider if it's called from async context.
        p.print("clear_logs is a blocking operation and not fully async.")
        if self.file:
            self.file.close()
            if self.filename and self._path_exists(self.filename):
                os.unlink(self.filename)
            # Re-opening should be handled by new_file or _log when needed
        self.file = None 

    def print_lines(self, path=None):
        try:
            if path is None:
                self.sync()
            with open(path or self.filename, "r") as file:
                for line in file:
                    p.print(line.strip())  # Print each line
        except OSError:
            p.print("Error: Cannot read JSON log file.")

    def print_last_line(self, path=None):
        try:
            if path is None:
                self.sync()
            else:
                # use print_last_lines for external files
                self.print_last_lines(N=1, path=path)
                return
            with open(self.filename, "r") as file:  # for internal file use cursor position
                file.seek(self.cursor_position_last)
                for line in file:
                    p.print(line.strip())
        except OSError:
            p.print("Error: Cannot read JSON log file.")

    def print_last_lines(self, N=10, path=None):
        try:
            if path is None:
                self.sync()
            with open(path or self.filename, "r") as file:
                for _ in range(self.file_rows - N):
                    file.readline()
                while True:
                    line = file.readline()
                    if not line:
                        break
                    p.print(line.strip())
        except OSError:
            p.print("Error: Cannot read JSON log file.")

    async def rename_current_file(self, new_name):
        if self.file is not None:
            await self.sync()
            self.file.close()
            await uasyncio.sleep_ms(0)
            os.rename(self.filename, "{}/{}".format(self.parent_dir, new_name))
            await uasyncio.sleep_ms(0)
            self.filename = "{}/{}".format(self.parent_dir, new_name)
            self.file = open(self.filename, "a")  # Reopen as empty file
            await uasyncio.sleep_ms(0)

    async def rename_current_filename(self, path):
        if self.file is not None:
            await self.sync()
            self.file.close()
            await uasyncio.sleep_ms(0)
            # print("Renaming {} to {}",self.filename, path)
            os.rename(self.filename, path)
            await uasyncio.sleep_ms(0)
            self.filename = path
            self.file = open(self.filename, "a")  # Reopen as empty file
            await uasyncio.sleep_ms(0)

    def request_new_file(self):
        self._requestNewFile = True

    def request_rename_file(self):
        self._requestRenameFile = True

    async def machine(self):
        if self._requestNewFile == True:
            self._requestNewFile = False
            while await self.process_log(0):
                await uasyncio.sleep_ms(0)
            await self.sync()
            await self.new_file()
        if self._requestRenameFile == True:
            self._requestRenameFile = False
            while await self.process_log(0):
                await uasyncio.sleep_ms(0)
                pass
            await self.sync()
            new_path = await self.get_new_file_path()
            await self.rename_current_filename(new_path)

        while await self.process_log(0): # Process one item from buffer
            await uasyncio.sleep_ms(0)

        await self.sync_process() # Sync file to card periodically
        # await uasyncio.sleep_ms(0) # Yield is already in sync_process if sync happens


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


if __name__ == "__main__":
    import sys
    callibration_data_file_csv = sys.argv[1]
    TempLoop_file_csv = sys.argv[2]

    callib_data, callib_data_mean = read_callibration_csv(
        callibration_data_file_csv)
    TempLoop_data, TempLoop_data_mean = read_callibration_csv(
        TempLoop_file_csv)

    p.print("Callibration data:\n", json.dumps(callib_data, indent=4))
    p.print("TempLoop data:\n", json.dumps(TempLoop_data, indent=4))

    p.print("Callibration data mean:\n",
            json.dumps(callib_data_mean, indent=4))
    p.print("TempLoop data mean:\n", json.dumps(TempLoop_data_mean, indent=4))
