try:
    import pyb
except:
    pass
import json
import os

try:
    import _thread
    # lock = _thread.allocate_lock()
except:
    pass

# Create a lock for safe printing
print_lock = _thread.allocate_lock()

class Print:
    def __init__(self):
        pass
    def print(self, *args, **kwargs):
        return
        with print_lock:
            print(*args, **kwargs)
    def debug(self, *args, **kwargs):
        with print_lock:
            print(*args, **kwargs)
            
class PrintButLouder:
    def __init__(self):
        pass
    def print(self, *args, **kwargs):
        with print_lock:
            print(*args, **kwargs)

p = PrintButLouder()
P = PrintButLouder()


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
    wdt = wdt_x()
    # from machine import WDT
    # wdt = WDT(timeout=10000)  # enable it with a timeout of 5s
    # wdt.feed()

except:
    pass

class AFECommand:
    getSerialNumber = 0x0
    getVersion = 0x1
    resetAll = 0x3
    startADC = 0x4
    getTimestamp = 0x5
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

class AFECommandSubdevice:
    AFECommandSubdevice_master = 0x1
    AFECommandSubdevice_slave = 0x2
    AFECommandSubdevice_both = 0x3

class AFECommandAverage:
    NONE = 0x00
    STANDARD = 0x01
    EXPONENTIAL = 0x02
    MEDIAN = 0x03
    RMS = 0x04
    HARMONIC = 0x05
    GEOMETRIC = 0x06
    TRIMMED = 0x07
    WEIGHTED_EXPONENTIAL = 0x08
    ARIMA = 0x09


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
    "DEBUG":4,
    "INFO":3,
    "WARNING":2,
    "ERROR":1,
    "CRITICAL":0,
    "MEASUREMENT":-1
}

class CommandStatus:
    NONE = 0x000
    IDLE = 0x001
    RECIEVED = 0x010
    ERROR = 0x100

