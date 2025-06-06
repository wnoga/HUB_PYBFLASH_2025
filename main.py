# # main.py -- put your code here!
# import misc
# import afedrv
# import server
# import hub_test
# import hub_interface_v3
import pyb
import micropython
micropython.alloc_emergency_exception_buf(100)
# exit()
import _thread
# import micropython
# micropython.alloc_emergency_exception_buf(100)
from my_utilities import p
from my_utilities import JSONLogger


# from my_utilities import wdt
# from my_utilities import lock
import time

print("RESTART")

logger = JSONLogger()
can_bus = pyb.CAN(1)
# pyb.delay(500)

# from machine import WDT
# wdt = WDT(timeout=2000)  # enable it with a timeout of 2s
# wdt.feed()
if False:
    from my_database import SimpleFileDB, StatusFlags
    db = SimpleFileDB()
    db.save("test",StatusFlags.READY)
    while True:
        tmp = db.next(exclude_flags=0x00)
        if tmp is None:
            break
        print(tmp)
    print("#######")
    db.read_pos = 0
    cnt = 0
    while True:
        tmp = db.next(exclude_flags=0x00)
        if tmp is None:
            break
        if cnt == 2:
            db.update_status(tmp[0],StatusFlags.READY | StatusFlags.SAVED)
        print(tmp)
        cnt += 1
    print("#######")
    def t():
        db.read_pos = 0
        while True:
            # tmp = db.next(exclude_flags=StatusFlags.SAVED | StatusFlags.SENT)
            tmp = db.next(exclude_flags=StatusFlags.SAVED)
            if tmp is None:
                break
            print("To send:",tmp)
if True:

    from HUB import HUBDevice, initialize_can_hub
    from HUB import RxDeviceCAN

    can = None
    hub = None

    use_rxcallback = True
    can, hub, rxDeviceCAN = initialize_can_hub(
        can_bus=can_bus,
        logger=logger,
        use_rxcallback=use_rxcallback,
        use_automatic_restart=True)
    hub.afe_devices_max = 2

    use_lan_server = False
    if use_lan_server:
        from my_simple_server import MySimpleServer
        server = MySimpleServer(hub)
        server.running = True

    hub_process_enabled = True
    hub.discovery_active = True
    hub.rx_process_active = True
    hub.use_tx_delay = True
    hub.afe_manage_active = True
    hub.tx_delay_ms = 1
    hub.afe_id_min = 35
    hub.afe_id_max = 37

    if not use_rxcallback:
        _thread.start_new_thread(rxDeviceCAN.main_loop, ())
    _thread.start_new_thread(hub.main_loop, ())
    if use_lan_server:
        _thread.start_new_thread(server.main_loop, ())
