# my_utilities.py
import json
import os
import sys
import time

rtc_synced = False

VerbosityLevel = {
    "DEBUG": 4,
    "INFO": 3,
    "WARNING": 2,
    "ERROR": 1,
    "CRITICAL": 0,
    "MEASUREMENT": -1
}

# --- Hardware and Platform Compatibility Fallbacks ---
try:
    import machine
    rtc = machine.RTC()
except (ImportError, AttributeError):
    class DummyRTC:
        def datetime(self):
            return (2000, 1, 1, 1, 0, 0, 0, 0)
    rtc = DummyRTC()

try:
    from machine import WDT
    wdt = WDT(timeout=20000)
    wdt.feed()
except (ImportError, AttributeError):
    class DummyWDT:
        def feed(self):
            pass
    wdt = DummyWDT()

try:
    import uasyncio
except ImportError:
    import asyncio

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
            return self

        def create_task(self, coro):
            return self.get_event_loop().create_task(coro)

        async def sleep(self, t_s):
            await asyncio.sleep(t_s)

        async def sleep_ms(self, t_ms):
            await asyncio.sleep(t_ms / 1000.0)

        async def gather(self, *aws, return_exceptions=False):
            return await asyncio.gather(*aws, return_exceptions=return_exceptions)

        def run(self, coro):
            return asyncio.run(coro)

    uasyncio = UasyncioShim()


# --- Timing Helpers ---
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

def is_timeout(timestamp_ms, timeout_ms):
    if timeout_ms == 0:
        return False
    return time.ticks_diff(time.ticks_ms(), timestamp_ms) > timeout_ms


def is_delay(timestamp_ms, delay_ms):
    if delay_ms == 0:
        return False
    return time.ticks_diff(time.ticks_ms(), timestamp_ms) < delay_ms


# --- RTC Utilities ---
def rtc_unix_timestamp():
    """Converts MicroPython RTC datetime tuple into a standard Unix timestamp (seconds since 1970)."""
    dt = rtc.datetime()  # (year, month, day, weekday, hours, minutes, seconds, subseconds)
    tm = (dt[0], dt[1], dt[2], dt[4], dt[5], dt[6], dt[3], 0)
    return time.mktime(tm) + 946684800  # Offset between MicroPython epoch (2000) and standard epoch (1970)


def rtc_datetime_pretty():
    dt = rtc.datetime()
    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
        dt[0], dt[1], dt[2], dt[4], dt[5], dt[6]
    )


# --- Non-blocking Async Logger ---
class PrintButLouder:
    """Non-blocking queue-backed logger to prevent serialization I/O from stalling async tasks."""
    def __init__(self, max_queue_size=50):
        self.queue = []
        self.max_queue_size = max_queue_size

    async def print(self, *args, **kwargs):
        self.queue.append((args, kwargs))
        if len(self.queue) > self.max_queue_size:
            self.queue.pop(0)

    async def machine(self):
        """Flushes buffered messages to console during idle event-loop ticks."""
        if not self.queue:
            return
        
        args, kwargs = self.queue.pop(0)
        print(*args, **kwargs)
        await uasyncio.sleep_ms(0)


p = PrintButLouder()


# --- AFE Command Registers & Enums ---

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

class AFECommand:
    getSerialNumber = 0x00
    getVersion = 0x01
    resetAll = 0x03
    startADC = 0x04
    getTimestamp = 0x05
    getSyncTimestamp = 0x06
    resetCAN = 0x07
    getSubdeviceStatus = 0x08

    getSensorDataSi_last_byMask = 0x30
    getSensorDataSi_average_byMask = 0x31
    getSensorDataBytes_last_byMask = 0x32
    getSensorDataBytes_average_byMask = 0x33
    getSensorDataSi_periodic = 0x3F

    setSensorDataSi_periodic_last = 0x40
    setSensorDataSiAndTimestamp_periodic_last = 0x41
    setSensorDataSi_periodic_average = 0x42
    setSensorDataSiAndTimestamp_periodic_average = 0x43

    transmitSPIData = 0xA0
    setAD8402Value_byte_byMask = 0xA1
    writeGPIO = 0xA2
    setCanMsgBurstDelay_ms = 0xA3
    setAfe_can_watchdog_timeout_ms = 0xA4

    setTemperatureLoop_loop_every_ms = 0xB0

    setTemperatureLoopForChannelState_byMask_asStatus = 0xC1
    setDACValueRaw_bySubdeviceMask = 0xC2
    setDACValueSi_bySubdeviceMask = 0xC3
    stopTemperatureLoopForAllChannels = 0xC4
    setDAC_bySubdeviceMask = 0xC5
    setDACRampOneBytePerMillisecond_ms = 0xC6
    setDACTargetSi_bySubdeviceMask = 0xC7

    setAveragingMode_byMask = 0xD0
    setAveragingAlpha_byMask = 0xD1
    setAveragingBufferSize_byMask = 0xD2
    setChannel_dt_ms_byMask = 0xD3
    setAveraging_max_dt_ms_byMask = 0xD4
    setAveragingSubdevice = 0xD6
    setChannel_a_byMask = 0xD7
    setChannel_b_byMask = 0xD8
    setChannel_period_ms_byMask = 0xD9

    setChannelBufferSize = 0xE0
    setRegulator_ramp_enabled_byMask = 0xE1
    setRegulator_T_opt_byMask = 0xE3
    setRegulator_dT_byMask = 0xE4
    setRegulator_a_dac_byMask = 0xE5
    setRegulator_b_dac_byMask = 0xE6
    setRegulator_dV_dT_byMask = 0xE7
    setRegulator_V_opt_byMask = 0xE8
    setRegulator_V_offset_byMask = 0xE9

    debug_machine_control = 0xF1
    clearRegulator_T_old = 0xF2

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

