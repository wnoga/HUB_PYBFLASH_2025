import pyb
import json
import os

class AFECommand:
    getSerialNumber = 0x0
    getVersion = 0x1
    resetAll = 0x3
    getSensorDataSi_last = 0x30
    getSensorDataSi_average = 0x31
    getSensorDataSi_all_last = 0x32
    getSensorDataSi_all_average = 0x33
    setSensorDataSi_all_periodic_average = 0x34
    getSensorDataSiAndTimestamp_average = 0x3b
    getSensorDataSi_all_periodic_average = 0x3f
    setSensorDataSi_periodic_last = 0x40
    setSensorDataSiAndTimestamp_periodic_last = 0x41
    setSensorDataSi_periodic_average = 0x42
    setSensorDataSiAndTimestamp_periodic_average = 0x43
    transmitSPIData = 0xa0
    setAD8402Value_byte = 0xa1
    writeGPIO = 0xa2
    setTemperatureLoopForChannelState_bySubdevice = 0xc0
    setTemperatureLoopForChannelState_byMask = 0xc1
    setDACValueRaw_bySubdevice = 0xc2
    setDACValueSi_bySubdevice = 0xc3
    stopTemperatureLoopForAllChannels = 0xc4
    setDAC_bySubdevice = 0xc5
    setDACRampOneBytePerMillisecond_ms = 0xc6
    setAveragingMode = 0xd0
    setAveragingAlpha = 0xd1
    setAveragingBufferSize = 0xd2
    setChannel_dt_ms = 0xd3
    setAveraging_max_dt_ms = 0xd4
    setChannel_multiplicator = 0xd5
    setAveragingSubdevice = 0xd6
    setChannel_a = 0xd7
    setChannel_b = 0xd8
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


class AFECommandGPIO:
    def __init__(self):
        self.port = {
            "A":0,"PORTA":0,
            "B":1,"PORTB":1,
            "C":2,"PORTC":2
        }
        self.EN_HV0_Pin = 10
        self.EN_HV0_Port = self.port["B"]
        self.EN_HV1_Pin = 11
        self.EN_HV1_Port = self.port["B"]
        self.blink_Pin = 9
        self.blink_Port = self.port["A"]

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
        self.averaging_mode = kwargs.get("averaging_mode", None)
        self.latest_reading = SensorReading()
        
        # Measurement download settings
        self.periodic_interval_ms = kwargs.get("periodic_interval_ms", 0)
        self.periodic_sending_is_enabled = False


class CSVLogger:
    def __init__(self, filename):
        self.filename = filename
        self.file = open(self.filename, "a")
        self.headers = []
        self._ensure_headers()
    
    def _ensure_headers(self):
        if os.stat(self.filename)[0] == 0:
            self.headers = ["log_timestamp", "measurement_timestamp", "level", "message"]
            self._write_row(self.headers)
    
    def _write_row(self, row):
        self.file.write(",".join(map(str, row)) + "\n")
        self.file.flush()
    
    def update_headers(self, new_entry):
        new_keys = [key for key in new_entry.keys() if key not in self.headers]
        if new_keys:
            self.headers.extend(new_keys)
            temp_filename = self.filename + ".tmp"
            with open(temp_filename, "w") as temp_file:
                temp_file.write(",".join(self.headers) + "\n")
                with open(self.filename, "r") as old_file:
                    old_lines = old_file.readlines()[1:]
                for line in old_lines:
                    temp_file.write(line.strip() + "," + ",".join(["" for _ in new_keys]) + "\n")
            os.rename(temp_filename, self.filename)
            self.file = open(self.filename, "a")
    
    def log(self, entry):
        self.update_headers(entry)
        self._write_row([entry.get(header, "") for header in self.headers])
    
    def close(self):
        self.file.close()
        
    def print_lines(self):
        try:
            with open(self.filename, "r") as file:
                for line in file:
                    print(line.strip())  # Print each line
        except OSError:
            print("Error: Cannot read JSON log file.")
            
class EmptyLogger:
    def __init__(self,**kwargs):
        pass
    def log(self,**kwargs):
        pass
    def log(self, level, message):
        pass
    def sync(self):
        pass
    def close(self):
        pass
    def read_logs(self):
        pass
    def clear_logs(self):
        pass
    def print_lines(self):
        pass
    
class JSONLogger:
    def __init__(self, filename="log.json", parent_dir="/sd/logs", verbosity_level="INFO", csv_filename="measurements.csv"):
        self.parent_dir = parent_dir
        self.verbosity_level = verbosity_level.upper()
        self._ensure_directory()
        self.filename = self._get_unique_filename("{}/{}".format(self.parent_dir, filename))
        self.csv_logger = CSVLogger(self._get_unique_filename("{}/{}".format(self.parent_dir, csv_filename)))
        self.file = open(self.filename, "a")  # Keep JSON log file open for appending
        self.levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "MEASUREMENT"]
        
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
        return self.levels.index(level.upper()) >= self.levels.index(self.verbosity_level)
    
    def log(self, level, message):
        if self._should_log(level):
            log_timestamp = millis()
            log_entry = {"timestamp": log_timestamp, "level": level.upper(), "message": message}
            self.file.write(json.dumps(log_entry) + "\n")  # Append log as a new line
            self.file.flush()  # Ensure data is written immediately
            print("LOG:",log_entry)
            if self.levels.index(level.upper()) >= self.levels.index("MEASUREMENT"):
                self.csv_logger.log(message)
    
    def sync(self):
        self.file.flush()
        os.sync()
    
    def close(self):
        self.file.close()
        self.csv_logger.close()
    
    def read_logs(self):
        self.file.close()  # Close before reading
        logs = []
        try:
            with open(self.filename, "r") as file:
                for line in file:
                    logs.append(json.loads(line))
        except (OSError, ValueError):
            pass
        self.file = open(self.filename, "a")  # Reopen file for appending
        return logs
    
    def clear_logs(self):
        self.file.close()
        self.csv_logger.close()
        os.unlink(self.filename)  # Remove JSON file
        os.unlink(self.csv_logger.filename)  # Remove CSV file
        self.file = open(self.filename, "w")  # Reopen as empty file
        self.csv_logger = CSVLogger(self.csv_logger.filename)
        
    def print_lines(self):
        try:
            with open(self.filename, "r") as file:
                for line in file:
                    print(line.strip())  # Print each line
        except OSError:
            print("Error: Cannot read JSON log file.")


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
            SensorChannel(6, time_interval_ms=1000, a=0.08057, b=6, averaging_mode=1),
            SensorChannel(7, time_interval_ms=1000, a=0.08057, b=6, averaging_mode=1),
        ],
    }
]
