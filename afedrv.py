import time
import pyb

waiting_time = 0

A_MASTER_VOLTAGE_SET = {
    32: -259.848,
    34: -259.137,
    35: -258.026,
    36: -259.680,
    15: -259.6153,
    17: -259.020,
    1:  -260.402
}

A_MASTER_VOLTAGE_MEASURED = {
    32: 0.0184781,
    34: 0.0184015,
    35: 0.0184305,
    36: 0.0183569,
    15: 0.0184353,
    17: 0.0185293,
    1:  0.0182541
}

B_MASTER_VOLTAGE_SET = {
    32: 16521.54,
    34: 16439.47,
    35: 16462.59,
    36: 16606.33,
    15: 16416.93,
    17: 16471.26,
    1:  16593.85
}

B_MASTER_VOLTAGE_MEASURED = {
    32: -1.635,
    34: -1.705,
    35: -1.682,
    36: -1.775,
    15: -1.668,
    17: -1.688,
    1:  -1.577
}

A_SLAVE_VOLTAGE_SET = {
    32: -259.6294,
    34: -258.374,
    35: -260.156,
    36: -260.141,
    15: -260.037,
    17: -259.521,
    1:  -258.732
}

A_SLAVE_VOLTAGE_MEASURED = {
    32: 0.0184407,
    34: 0.0183964,
    35: 0.0184310,
    36: 0.0184000,
    15: 0.0183981,
    17: 0.0184359,
    1:  0.0184320
}

B_SLAVE_VOLTAGE_SET = {
    32: 16585.79,
    34: 16537.90,
    35: 16541.05,
    36: 16475.48,
    15: 16532.82,
    17: 16445.62,
    1:  16621.75
}

B_SLAVE_VOLTAGE_MEASURED = {
    32: -1.623,
    34: -1.619,
    35: -1.622,
    36: -1.672,
    15: -1.633,
    17: -1.687,
    1:  -1.628
}

A_MASTER_CURRENT_MEASURED = {
    32: 2.4126E-09,
    34: 2.4256E-09,
    35: 2.4243E-09,
    36: 2.4259E-09,
    15: 2.4250E-09,
    17: 2.4221E-09,
    1:  2.40547E-09
}

B_MASTER_CURRENT_MEASURED = {
    32: 1.06E-07,
    34: 5.0E-08,
    35: 7.0E-08,
    36: 4.0E-08,
    15: 7.0E-08,
    17: 7.7E-08,
    1:  6.27E-08
}

A_SLAVE_CURRENT_MEASURED = {
    32: 2.4297E-09,
    34: 2.4468E-09,
    35: 2.4185E-09,
    36: 2.4300E-09,
    15: 2.4147E-09,
    17: 2.4463E-09,
    1:  2.45140E-09
}

B_SLAVE_CURRENT_MEASURED = {
    32: 5.90E-08,
    34: 0.7E-08,
    35: 7.6E-08,
    36: 8.6E-08,
    15: 7.0E-08,
    17: 2.7E-08,
    1:  6.74E-08
}

A_TEMPERATURE_SET = 0.08057
B_TEMPERATURE_SET = 6

A_MASTER_VOLTAGE_SET_AVG = None
B_MASTER_VOLTAGE_SET_AVG = None

A_SLAVE_VOLTAGE_SET_AVG = None
B_SLAVE_VOLTAGE_SET_AVG = None

A_MASTER_VOLTAGE_MEASURED_AVG = None
B_MASTER_VOLTAGE_MEASURED_AVG = None

A_SLAVE_VOLTAGE_MEASURED_AVG = None
B_SLAVE_VOLTAGE_MEASURED_AVG = None

A_MASTER_CURRENT_MEASURED_AVG = None
B_MASTER_CURRENT_MEASURED_AVG = None

A_SLAVE_CURRENT_MEASURED_AVG = None
B_SLAVE_CURRENT_MEASURED_AVG = None

MASTER_SET_VOLTAGE = {}
SLAVE_SET_VOLTAGE = {}


