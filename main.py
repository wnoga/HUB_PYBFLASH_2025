# # main.py -- put your code here!
# import misc
# import afedrv
# import server
# import hub_test
# import hub_interface_v3

from HUB import HUBDevice, initialize_can_hub
# can, hub = ci()
# hub.start_discover(1)
can = None
hub = None
# can, hub = initialize_can_hub()
# pyb.delay(500)
from my_server import MyServer
# Example usage:

if True:
    can, hub = initialize_can_hub()
    # server = MyServer(hub)
    # server.start_server()
    # hub.start_discovery(interval=0.1)
    # pass
    hub_process_enabled = True
    hub.discovery_active = True
    hub.rx_process_active = True
    # hub.tx_timeout_ms = 5000
    hub.use_tx_delay = True
    hub.afe_manage_active = True
    if True:
        import machine
        hubTask = machine.Timer()
        if True:
            hubTask.init(period=int(0.001 * 1000), mode=machine.Timer.PERIODIC, callback=hub.main_process)
        else:
            while(hub_process_enabled is True):
                hub.main_process()
        pyb.delay(500)
        hub.default_procedure()

# from pyb import CAN
# can = CAN(1)
# can.init(CAN.NORMAL,baudrate=100000)
# can.recv(0)
# def x():
#     can.send("\x00\x01",12)
#     return can.recv(0)

# import pyb
# import time
# from pyb import CAN
#
# def cb0(bus, reason):
#   print('cb0')
#   if reason == 0:
#       print('pending')
#   if reason == 1:
#       print('full')
#   if reason == 2:
#       print('overflow')
#
# can = CAN(1, CAN.LOOPBACK)
#
# # # can = pyb.CAN(1,pyb.CAN.LOOPBACK)
# # can = pyb.CAN(1)
# # can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54, sjw=1, bs1=7, bs2=2, auto_restart=False)
# #
# # can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
# # can.send('xyz',1)
#
# print("XxXXxxCjoajkkjkaS")
#
# can.rxcallback(0, cb0)
#
#
# # print(can.recv(0))
# # can.deinit()
# # exit(0)
# #
# # while True:
# #     try:
# #         can = pyb.CAN(1)
# #         # can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54, sjw=1, bs1=7, bs2=2, auto_restart=True)
# #         can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54, sjw=1, bs1=7, bs2=2, auto_restart=False)
# #         # Set filer - all responses to FIFO 0
# #         #can.setfilter(0, can.LIST16, 0, (0, 1, 2, 4))
# #         can.setfilter(0, can.MASK16, 0, (0, 0, 0, 0))
# #         # can.clearfilter(0)
# #         can.send("\x00\x02", 1)
# #     except:
# #         pass
# #     time.sleep(2.0)
