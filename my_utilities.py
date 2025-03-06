import pyb

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
    writeGPIO = 0xa2
    setTemperatureLoopForChannelState_bySubdevice = 0xc0
    setTemperatureLoopForChannelState_byMask = 0xc1
    setDACValueRaw_bySubdevice = 0xc2
    setDACValueSi_bySubdevice = 0xc3
    stopTemperatureLoopForAllChannels = 0xc4
    setDAC_bySubdevice = 0xc5
    setAveragingMode = 0xd0
    setAveragingAlpha = 0xd1
    setAveragingBufferSize = 0xd2
    setChannel_dt_ms = 0xd3
    setAveraging_max_dt_ms = 0xd4
    setChannel_multiplicator = 0xd5
    setAveragingSubdevice = 0xd6
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
    def __init__(self, channel_id):
        self.channel_id = channel_id
        self.time_interval_ms = None
        self.alpha = None
        self.multiplicator = None
        self.averaging_mode = None
        self.latest_reading = SensorReading()
        
        #measurement download setttings
        self.periodic_interval_ms = 0
        self.periodic_sending_is_enabled = False
        