def GetVer(id):
    print("Jestem GetVer()\n")
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # can.clearfilter(0)
    can.send("\x00\x01", id)
    # time.sleep(waiting_time)
    # print(can.recv(0))
    buf = bytearray(8)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    print("ID: ", lst[0])
    print("RTR: ", lst[1])
    print("FMI: ", lst[2])
    VerH = (lst[3][2] << 8) | (lst[3][3] & 0xff)
    print("VerH: ", VerH)
    VerL = (lst[3][4] << 8) | (lst[3][5] & 0xff)
    print("VerL: ", VerL)
    VerD = (lst[3][6] << 8) | (lst[3][7] & 0xff)
    print("VerD: ", VerD)


def GetUID0(id):
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    #can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # can.clearfilter(0)
    can.send("\x00\x02", id)
    # time.sleep(waiting_time)
    # print(can.recv(0))
    buf = bytearray(8)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    print("ID: ", lst[0])
    print("RTR: ", lst[1])
    print("FMI: ", lst[2])
    uid0 = (lst[3][2] << 24) | (lst[3][3] << 16) | (lst[3][4] << 8) | (lst[3][5] & 0xff)
    print("uid0: ", uid0)


def GetUID1(id):
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    #can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # can.clearfilter(0)
    can.send("\x00\x03", id)
    # time.sleep(waiting_time)
    # print(can.recv(0))
    buf = bytearray(8)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    print("ID: ", lst[0])
    print("RTR: ", lst[1])
    print("FMI: ", lst[2])
    uid1 = (lst[3][2] << 24) | (lst[3][3] << 16) | (lst[3][4] << 8) | (lst[3][5] & 0xff)
    print("uid1: ", uid1)


def GetUID2(id):
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    #can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # can.clearfilter(0)
    can.send("\x00\x04", id)
    # time.sleep(waiting_time)
    # print(can.recv(0))
    buf = bytearray(8)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    print("ID: ", lst[0])
    print("RTR: ", lst[1])
    print("FMI: ", lst[2])
    uid2 = (lst[3][2] << 24) | (lst[3][3] << 16) | (lst[3][4] << 8) | (lst[3][5] & 0xff)
    print("uid2: ", uid2)


def GetAdc(id, chn):
    print("Jestem AFE_GetAdc()\n")
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    if chn >= 1 and chn <= 3:
        can.send("\x00\x10", id)
    elif chn >= 4 and chn <= 6:
        can.send("\x00\x11", id)
    # Wait and read response
    time.sleep(waiting_time)
    buf = bytearray(8)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    print("ID: ", lst[0])
    print("RTR: ", lst[1])
    print("FMI: ", lst[2])
    if chn == 1:
        AdcValue = (lst[3][2] << 8) | (lst[3][3] & 0xff)
        print("adc value of ch", chn, ":", AdcValue, "V")
    elif chn == 2:
        AdcValue = (lst[3][4] << 8) | (lst[3][5] & 0xff)
        print("adc value of ch", chn, ":", AdcValue, "V")
    elif chn == 3:
        AdcValue = (lst[3][6] << 8) | (lst[3][7] & 0xff)
        # AdcValue = AdcValue * (70/4095)
        print("adc value of ch", chn, ":", AdcValue)
    elif chn == 4:
        AdcValue = (lst[3][2] << 8) | (lst[3][3] & 0xff)
        # AdcValue = AdcValue * (70/4095)
        print("adc value of ch", chn, ":", AdcValue)
    elif chn == 5:
        AdcValue = (lst[3][4] << 8) | (lst[3][5] & 0xff)
        print("adc value of ch", chn, ":", AdcValue, "I")
    elif chn == 6:
        AdcValue = (lst[3][6] << 8) | (lst[3][7] & 0xff)
        print("adc value of ch", chn, ":", AdcValue, "I")
    return AdcValue