class CommandStatus:
    NONE = 0x000
    IDLE = 0x001
    RECIEVED = 0x010
    ERROR = 0x100

class AFECommandChannel:
    AFECommandChannel_0 = 0x01
    AFECommandChannel_1 = 0x02
    AFECommandChannel_2 = 0x04
    AFECommandChannel_3 = 0x08
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

e_ADC_CHANNEL = {
    0: "DC_LEVEL_MEAS0",
    1: "DC_LEVEL_MEAS1",
    2: "U_SIPM_MEAS0",
    3: "U_SIPM_MEAS1",
    4: "I_SIPM_MEAS0",
    5: "I_SIPM_MEAS1",
    6: "TEMP_EXT",
    7: "TEMP_LOCAL",
}


class SensorChannel:
    def __init__(self, channel_id):
        self.channel_id = channel_id
        self.config = {}
        self.name = e_ADC_CHANNEL.get(channel_id, "UNKNOWN")
        self.last_received_data = {"last": {}, "average": {}}


# --- Data Parsers & Utility Functions ---
def convert_to_si(value, unit):
    """Converts input values to standard SI units (Seconds, Celsius, Volts)."""
    try:
        v = float(value) if value != "" else 1.0
    except (ValueError, TypeError):
        return 1.0

    if not unit:
        return v

    unit = unit.strip().lower()
    unit_map = {
        # Time -> Seconds
        "ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0,
        # Temperature -> Celsius
        "c": lambda x: x,
        "k": lambda x: x - 273.15,
        "f": lambda x: (x - 32) * 5 / 9,
        # Voltage -> Volts
        "mv": 1e-3, "v": 1.0, "kv": 1e3,
    }

    if unit in unit_map:
        conversion = unit_map[unit]
        return conversion(v) if callable(conversion) else v * conversion

    return v


def parse_csv_line(line):
    """Splits a single CSV line while respecting quoted entries containing commas."""
    fields, current_field, in_quotes = [], [], False

    for char in line:
        if char == '"':
            in_quotes = not in_quotes
        elif char == "," and not in_quotes:
            fields.append("".join(current_field).strip())
            current_field = []
        else:
            current_field.append(char)

    fields.append("".join(current_field).strip())
    return fields


def _clean_csv_value(key, value):
    if not value:
        return ""
    if key == "ID":
        try:
            return int(value)
        except ValueError:
            return value
    elif key in ("SN_AFE", "SN_SiPM", "M/S"):
        return value

    clean_val = value.replace(",", ".")
    try:
        return float(clean_val)
    except ValueError:
        return value


async def read_calibration_csv(csv_file):
    """Reads calibration CSV files asynchronously and outputs averaged parameters per channel role."""
    callib_data = []
    try:
        with open(csv_file, mode="r", encoding="utf-8") as file:
            header_line = file.readline()
            if not header_line:
                return [], {}

            headers = parse_csv_line(header_line.strip())
            for line in file:
                line_str = line.strip()
                if not line_str:
                    continue
                values = parse_csv_line(line_str)
                row_dict = {
                    key: _clean_csv_value(key, val)
                    for key, val in zip(headers, values)
                }
                callib_data.append(row_dict)
    except Exception as e:
        print("Error reading calibration file {}: {}".format(csv_file, e))
        return [], {}

    if not callib_data:
        return [], {}

    callib_data_mean = {}
    for entry in callib_data:
        group = entry.get("M/S")
        if not group:
            continue

        if group not in callib_data_mean:
            callib_data_mean[group] = {}

        for k, v in entry.items():
            if k != "ID" and isinstance(v, (float, int)):
                if k not in callib_data_mean[group]:
                    callib_data_mean[group][k] = []
                callib_data_mean[group][k].append(v)

    # Compute group averages
    for group in ("M", "S"):
        if group in callib_data_mean:
            group_dict = callib_data_mean[group]
            for k, values in group_dict.items():
                group_dict[k] = sum(values) / len(values) if values else None
            group_dict["ID"] = 0
            group_dict["M/S"] = group

    return callib_data, callib_data_mean


async def get_configuration_from_files(
    afe_id,
    calibration_data_file_csv="dane_kalibracyjne.csv",
    temp_loop_file_csv="TempLoop.csv",
    uid=None,
):
    """Aggregates board configuration parameters from calibration CSV assets."""
    (temp_loop_data, temp_loop_mean), (calib_data, calib_mean) = await uasyncio.gather(
        read_calibration_csv(temp_loop_file_csv),
        read_calibration_csv(calibration_data_file_csv),
    )

    calibration = {"ID": afe_id}

    for dataset in (calib_data, temp_loop_data):
        for entry in dataset:
            if entry.get("ID") == afe_id:
                if uid is not None and entry.get("SN_AFE") != uid:
                    continue
                group = entry.get("M/S")
                if group:
                    if group not in calibration:
                        calibration[group] = {}
                    calibration[group].update(entry)

    for mean_dataset in (calib_mean, temp_loop_mean):
        for group in ("M", "S"):
            if group in mean_dataset:
                if group not in calibration:
                    calibration[group] = {}
                for k, default_val in mean_dataset[group].items():
                    current_val = calibration[group].get(k)
                    if current_val is None or str(current_val) == "":
                        calibration[group][k] = default_val

    return calibration