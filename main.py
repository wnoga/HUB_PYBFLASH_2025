# # main.py -- put your code here!
# import misc
# import afedrv
# import server
# import hub_test
# import hub_interface_v3
import pyb
import uasyncio
import micropython
micropython.alloc_emergency_exception_buf(100)
# import micropython
# micropython.alloc_emergency_exception_buf(100)
from my_utilities import p
from my_utilities import JSONLogger
from my_utilities import rtc_unix_timestamp, rtc


# from my_utilities import lock
import time

print("RESTART")

logger = JSONLogger()
can_bus = pyb.CAN(1)

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

async def main():
    p.print("Main async task started.")
    # Initialize components
    from HUB import initialize_can_hub # HUBDevice and RxDeviceCAN are returned by this
    # from HUB import RxDeviceCAN # Not needed separately if obtained from initialize_can_hub

    can = None
    hub = None
    rxDeviceCAN = None # Initialize to None

    use_rxcallback = True
    can, hub, rxDeviceCAN = initialize_can_hub(
        can_bus=can_bus,
        logger=logger,
        use_rxcallback=use_rxcallback,
        use_automatic_restart=True
    )
    hub.afe_devices_max = 2

    use_lan_server = True
    server = None # Initialize to None
    if use_lan_server:
        from my_simple_server import MySimpleServer
        server = MySimpleServer(hub)
        server.running = True

    # Configure HUB
    hub.discovery_active = True
    hub.rx_process_active = True
    hub.use_tx_delay = True
    hub.afe_manage_active = True
    hub.tx_delay_ms = 1
    hub.afe_id_min = 35
    hub.afe_id_max = 37 # Ensure this is less than afe_devices_max for discovery to stop if all found

    # Create asyncio tasks
    tasks = []
    
    tasks.append(uasyncio.create_task(hub.main_loop()))
    p.print("hub.main_loop task created.")

    if use_lan_server and server:
        tasks.append(uasyncio.create_task(server.main_loop()))
        p.print("server.main_loop task created.")
        tasks.append(uasyncio.create_task(server.sync_ntp_loop()))
        p.print("server.sync_ntp_loop task created.")
    
    if not use_rxcallback:
        tasks.append(uasyncio.create_task(rxDeviceCAN.main_loop()))
        p.print("rxDeviceCAN.main_loop task created.")

    # Keep main task alive and perform periodic operations
    while True:
        # wdt.feed() # If you have a watchdog, feed it here.
        logger.machine()  # Process logger queue
        p.process_queue() # Process print queue
        await uasyncio.sleep_ms(100) # Main loop tick

if __name__ == "__main__":
    try:
        uasyncio.run(main())
    except KeyboardInterrupt:
        p.print("Main program interrupted.")
    finally:
        p.print("Cleaning up and exiting main program.")
        # Perform any cleanup if necessary, e.g., closing server explicitly if not handled by tasks
        # Note: uasyncio tasks might need explicit cancellation for cleaner shutdown,
        # but for embedded systems, a hard reset or power cycle is common.