def GetVoltageMasterV(id):
    global A_MASTER_VOLTAGE_MEASURED_AVG
    global B_MASTER_VOLTAGE_MEASURED_AVG
    voltage_bit = GetAdc(id, 3)

    if A_MASTER_VOLTAGE_MEASURED_AVG is None:
        res = 0
        for val in A_MASTER_VOLTAGE_MEASURED.values():
            res += val
        A_MASTER_VOLTAGE_MEASURED_AVG = res / len(A_MASTER_VOLTAGE_MEASURED)
    a_m = A_MASTER_VOLTAGE_MEASURED.get(id) if A_MASTER_VOLTAGE_MEASURED.get(
        id) is not None else A_MASTER_VOLTAGE_MEASURED_AVG
    print("a_m: ", a_m)
    print("a_m_avg: ", A_MASTER_VOLTAGE_MEASURED_AVG)

    if B_MASTER_VOLTAGE_MEASURED_AVG is None:
        res = 0
        for val in B_MASTER_VOLTAGE_MEASURED.values():
            res += val
        B_MASTER_VOLTAGE_MEASURED_AVG = res / len(B_MASTER_VOLTAGE_MEASURED)
    b_m = B_MASTER_VOLTAGE_MEASURED.get(id) if B_MASTER_VOLTAGE_MEASURED.get(
        id) is not None else B_MASTER_VOLTAGE_MEASURED_AVG
    print("b_m: ", b_m)
    print("b_m_avg: ", B_MASTER_VOLTAGE_MEASURED_AVG)

    return a_m * voltage_bit + b_m


def GetVoltageSlaveV(id):
    global A_SLAVE_VOLTAGE_MEASURED_AVG
    global B_SLAVE_VOLTAGE_MEASURED_AVG
    voltage_bit = GetAdc(id, 4)

    if A_SLAVE_VOLTAGE_MEASURED_AVG is None:
        res = 0
        for val in A_SLAVE_VOLTAGE_MEASURED.values():
            res += val
        A_SLAVE_VOLTAGE_MEASURED_AVG = res / len(A_SLAVE_VOLTAGE_MEASURED)

    a_s = A_SLAVE_VOLTAGE_MEASURED.get(id) if A_SLAVE_VOLTAGE_MEASURED.get(
        id) is not None else A_SLAVE_VOLTAGE_MEASURED_AVG
    print("a_s: ", a_s)
    print("a_s_avg: ", A_SLAVE_VOLTAGE_MEASURED_AVG)

    if B_SLAVE_VOLTAGE_MEASURED_AVG is None:
        res = 0
        for val in B_SLAVE_VOLTAGE_MEASURED.values():
            res += val
        B_SLAVE_VOLTAGE_MEASURED_AVG = res / len(B_SLAVE_VOLTAGE_MEASURED)

    b_s = B_SLAVE_VOLTAGE_MEASURED.get(id) if B_SLAVE_VOLTAGE_MEASURED.get(id) is not None else B_SLAVE_VOLTAGE_MEASURED_AVG
    print("b_s: ", b_s)
    print("b_s_avg: ", B_SLAVE_VOLTAGE_MEASURED_AVG)

    return a_s * voltage_bit + b_s


def GetSetVoltageMasterV(id):
    return MASTER_SET_VOLTAGE.get(id)


def GetSetVoltageSlaveV(id):
    return SLAVE_SET_VOLTAGE.get(id)


def GetCurrentMasterA(id):
    global A_MASTER_CURRENT_MEASURED_AVG
    global B_MASTER_CURRENT_MEASURED_AVG
    current_bit = GetAdc(id, 5)
    if A_MASTER_CURRENT_MEASURED_AVG is None:
        res = 0
        for val in A_MASTER_CURRENT_MEASURED.values():
            res += val
        A_MASTER_CURRENT_MEASURED_AVG = res / len(A_MASTER_CURRENT_MEASURED)

    a_m = A_MASTER_CURRENT_MEASURED.get(id) if A_MASTER_CURRENT_MEASURED.get(
        id) is not None else A_MASTER_CURRENT_MEASURED_AVG
    print("a_m: ", a_m)
    print("a_m_avg: ", A_MASTER_CURRENT_MEASURED_AVG)

    if B_MASTER_CURRENT_MEASURED_AVG is None:
        res = 0
        for val in B_MASTER_CURRENT_MEASURED.values():
            res += val
        B_MASTER_CURRENT_MEASURED_AVG = res / len(B_MASTER_CURRENT_MEASURED)

    b_m = B_MASTER_CURRENT_MEASURED.get(id) if B_MASTER_CURRENT_MEASURED.get(
        id) is not None else B_MASTER_CURRENT_MEASURED_AVG
    print("b_m: ", b_m)
    print("b_m_avg: ", B_MASTER_CURRENT_MEASURED_AVG)

    return a_m * current_bit + b_m


