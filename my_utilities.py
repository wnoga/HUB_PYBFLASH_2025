import pyb

class AFECommand:
    def __init__(self):
        self.getSerialNumber = 0x00
        self.getVersion = 0x01
        self.resetAll = 0x03

        self.getSensorDataSi_last = 0x30
        self.getSensorDataSi_average = 0x31
        self.getSensorDataSi_all_last = 0x32
        self.getSensorDataSi_all_average = 0x33
        self.setSensorDataSi_all_periodic_average = 0x34
        
        self.getSensorDataSiAndTimestamp_average = 0x3B
        self.getSensorDataSi_all_periodic_average = 0x3F

        self.transmitSPIData = 0xA0
        self.writeGPIO = 0xA2

        self.setTemperatureLoopForChannelState_bySubdevice = 0xC0
        self.setTemperatureLoopForChannelState_byMask = 0xC1
        self.setDACValueRaw_bySubdevice = 0xC2
        self.setDACValueSi_bySubdevice = 0xC3
        self.stopTemperatureLoopForAllChannels = 0xC4
        self.setDAC_bySubdevice = 0xC5

        self.setAveragingMode = 0xD0
        self.setAveragingAlpha = 0xD1
        self.setAveragingBufferSize = 0xD2
        self.setAveragingDt_ms = 0xD3
        self.setAveragingMaxDt_ms = 0xD4
        self.setAveragingMultiplicator = 0xD5
        self.setAveragingSubdevice = 0xD6

class AFECommandChannel:
    def __init__(self):
        self.Channel_0 = 0b00000001
        self.Channel_1 = 0b00000010
        self.Channel_2 = 0b00000100
        self.Channel_3 = 0b00001000
        self.Channel_4 = 0b00010000
        self.Channel_5 = 0b00100000
        self.Channel_6 = 0b01000000
        self.Channel_7 = 0b10000000

class AFECommandSubdevice:
    def __init__(self):
        self.master 	= 0b00000001
        self.slave 	= 0b00000010
        self.both 	= 0b00000011

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
