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
# from HUB import RxDeviceCAN # Not needed separately if obtained from initialize_can_hub

use_lan_server = False
use_async_server = True

use_rxcallback = True

# from my_utilities import VerbosityLevel
# def test():
#     logger.log(VerbosityLevel["CRITICAL"],"test {}".format(rtc_datetime_pretty()))

# async def clock_printer():
#     while True:
#         print(rtc_datetime_pretty())
        
#         await uasyncio.sleep(5)

async def periodic_tasks_loop():
    """Handles periodic background tasks like watchdog, logging, and printing."""
    await p.print("Periodic tasks loop started.") # Added await
    while True:
        wdt.feed()
        # try:
        # for _ in range(10):
        await logger.machine()  # logger.machine() can have blocking I/O
        # await uasyncio.sleep_ms(0)  # Yield after logger processing

        # Process logger queue -  This reduces the work per cycle & allows other operations to run.
        # if logger.log_queue:
        #     await logger._process_log_queue()
        
        await p.machine()  # p.process_queue() can have blocking I/O


        # await uasyncio.sleep_ms(0)  # Yield after print queue processing
        # except Exception as e:
        #     await p.print("Error in periodic_tasks_loop:", e) # Added await
        await uasyncio.sleep_ms(50) # Overall frequency for this loop


async def main():
    global can,hub,rxDeviceCAN,server
    await p.print("Main async task started.") # Added await

    # Create asyncio tasks list
    tasks = []
    
    # # ... other task creations ...
    
    # async def simple_test_task_func():
    #     count = 0
    #     while True:
    #         # Use standard print for direct output, bypassing p.print for this test
    #         print(f"SIM_SIMPLE_TEST_TASK: Alive! Count: {count}, Current Time: {time.time()}")
    #         count += 1
    #         await uasyncio.sleep(2) # Sleep for a noticeable interval
    
    # tasks.append(uasyncio.create_task(simple_test_task_func()))
    # # If p.print is working, this will show up (assuming periodic_tasks_loop is running):
    # await p.print("simple_test_task_func task created.") 
    # # Or use standard print for certainty during diagnostics:
    # print("INFO: simple_test_task_func task creation attempted.")
    
    
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

    
    if use_lan_server:
        from my_simple_server import MySimpleServer
        server = MySimpleServer(hub)
        server.running = True
        
    
    if use_async_server:
        from my_simple_server import AsyncWebServer
        server = AsyncWebServer(hub)
        # server.run()
    
    tasks.append(uasyncio.create_task(hub.main_loop()))
    await p.print("hub.main_loop task created.") # Added await

    if use_lan_server and server:
        tasks.append(uasyncio.create_task(server.main_loop()))
        await p.print("server.main_loop task created.") # Added await
        tasks.append(uasyncio.create_task(server.sync_ntp_loop()))
        await p.print("server.sync_ntp_loop task created.") # Added await
    
    if use_async_server and server:
        tasks.append(uasyncio.create_task(server.start()))
    
    # if not use_rxcallback:
    tasks.append(uasyncio.create_task(rxDeviceCAN.main_loop()))
    await p.print("rxDeviceCAN.main_loop task created.") # Added await
    
    tasks.append(uasyncio.create_task(logger.writer_main_loop()))
    await p.print("logger.writer_main_loop task created.") # Added await

    tasks.append(uasyncio.create_task(periodic_tasks_loop()))
    await p.print("periodic_tasks_loop task created.")
    
    # tasks.append(uasyncio.create_task(clock_printer()))
    
    

loop = uasyncio.get_event_loop()
loop.create_task(main())
# uasyncio.loop_forever()

# loop.run_forever()
# _thread.start_new_thread(hub.main_loop, ())
_thread.start_new_thread(loop.run_forever, ())

# if __name__ == "__main__":
#     try:
#         uasyncio.run(main())
#     except KeyboardInterrupt:
#         p.print("Main program interrupted.")
#     finally:
#         p.print("Cleaning up and exiting main program.")
#         # Perform any cleanup if necessary, e.g., closing server explicitly if not handled by tasks
#         # Note: uasyncio tasks might need explicit cancellation for cleaner shutdown,
#         # but for embedded systems, a hard reset or power cycle is common.