def GetCurrentSlaveA(id):
    global A_SLAVE_CURRENT_MEASURED_AVG
    global B_SLAVE_CURRENT_MEASURED_AVG
    current_bit = GetAdc(id, 6)
    if A_SLAVE_CURRENT_MEASURED_AVG is None:
        res = 0
        for val in A_SLAVE_CURRENT_MEASURED.values():
            res += val
        A_SLAVE_CURRENT_MEASURED_AVG = res / len(A_SLAVE_CURRENT_MEASURED)

    a_s = A_SLAVE_CURRENT_MEASURED.get(id) if A_SLAVE_CURRENT_MEASURED.get(
        id) is not None else A_SLAVE_CURRENT_MEASURED_AVG
    print("a_s: ", a_s)
    print("a_s_avg: ", A_SLAVE_CURRENT_MEASURED_AVG)

    if B_SLAVE_CURRENT_MEASURED_AVG is None:
        res = 0
        for val in B_SLAVE_CURRENT_MEASURED.values():
            res += val
        B_SLAVE_CURRENT_MEASURED_AVG = res / len(B_SLAVE_CURRENT_MEASURED)

    b_s = B_SLAVE_CURRENT_MEASURED.get(id) if B_SLAVE_CURRENT_MEASURED.get(
        id) is not None else B_SLAVE_CURRENT_MEASURED_AVG
    print("b_s: ", b_s)
    print("b_s_avg: ", B_SLAVE_CURRENT_MEASURED_AVG)

    return a_s * current_bit + b_s


def SetDacRAW(id, val1, val2):
    print("Jestem AFE_SetDacRaw()\n")
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = 0x12
    buf[2] = (val1 >> 8) & 0xFF
    buf[3] = val1 & 0xFF
    buf[4] = (val2 >> 8) & 0xFF
    buf[5] = val2 & 0xFF
    # buf[2] = int(val1conv)
    # buf[3] = int(val2conv)
    can.send(buf, id)
    time.sleep(waiting_time)
    buf2 = bytearray(8)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def SetDac(id, val1, val2):
    print("Jestem AFE_SetDac()\n")
    MASTER_SET_VOLTAGE[id] = val1
    SLAVE_SET_VOLTAGE[id] = val2
    global A_MASTER_VOLTAGE_SET_AVG
    if A_MASTER_VOLTAGE_SET_AVG is None:
        res = 0
        for val in A_MASTER_VOLTAGE_SET.values():
            res += val
        A_MASTER_VOLTAGE_SET_AVG = res / len(A_MASTER_VOLTAGE_SET)

    a_m = A_MASTER_VOLTAGE_SET.get(id) if A_MASTER_VOLTAGE_SET.get(id) is not None else A_MASTER_VOLTAGE_SET_AVG

    global B_MASTER_VOLTAGE_SET_AVG
    if B_MASTER_VOLTAGE_SET_AVG is None:
        res = 0
        for val in B_MASTER_VOLTAGE_SET.values():
            res += val
        B_MASTER_VOLTAGE_SET_AVG = res / len(B_MASTER_VOLTAGE_SET)

    b_m = B_MASTER_VOLTAGE_SET.get(id) if B_MASTER_VOLTAGE_SET.get(id) is not None else B_MASTER_VOLTAGE_SET_AVG

    global A_SLAVE_VOLTAGE_SET_AVG
    if A_SLAVE_VOLTAGE_SET_AVG is None:
        res = 0
        for val in A_SLAVE_VOLTAGE_SET.values():
            res += val
        A_SLAVE_VOLTAGE_SET_AVG = res / len(A_SLAVE_VOLTAGE_SET)

    a_s = A_SLAVE_VOLTAGE_SET.get(id) if A_SLAVE_VOLTAGE_SET.get(id) is not None else A_SLAVE_VOLTAGE_SET_AVG

    global B_SLAVE_VOLTAGE_SET_AVG
    if B_SLAVE_VOLTAGE_SET_AVG is None:
        res = 0
        for val in B_SLAVE_VOLTAGE_SET.values():
            res += val
        B_SLAVE_VOLTAGE_SET_AVG = res / len(B_SLAVE_VOLTAGE_SET)

    b_s = B_SLAVE_VOLTAGE_SET.get(id) if B_SLAVE_VOLTAGE_SET.get(id) is not None else B_SLAVE_VOLTAGE_SET_AVG

    val1conv = a_m * val1 + b_m
    val2conv = a_s * val2 + b_s

    if int(val1conv) < 0:
        val1conv = 0
    if int(val1conv) > 4095:
        val1conv = 4095

    if int(val2conv) < 0:
        val2conv = 0
    if int(val2conv) > 4095:
        val2conv = 4095

    print("dac1: ", int(val1conv), "dac2: ", int(val2conv))
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = 0x12
    buf[2] = (int(val1conv) >> 8) & 0xFF
    buf[3] = int(val1conv) & 0xFF
    buf[4] = (int(val2conv) >> 8) & 0xFF
    buf[5] = int(val2conv) & 0xFF
    can.send(buf, id)
    time.sleep(waiting_time)
    buf2 = bytearray(8)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)
    return (int(val1conv), int(val2conv))