class AFECommandGPIO:
    def __init__(self,pin=None,port=None):
        self.port = {
            None:None,
            "A":0,"PORTA":0,
            "B":1,"PORTB":1,
            "C":2,"PORTC":2
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

# Measurement structure
class SensorReading:
    def __init__(self, timestamp_ms=0, value=0.0):
        self.timestamp_ms = timestamp_ms
        self.value = None
    def __str__(self):
        return "{{\"timestamp_ms\":{},\"value\":{}}}".format(self.timestamp_ms, self.value)

# Channel structure
class SensorChannel:
    def __init__(self, channel_id,**kwargs):
        self.channel_id = channel_id
        
        # Use .get() to avoid KeyError if a key is missing
        self.time_interval_ms = kwargs.get("time_interval_ms", 0)
        self.alpha = kwargs.get("alpha", 0)
        self.multiplicator = kwargs.get("multiplicator", 1)
        self.a = kwargs.get("a", 0)
        self.b = kwargs.get("b", 0)
        averagingmodes = AFECommandAverage()
        self.averaging_mode = kwargs.get("averaging_mode", averagingmodes.NONE)
        self.latest_reading = SensorReading()
        
        # Measurement download settings
        self.periodic_interval_ms = kwargs.get("periodic_interval_ms", 0)
        self.periodic_sending_is_enabled = False
   
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
        self.burst_delay_ms = 10
        self.burst_timestamp_ms = 0
        self.new_file()
        
    def _ensure_directory(self):
        if not self._path_exists(self.parent_dir):
            os.mkdir(self.parent_dir)
    
    def _get_unique_filename(self, filename):
        base, ext = filename.rsplit(".", 1) if "." in filename else (filename, "")
        counter = 1
        while self._path_exists(filename):
            filename = "{}_{}{}".format(base, counter, "." + ext if ext else "")
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
    
    def new_file(self):
        try:
            if self.file is None:
                pass
            else:
                self.sync()
                self.file.close()
        except:
            pass
        self._ensure_directory()
        self.filename = self._get_unique_filename("{}/{}".format(self.parent_dir, self.filename_org))
        self.file = open(self.filename, "w")  # Keep JSON log file open for appending
        p.print("New logger file", self.filename)
    
    def _log(self, level: int, message):
        if (millis() - self.burst_timestamp_ms) < self.burst_delay_ms:
            return
        self.burst_timestamp_ms = millis()
        if self.file is None:
            self.new_file()
        # if self._should_log(level):
        # print(message)
        log_timestamp = millis()
        # try:
        log_entry = {"timestamp": log_timestamp, "level": level, "message": message}
        try:
            toLog = json.dumps(log_entry)
            self.file.write(str(toLog) + "\n")  # Append log as a new line
        except Exception as e:
            print("ERROR in _log: {} -> {}".format(e,log_entry))
            self.new_file()
            self._log(level, message)
            return
        
        # self.file.flush()  # Ensure data is written immediately
        if level >= self.print_verbosity_level:
            p.print("LOG:",toLog)
        # except Exception as e:
        #     # p.print("ERROR log: {}  @ {} -> {}".format(e,log_timestamp,message))
        #     p.print("ERROR LOG",e,log_entry)
    def process_log(self, _):
        if len(self.buffer) == 0:
            return None
        toLog = self.buffer[0].copy()
        # print(toLog)
        self.buffer.pop(0)
        self._log(toLog[0],toLog[1])
        return True
    
    def log(self, level: int, message):
        if self._should_log(level):
            self.buffer.append([level,message])
        # print("TRY LOG",message)
        # self._log(level,message)
        # self.process_log(None)
        
    def sync(self):
        if self.file is not None:
            self.file.flush()
            self.last_sync = millis()
        
    def sync_process(self):
        if (millis() - self.last_sync) > self.sync_every_ms:
            self.sync()
    
    def close(self):
        self.sync()
        self.file.close()
        self.file = None
    
    def read_logs(self, path=None):
        # self.file.close()  # Close before reading
        logs = []
        try:
            with open(path or self.filename, "r") as file:
                for line in file:
                    logs.append(json.loads(line))
        except (OSError, ValueError):
            pass
        # self.file = open(self.filename, "a")  # Reopen file for appending
        return logs
    
    def clear_logs(self):
        self.file.close()
        os.unlink(self.filename)  # Remove JSON file
        self.file = open(self.filename, "w")  # Reopen as empty file
        
    def print_lines(self, path=None):
        try:
            with open(path or self.filename, "r") as file:
                for line in file:
                    p.print(line.strip())  # Print each line
        except OSError:
            p.print("Error: Cannot read JSON log file.")
    
    
    

cmndavrg = AFECommandAverage()
AFE_Config = [
    {
        "afe_id": 35,
        "afe_uid": "",
        "channel": [
            SensorChannel(0, time_interval_ms=1000),
            SensorChannel(1, time_interval_ms=1000),
            SensorChannel(2, time_interval_ms=1000),
            SensorChannel(3, time_interval_ms=1000),
            SensorChannel(4, time_interval_ms=1000),
            SensorChannel(5, time_interval_ms=1000),
            SensorChannel(6, time_interval_ms=1000, a=0.08057, b=6, alpha=1.0/1000, averaging_mode=cmndavrg.WEIGHTED_EXPONENTIAL),
            SensorChannel(7, time_interval_ms=1000, a=0.08057, b=6, alpha=1.0/1000, averaging_mode=cmndavrg.WEIGHTED_EXPONENTIAL),
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
            return value

    with open(csv_file, mode='r', encoding='utf-8') as file:
        lines = file.readlines()

    headers = lines[0].strip().split(',')
    
    rows = [
        {key: convert_value(key, value) for key, value in zip(headers, line.strip().split(','))}
        for line in lines[1:]
    ]
    
    return rows
def read_callibration_csv(file): 
    callib_data = callibration_reader_csv(file)
    callib_data_mean = {}
    uniq_id = []
    groups = ['M','S']
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
            if isinstance(v,(float,int)):
                if k not in callib_data_mean[g]:
                    callib_data_mean[g][k] = []
                callib_data_mean[g][k].append(v)
                
    # Compute the mean for each key
    for g in groups:
        if g in callib_data_mean:
            for k in callib_data_mean[g]:
                values = callib_data_mean[g][k]
                callib_data_mean[g][k] = sum(values) / len(values) if values else None
            callib_data_mean[g]['ID'] = 0 # append ID as default
            callib_data_mean[g]['M/S'] = g
    
    return callib_data, callib_data_mean


if __name__ == "__main__":
    import sys
    callibration_data_file_csv = sys.argv[1]
    TempLoop_file_csv = sys.argv[2]
    
    callib_data, callib_data_mean = read_callibration_csv(callibration_data_file_csv)
    TempLoop_data, TempLoop_data_mean = read_callibration_csv(TempLoop_file_csv)

    p.print("Callibration data:\n",json.dumps(callib_data, indent=4))
    p.print("TempLoop data:\n",json.dumps(TempLoop_data, indent=4))

    
    p.print("Callibration data mean:\n",json.dumps(callib_data_mean, indent=4))
    p.print("TempLoop data mean:\n",json.dumps(TempLoop_data_mean, indent=4))
