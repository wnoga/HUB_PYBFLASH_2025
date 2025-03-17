import network
import _thread
import usocket
import ujson
import misc
import afedrv

import machine
from HUB import HUBDevice, initialize_can_hub

OK = None
ERROR = 1

can = None
hub = None
hubTask = None


BUFFER_SIZE = 1024
DISCONNECTED_MESSAGE = '!disconnect'


# Functions which process requests
def test_proper_connection():
    if serv:
        return ('OK', 'Connected')
    else:
        return ('ERR', 'Not Connected')

# print("after test proper connection")

def loop_for_list_arg(func, obj):
    result = dict()
    for board in obj:
        try:
            result[board] = func(board)
        except:
            result[board] = 'ERR'
    return ('OK', result)


def loop_for_afe_list_arg(func, obj1, obj2, obj3):
    result = dict()
    for num, v1, v2 in zip(obj1, obj2, obj3):
        try:
            result[num] = func(num, v1, v2)
        except:
            result[num] = 'ERR'
    return ('OK', result)

can = None
hub = HUBDevice() # this is only for the IDE autocomplete
hub = None
hubTask = None

def initialization(obj):
    can, hub = initialize_can_hub()
    # hub.start_discovery(interval=0.1)
    # hub_process_enabled = True
    # hub.tx_timeout_ms = 5000
    hub.use_tx_delay = True
    hubTask = machine.Timer()
    return 'OK', OK
    # if isinstance(obj[1], list):
    #     return loop_for_list_arg(misc.init, obj[1])
    # else:
    #     return ('OK', misc.init(obj[1]))


def turn_on_hub(obj):
    # return 'OK', misc.HUBon()
    ###########
    hub.discovery_active = True
    hub.rx_process_active = True
    hubTask.init(period=int(0.001 * 1000), mode=machine.Timer.PERIODIC, callback=hub.main_process)
    return 'OK', OK


def is_hub_on(obj):
    # return 'OK', misc.isHUBon()
    return 'OK', OK if hub is not None else ERROR


def turn_off_hub(obj):
    hub.discovery_active = False
    hub.rx_process_active = False
    hubTask.deinit()
    return 'OK', OK


def initId(obj):
    raise "Not implemented"
    return ("init", obj[1]), misc.init(obj[1])

def turn_on(obj):
    raise "Not implemented"
    if isinstance(obj[1], list):
        return loop_for_list_arg(misc.HVon, obj[1])
    return ('OK', misc.HVon(obj[1]))

def hvonId(obj):
    hub.set_hv_on(obj[1])
    raise "Not implemented"
    return ("hvon", obj[1]), misc.HVon(obj[1])

def turn_off(obj):
    if isinstance(obj[1], list):
        return loop_for_list_arg(misc.HVoff, obj[1])
    return ('OK', misc.HVoff(obj[1]))


def hvoffId(obj):
    return ("hvoff", obj[1]), misc.HVoff(obj[1])


def turn_on_slab(obj):
    return 'OK', afedrv.SetAllHV(obj[1])


def turn_off_slab(obj):
    return 'OK', afedrv.ClrAllHV(obj[1])

def is_slab_on(obj):
    return 'OK', afedrv.GetAllHV(obj[1])


def setdac(obj):
    if isinstance(obj[1], list):
        return loop_for_afe_list_arg(afedrv.SetDac, obj[1], obj[2], obj[3])
    return ('OK', afedrv.SetDac(obj[1], obj[2], obj[3]))


def setdacId(obj):
    return ("setdac", obj[1]), afedrv.SetDac(obj[1], obj[2], obj[3])


def setrawdac(obj):
    return ('OK', afedrv.SetDacRAW(obj[1], obj[2], obj[3]))


def get_adc_and_temp(obj):
    adc_m = afedrv.GetAdc(obj[1], 3)
    adc_s = afedrv.GetAdc(obj[1], 4)
    temp = afedrv.GetTemp(obj[1])
    return ('OK', (adc_m, temp[0]), (adc_s, temp[1]))


def getadc(obj):
    return ('OK', afedrv.GetAdc(obj[1], obj[2]))

def getVM(obj):
    return ('OK', afedrv.GetVoltageMasterV(obj[1]))

def getVS(obj):
    return ('OK', afedrv.GetVoltageSlaveV(obj[1]))

def set_offset(obj):
    offm = afedrv.SetDigRes(obj[1], 0, obj[2])
    offs = afedrv.SetDigRes(obj[1], 1, obj[3])
    return ('OK', offm, offs)


def gettemp(obj):
    return ('OK', afedrv.GetTemp(obj[1]))


def get_temperature_master_c(obj):
    return ('OK', afedrv.GetTempMaster(obj[1]))


def get_temperature_degree_masterId(obj):
    return ("get_temperature_degree_masterId", obj[1]), afedrv.GetTempMaster(obj[1])


def get_temperature_slave_c(obj):
    return ('OK', afedrv.GetTempSlave(obj[1]))


def get_temperature_degree_slaveId(obj):
    return ("get_temperature_degree_slaveId", obj[1]), afedrv.GetTempSlave(obj[1])

def get_temp_avg(obj):
    return ('OK', afedrv.GetTempAvg(obj[1]))


def get_adc_avg(obj):
    return ('OK', afedrv.GetAdcAvg(obj[1], obj[2]))