def GetTemp(id):
    print("Jestem AFE_Temp()\n")
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    can.send("\x00\x13", id)
    time.sleep(waiting_time)
    # print(can.recv(0))
    buf = bytearray(6)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    print("ID: ", lst[0])
    print("RTR: ", lst[1])
    print("FMI: ", lst[2])
    TempVal1 = (lst[3][2] << 8) | (lst[3][3] & 0xff)
    print("temp value 1: ", TempVal1, "bits")
    TempVal2 = (lst[3][4] << 8) | (lst[3][5] & 0xff)
    print("temp value 2: ", TempVal2, "bits")
    return TempVal1, TempVal2


def GetTempMaster(id):
    tempMasterBit = GetTemp(id)[0]
    return A_TEMPERATURE_SET * tempMasterBit + B_TEMPERATURE_SET

def GetTempSlave(id):
    tempSlaveBit = GetTemp(id)[1]
    return A_TEMPERATURE_SET * tempSlaveBit + B_TEMPERATURE_SET

def SetDigRes(id, ch, val):
    print("Jestem SetDigRes()\n")
    # convert data
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(4)
    buf[0] = 0x00
    buf[1] = 0xA0
    buf[2] = ((ch) & 0xFF)
    buf[3] = ((val) & 0xFF)
    can.send(buf, id)
    time.sleep(waiting_time)
    buf2 = bytearray(8)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def SetHV(id, val):
    print("Jestem SetHV()\n")
    # convert data
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = 0x40
    buf[2] = 0
    buf[3] = 0
    buf[4] = 0
    buf[5] = 1 << val
    can.send(buf, id)
    time.sleep(waiting_time)
    # buf2 = bytearray(8)
    # lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def SetAllHV(id):
    print("Jestem SetHV()\n")
    # convert data
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = 0x40
    buf[2] = 0
    buf[3] = 0
    buf[4] = 0
    buf[5] = 3
    can.send(buf, id)
    time.sleep(waiting_time)
    # buf2 = bytearray(8)
    # lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def ClrHV(id, val):
    print("Jestem ClrHV()\n")
    # convert data
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = 0x41
    buf[2] = 0
    buf[3] = 0
    buf[4] = 0
    buf[5] = 1 << val
    can.send(buf, id)
    time.sleep(waiting_time)
    # buf2 = bytearray(8)
    # lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def ClrAllHV(id):
    print("Jestem ClrHV()\n")
    # convert data
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = 0x41
    buf[2] = 0
    buf[3] = 0
    buf[4] = 0
    buf[5] = 3
    can.send(buf, id)
    time.sleep(waiting_time)
    # buf2 = bytearray(8)
    # lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def GetHV(id, val):
    print("Jestem GetHV()\n")
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    can.send("\x00\x42", id)
    time.sleep(waiting_time)
    # print(can.recv(0))
    buf = bytearray(6)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    print("ID: ", lst[0])
    print("RTR: ", lst[1])
    print("FMI: ", lst[2])
    HVVal = ((lst[3][5] >> val) & 0x1)
    print("HV val: ", HVVal)


