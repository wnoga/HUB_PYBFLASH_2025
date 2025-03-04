# misc.py
import time
import pyb
import afedrv
import hub
import os

def HUBon():
    pyb.Pin.cpu.E12.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
    pyb.Pin.cpu.E12.value(1)

def isHUBon():
    pyb.Pin.cpu.E12.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
    return pyb.Pin.cpu.E12.value()

def HUBoff():
    pyb.Pin.cpu.E12.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
    pyb.Pin.cpu.E12.value(0)

def HVon(id):
    pyb.Pin.cpu.E12.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
    pyb.Pin.cpu.E12.value(1)
    afedrv.SetAllHV(id)
    print("HV is on")


def HVoff(id):
    pyb.Pin.cpu.E12.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
    pyb.Pin.cpu.E12.value(0)
    afedrv.ClrAllHV(id)
    print("HV is off")


def init(id):
    x = afedrv.SetDac(id, 50, 50)
    afedrv.SetDigRes(id, 0, 200)
    afedrv.SetDigRes(id, 1, 200)
    return x
    
#ch to simp 
# misc.testCtrlLoop(1, 0, 60, 200, 10, 60)
def testCtrlLoop(id, ch, val, vdelta, tdelta, filtr):
    pyb.Pin.cpu.E12.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
    pyb.Pin.cpu.E12.value(1)
    afedrv.SetHV(id, ch)
    val1conv = int((-271.2)*val + 18142)
    afedrv.SetConfRaw01(id, ch, val1conv, vdelta)
    afedrv.SetConfRaw02(id, ch, tdelta, filtr)
    afedrv.SetCtrlLoop(id, ch)

def testCtrlLoopOff(id, ch):
    afedrv.ClrCtrlLoop(id, ch)
    
    

def printtest():
    return "Testing printing connection"