def get_master_voltage(obj):
    return ('OK', afedrv.GetVoltageMasterV(obj[1]))


def get_master_voltageId(obj):
    return ("get_master_voltageId", obj[1]), afedrv.GetVoltageMasterV(obj[1])

def get_master_set_voltage(obj):
    return ('OK', afedrv.GetSetVoltageMasterV(obj[1]))

def get_slave_voltage(obj):
    return ('OK', afedrv.GetVoltageSlaveV(obj[1]))


def get_slave_voltageId(obj):
    return ("get_slave_voltageId", obj[1]), afedrv.GetVoltageSlaveV(obj[1])


def get_slave_set_voltage(obj):
    return ('OK', afedrv.GetSetVoltageSlaveV(obj[1]))

def get_master_amperage(obj):
    return ('OK', afedrv.GetCurrentMasterA(obj[1]))


def get_master_amperageId(obj):
    return ("get_master_amperageId", obj[1]), afedrv.GetCurrentMasterA(obj[1])

def get_slave_amperage(obj):
    return ('OK', afedrv.GetCurrentSlaveA(obj[1]))


def get_slave_amperageId(obj):
    return ("get_slave_amperageId", obj[1]), afedrv.GetCurrentSlaveA(obj[1])


def Data1(obj):
    return ('OK', afedrv.GetCtrLoopData1(obj[1], obj[2]))


def Data2(obj):
    return ('OK', afedrv.GetCtrLoopData2(obj[1], obj[2]))


def Data3(obj):
    return ('OK', afedrv.GetCtrLoopData3(obj[1], obj[2]))


def Data4(obj):
    return ('OK', afedrv.GetCtrLoopData4(obj[1], obj[2]))


# Table of functions
func = {
    'init': initialization,
    'initId': initId,
    'hvon': turn_on,
    'hubOn': turn_on_hub,
    'slabOn': turn_on_slab,
    'isHubOn': is_hub_on,
    'hvoff': turn_off,
    'hubOff': turn_off_hub,
    'slabOff': turn_off_slab,
    'isSlabOn': is_slab_on,
    'setdac': setdac,
    'setdacId': setdacId,
    'setrawdac': setrawdac,
    'test': test_proper_connection,
    'getVT': get_adc_and_temp,
    'adc': getadc,
    'setoffset': set_offset,
    'gettemp': gettemp,
    'get_temperature_degree_master': get_temperature_master_c,
    'get_temperature_degree_masterId': get_temperature_degree_masterId,
    'get_temperature_degree_slave': get_temperature_slave_c,
    'get_temperature_degree_slaveId': get_temperature_degree_slaveId,
    'get_adc_avg': get_adc_avg,
    'get_temp_avg': get_temp_avg,
    'get_master_set_voltage': get_master_set_voltage,
    'get_slave_set_voltage': get_slave_set_voltage,
    'get_master_voltage': get_master_voltage,
    'get_master_voltageId': get_master_voltageId,
    'get_slave_voltage': get_slave_voltage,
    'get_slave_voltageId': get_slave_voltageId,
    'get_master_amperage': get_master_amperage,
    'get_master_amperageId': get_master_amperageId,
    'get_slave_amperage': get_slave_amperage,
    'get_slave_amperageId': get_slave_amperageId,
    'data1': Data1,
    'data2': Data2,
    'data3': Data3,
    'data4': Data4,
    'getVM': getVM,
    'getVS': getVS
}


class Ctlsrv():
    def __init__(self):
        # Start Ethernet
        self.lan = network.LAN()
        self.lan.active(1)
        # Start server
        self.srvthread = None
        self.runflag = False
        self.ip = None

    def getip(self):
        self.ip = self.lan.ifconfig()[0]

    def __str__(self):
        self.getip()
        return 'AFE HUB %s' % (self.ip)

    @staticmethod
    def send_msg(cl, msg):
        cl.sendall((ujson.dumps(msg)).encode("utf8"))

    def get_IP(self):
        print(self.lan.ifconfig())

    def srv_handle(self, port):
        addr = usocket.getaddrinfo('0.0.0.0', port)[0][-1]
        print(addr)
        s = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
        s.setsockopt(usocket.SOL_SOCKET, usocket.SO_REUSEADDR, 1)
        s.bind(addr)
        print(s)
        s.listen(1)
        print('listening on', addr)
        while self.runflag:
            cl, addr = s.accept()
            print('client connected from', addr)
            Ctlsrv.send_msg(cl, ('Client connected with %s' % (self)))
            while True:
                try:
                    json = cl.recv(BUFFER_SIZE)
                except Exception as e:
                    res = ('ERR', str(e))
                    break

                try:
                    cmd = ujson.loads(json)
                    print(cmd[0])
                    if cmd[0] == DISCONNECTED_MESSAGE: break
                    res = func[cmd[0]](cmd)
                except Exception as e:
                    res = ('ERR', str(e))
                Ctlsrv.send_msg(cl, res)

            cl.close()

    def run(self, port):
        if self.srvthread:
            raise (Exception("Server already running"))
        self.runflag = True
        self.srvthread = _thread.start_new_thread(self.srv_handle, (port,))
        return

    def stop(self):
        self.runflag = False
        return


serv = Ctlsrv()
serv.run(5555)