def GetAllHV(id):
    print("Jestem GetHV()\n")
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    can.send("\x00\x42", id)
    time.sleep(waiting_time)
    # print(can.recv(0))
    buf = bytearray(6)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    print("ID: ", lst[0])
    print("RTR: ", lst[1])
    print("FMI: ", lst[2])
    HVVal1 = ((lst[3][5] >> 0) & 0x1)
    print("HV val 1: ", HVVal1)
    HVVal2 = ((lst[3][5] >> 1) & 0x1)
    print("HV val 2: ", HVVal2)
    return HVVal1 & HVVal2


def SetCal(id, val):
    print("Jestem SetCal()\n")
    # convert data
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = 0x40
    buf[2] = 0
    buf[3] = 0
    buf[4] = 0
    buf[5] = 1 << (val + 2)
    can.send(buf, id)
    time.sleep(waiting_time)
    # buf2 = bytearray(8)
    # lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def SetAllCal(id):
    print("Jestem SetCal()\n")
    # convert data
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = 0x40
    buf[2] = 0
    buf[3] = 0
    buf[4] = 0
    buf[5] = 0xC
    can.send(buf, id)
    time.sleep(waiting_time)
    # buf2 = bytearray(8)
    # lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def ClrCal(id, val):
    print("Jestem ClrCal()\n")
    # convert data
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = 0x41
    buf[2] = 0
    buf[3] = 0
    buf[4] = 0
    buf[5] = 1 << (val + 2)
    can.send(buf, id)
    time.sleep(waiting_time)
    # buf2 = bytearray(8)
    # lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def ClrAllCal(id):
    print("Jestem ClrCal()\n")
    # convert data
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = 0x41
    buf[2] = 0
    buf[3] = 0
    buf[4] = 0
    buf[5] = 0xC
    can.send(buf, id)
    time.sleep(waiting_time)
    # buf2 = bytearray(8)
    # lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def GetCal(id, val):
    print("Jestem GetCal()\n")
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    can.send("\x00\x42", id)
    time.sleep(waiting_time)
    # print(can.recv(0))
    buf = bytearray(6)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    print("ID: ", lst[0])
    print("RTR: ", lst[1])
    print("FMI: ", lst[2])
    HVVal = ((lst[3][5] >> (val + 2)) & 0x1)
    print("HV val: ", HVVal)


def GetAllCal(id):
    print("Jestem GetCal()\n")
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    can.send("\x00\x42", id)
    time.sleep(waiting_time)
    # print(can.recv(0))
    buf = bytearray(6)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    print("ID: ", lst[0])
    print("RTR: ", lst[1])
    print("FMI: ", lst[2])
    HVVal1 = ((lst[3][5] >> (0 + 2)) & 0x1)
    print("HV val 1: ", HVVal1)
    HVVal2 = ((lst[3][5] >> (1 + 2)) & 0x1)
    print("HV val 2: ", HVVal2)


