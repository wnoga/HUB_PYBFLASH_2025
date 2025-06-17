# # main.py -- put your code here!
# import misc
# import afedrv
# import server
# import hub_test
# import hub_interface_v3
import pyb
import uasyncio
import micropython
import _thread
# micropython.alloc_emergency_exception_buf(100)
# import micropython
# micropython.alloc_emergency_exception_buf(100)
from my_utilities import p, wdt
from my_utilities import JSONLogger
from my_utilities import rtc_unix_timestamp, rtc, rtc_datetime_pretty
from my_RxDeviceCAN import RxDeviceCAN
# from my_utilities import lock
import time

can_bus = pyb.CAN(1)
logger = JSONLogger(keep_file_open=True
                    # ,parent_dir="/tmp/HUB_simulator/"
                    )
# print("RESTART") # This would need to be `await p.print` within an async context
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
            
can = None
hub = None
rxDeviceCAN = None # Initialize to None
server = None # Initialize to None

# Initialize components
from HUB import initialize_can_hub # HUBDevice and RxDeviceCAN are returned by this

use_async_server = True
use_rxcallback = True

async def periodic_tasks_loop():
    """Handles periodic background tasks like watchdog, logging, and printing."""
    await p.print("Periodic tasks loop started.") # Added await
    while True:
        wdt.feed()
        await logger.machine()  # logger.machine() can have blocking I/O
        await p.machine()  # p.process_queue() can have blocking I/O
        await uasyncio.sleep_ms(50) # Overall frequency for this loop


async def main():
    global can,hub,rxDeviceCAN,server
    await p.print("Main async task started.") # Added await

    # Create asyncio tasks list
    tasks = []
    
    can, hub, rxDeviceCAN = await initialize_can_hub( # Added await
        can_bus=can_bus,
        logger=logger,
        use_rxcallback=use_rxcallback,
        use_automatic_restart=True
    )
    hub.afe_devices_max = 2 # Configure after hub is initialized

    # Configure HUB (moved here after hub is initialized)
    hub.discovery_active = True
    hub.rx_process_active = True
    hub.use_tx_delay = True
    hub.afe_manage_active = True
    hub.tx_delay_ms = 1
    hub.afe_id_min = 35
    hub.afe_id_max = 37 # Ensure this is less than afe_devices_max for discovery to stop if all found
    await p.print("HUB configured.")
    
    if use_async_server:
        from my_simple_server import AsyncWebServer
        server = AsyncWebServer(hub)
        tasks.append(uasyncio.create_task(server.start()))

    tasks.append(uasyncio.create_task(hub.main_loop()))
    await p.print("hub.main_loop task created.") # Added await

    if server:
        tasks.append(uasyncio.create_task(server.sync_ntp_loop()))
        await p.print("server.sync_ntp_loop task created.") # Added await
    
    tasks.append(uasyncio.create_task(rxDeviceCAN.main_loop()))
    await p.print("rxDeviceCAN.main_loop task created.") # Added await
    
    tasks.append(uasyncio.create_task(logger.writer_main_loop()))
    await p.print("logger.writer_main_loop task created.") # Added await

    tasks.append(uasyncio.create_task(periodic_tasks_loop()))
    await p.print("periodic_tasks_loop task created.")

    
    

loop = uasyncio.get_event_loop()
loop.create_task(main())
# uasyncio.loop_forever()

# loop.run_forever()
_thread.start_new_thread(loop.run_forever, ()) # allow interactive mode (REPL)