def SetCtrlLoop(id, ch):
    print("Jestem SetCtrlLoop()\n")
    # convert data
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54, sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = 0x43
    buf[2] = 0
    buf[3] = 0
    buf[4] = 0
    buf[5] = 0x01 << ch
    can.send(buf, id)
    time.sleep(waiting_time)
    # buf2 = bytearray(8)
    # lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def ClrCtrlLoop(id, ch):
    print("Jestem ClrCtrlLoop()\n")
    # convert data
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54, sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = 0x44
    buf[2] = 0
    buf[3] = 0
    buf[4] = 0
    buf[5] = 0x01 << ch
    can.send(buf, id)
    time.sleep(waiting_time)
    # buf2 = bytearray(8)
    # lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def SetConfRaw01(id, ch, vSet, vDelta):
    print("SetConf01()\n")
    # convert data
    # val1conv = ((val1 - 60)/5.2)*255
    # val2conv = ((val2 - 60)/5.2)*255
    # print("dac1: ",int(val1conv),"dac2: ",int(val2conv))
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54, sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = (0x80 + 2 * ch)
    buf[2] = (vSet >> 8) & 0xFF
    buf[3] = vSet & 0xFF
    buf[4] = (vDelta >> 8) & 0xFF
    buf[5] = vDelta & 0xFF
    # buf[2] = int(val1conv)
    # buf[3] = int(val2conv)
    can.send(buf, id)
    time.sleep(waiting_time)
    buf2 = bytearray(6)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def SetConfRaw02(id, ch, tDelta, filLen):
    print("SetConf02()\n")
    # convert data
    # val1conv = ((val1 - 60)/5.2)*255
    # val2conv = ((val2 - 60)/5.2)*255
    # print("dac1: ",int(val1conv),"dac2: ",int(val2conv))
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54, sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = (0x81 + 2 * ch)
    buf[2] = (tDelta >> 8) & 0xFF
    buf[3] = tDelta & 0xFF
    buf[4] = (filLen >> 8) & 0xFF
    buf[5] = filLen & 0xFF
    # buf[2] = int(val1conv)
    # buf[3] = int(val2conv)
    can.send(buf, id)
    time.sleep(waiting_time)
    buf2 = bytearray(6)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def GetCtrLoopData1(id, ch):
    print("GetCtrLoopData1\n")
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    buf = bytearray(6)
    buf[0] = 0x01
    buf[1] = 0x10
    buf[2] = 0
    buf[3] = 0
    buf[4] = 0
    buf[5] = 1 << ch
    can.send(buf, id)
    time.sleep(waiting_time)
    # print(can.recv(0))
    buf2 = bytearray(8)
    lst = [0, 0, 0, memoryview(buf2)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    vDelta = (lst[3][2] << 8) | (lst[3][3] & 0xff)
    print("vDelta value: ", vDelta)
    tDelta = (lst[3][4] << 8) | (lst[3][5] & 0xff)
    print("tDelta value: ", tDelta)
    filLen = (lst[3][6] << 8) | (lst[3][7] & 0xff)
    print("filLen value: ", filLen)
    return vDelta, tDelta, filLen


def GetCtrLoopData2(id, ch):
    print("GetCtrLoopData2\n")
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    buf = bytearray(6)
    buf[0] = 0x01
    buf[1] = 0x11
    buf[2] = 0
    buf[3] = 0
    buf[4] = 0
    buf[5] = 1 << ch
    can.send(buf, id)
    time.sleep(waiting_time)
    # print(can.recv(0))
    buf2 = bytearray(8)
    lst = [0, 0, 0, memoryview(buf2)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    vSetAct = (lst[3][2] << 8) | (lst[3][3] & 0xff)
    print("vSetAct value: ", vSetAct)
    tSetAct = (lst[3][4] << 8) | (lst[3][5] & 0xff)
    print("tSetAct value: ", tSetAct)
    avgFil = (lst[3][6] << 8) | (lst[3][7] & 0xff)
    print("avgFil value: ", avgFil)
    return vSetAct, tSetAct, avgFil


def GetCtrLoopData3(id, ch):
    print("GetCtrLoopData3\n")
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    buf = bytearray(6)
    buf[0] = 0x01
    buf[1] = 0x12
    buf[2] = 0
    buf[3] = 0
    buf[4] = 0
    buf[5] = 1 << ch
    can.send(buf, id)
    time.sleep(waiting_time)
    # print(can.recv(0))
    buf2 = bytearray(6)
    lst = [0, 0, 0, memoryview(buf2)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    sumFil = (lst[3][2] << 24) | (lst[3][3] << 16) | (lst[3][4] << 8) | (lst[3][5] & 0xff)
    print("sumFil value: ", sumFil)
    return sumFil


def GetCtrLoopData4(id, ch):
    print("GetCtrLoopData4\n")
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    buf = bytearray(6)
    buf[0] = 0x01
    buf[1] = 0x13
    buf[2] = 0
    buf[3] = 0
    buf[4] = 0
    buf[5] = 1 << ch
    can.send(buf, id)
    time.sleep(waiting_time)
    # print(can.recv(0))
    buf2 = bytearray(7)
    lst = [0, 0, 0, memoryview(buf2)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    filNewPtr = (lst[3][2] << 8) | (lst[3][3] & 0xff)
    print("filNewPtr value: ", filNewPtr)
    filOldPtr = (lst[3][4] << 8) | (lst[3][5] & 0xff)
    print("filOldPtr value: ", filOldPtr)
    CtreReg = (lst[3][6] & 0xff)
    print("CtreReg value: ", CtreReg)
    return filNewPtr, filOldPtr, CtreReg


def SetSimTempTest(id):
    print("setSimTempTest\n")
    # convert data
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54, sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = 0x43
    buf[2] = 0x01
    buf[3] = 0
    buf[4] = 0
    buf[5] = 0
    can.send(buf, id)
    time.sleep(waiting_time)
    # buf2 = bytearray(8)
    # lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def ClrSimTempTest(id):
    print("Jestem ClrCtrlLoop()\n")
    # convert data
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54, sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x00
    buf[1] = 0x44
    buf[2] = 0x01
    buf[3] = 0
    buf[4] = 0
    buf[5] = 0
    can.send(buf, id)
    time.sleep(waiting_time)
    # buf2 = bytearray(8)
    # lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def SetSimTempValTest(id, val1, val2):
    print("Jestem SetSimTempValTest()\n")
    # convert data
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    buf = bytearray(6)
    buf[0] = 0x01
    buf[1] = 0x14
    buf[2] = (val1 >> 8) & 0xFF
    buf[3] = val1 & 0xFF
    buf[4] = (val2 >> 8) & 0xFF
    buf[5] = val2 & 0xFF
    can.send(buf, id)
    time.sleep(waiting_time)
    buf2 = bytearray(8)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    print(can.recv(0))
    # can.recv(0, lst)


def GetAdcAvg(id, chn):
    print("Jestem AFE_GetAdcAvg()\n")
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    # Send the command to AFE:
    if chn >= 1 and chn <= 2:
        can.send("\x00\x14", id)
    elif chn >= 3 and chn <= 4:
        can.send("\x00\x15", id)
    elif chn >= 5 and chn <= 6:
        can.send("\x00\x16", id)
        # Wait and read response
    time.sleep(waiting_time)
    buf = bytearray(8)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    print("ID: ", lst[0])
    print("RTR: ", lst[1])
    print("FMI: ", lst[2])
    if chn == 1:
        AdcValue = (lst[3][2] << 16) | (lst[3][3] << 8) | (lst[3][4] & 0xff)
        print("adc value of ch", chn, ":", AdcValue, "V")
    elif chn == 2:
        AdcValue = (lst[3][5] << 16) | (lst[3][6] << 8) | (lst[3][7] & 0xff)
        print("adc value of ch", chn, ":", AdcValue, "V")
    elif chn == 3:
        AdcValue = (lst[3][2] << 16) | (lst[3][3] << 8) | (lst[3][4] & 0xff)
        # AdcValue = AdcValue * (70/4095)
        print("adc value of ch", chn, ":", AdcValue)
    elif chn == 4:
        AdcValue = (lst[3][5] << 16) | (lst[3][6] << 8) | (lst[3][7] & 0xff)
        # AdcValue = AdcValue * (70/4095)
        print("adc value of ch", chn, ":", AdcValue)
    elif chn == 5:
        AdcValue = (lst[3][2] << 16) | (lst[3][3] << 8) | (lst[3][4] & 0xff)
        print("raw adc value of ch", chn, ":", AdcValue, "I")
        print("adc value of ch [uA]", chn, ":", AdcValue, "I")
    elif chn == 6:
        AdcValue = (lst[3][5] << 16) | (lst[3][6] << 8) | (lst[3][7] & 0xff)
        print("adc value of ch", chn, ":", AdcValue, "I")
    return AdcValue


def GetTempAvg(id):
    print("Jestem AFE_TempAvg()\n")
    can = pyb.CAN(1)
    can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
             sjw=1, bs1=7, bs2=2, auto_restart=True)
    # Set filer - all responses to FIFO 0
    # can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
    can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
    can.send("\x00\x17", id)
    time.sleep(waiting_time)
    # print(can.recv(0))
    buf = bytearray(8)
    lst = [0, 0, 0, memoryview(buf)]
    # No heap memory is allocated in the following call
    can.recv(0, lst)
    print("ID: ", lst[0])
    print("RTR: ", lst[1])
    print("FMI: ", lst[2])
    TempVal1 = (lst[3][2] << 16) | (lst[3][3] << 8) | (lst[3][4] & 0xff)
    print("temp value 1: ", TempVal1, "bits")
    TempVal2 = (lst[3][5] << 16) | (lst[3][6] << 8) | (lst[3][7] & 0xff)
    print("temp value 2: ", TempVal2, "bits")
    return TempVal1, TempVal2
