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
import sys
import select
import time
# micropython.alloc_emergency_exception_buf(100)
# import micropython
# micropython.alloc_emergency_exception_buf(100)
from my_utilities import p, wdt
from my_utilities import JSONLogger
from my_utilities import rtc_unix_timestamp, rtc, rtc_datetime_pretty
from my_RxDeviceCAN import RxDeviceCAN
# from my_utilities import lock


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
        

# Optional: you can use a globals dictionary to persist variables
user_globals = {}

async def async_repl():
    print("Async REPL (type 'exit()' to quit):")
    line = ''
    while True:
        # Check if data is available on stdin (non-blocking)
        if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
            char = sys.stdin.read(1)
            if char in ('\n', '\r'):
                if line.strip() in ('exit()', 'quit()'):
                    print("Exiting REPL.")
                    return
                try:
                    # Try evaluating the line
                    result = eval(line, user_globals)
                    if result is not None:
                        print(repr(result))
                except SyntaxError:
                    # If not an expression, treat as statement
                    try:
                        exec(line, user_globals)
                    except Exception as e:
                        print("Exec error:", e)
                except Exception as e:
                    print("Eval error:", e)
                line = ''  # Clear line buffer
                print('>>> ', end='')  # Prompt again
            else:
                if char == '\x7f':  # Backspace
                    if line:
                        line = line[:-1]
                        print('\b \b', end='')  # Erase character from terminal
                elif char == '\x04':  # Ctrl+D (EOF)
                    pass # Not implemented
                else:
                    # Handle arrow keys (common ANSI escape codes)
                    if char == '\x1b':  # Start of an escape sequence
                        next_char = sys.stdin.read(1)
                        if next_char == '[':
                            final_char = sys.stdin.read(1)
                            if final_char == 'A': # Up arrow
                                print("\nUp arrow pressed (not implemented)")
                            elif final_char == 'B': # Down arrow
                                print("\nDown arrow pressed (not implemented)")
                            elif final_char == 'C': # Right arrow
                                print("\nRight arrow pressed (not implemented)")
                            elif final_char == 'D': # Left arrow
                                print("\nLeft arrow pressed (not implemented)")
                            continue # Skip adding escape sequence to line
                    line += char
                    print(char, end='') # Echo the character back to the user
        await uasyncio.sleep(0.05)  # Yield to other tasks


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
    hub.afe_devices_max = 1 # Configure after hub is initialized

    # Configure HUB (moved here after hub is initialized)
    hub.discovery_active = True
    hub.rx_process_active = True
    hub.use_tx_delay = True
    hub.afe_manage_active = True
    hub.tx_delay_ms = 1
    hub.afe_id_min = 1
    hub.afe_id_max = 99 # Ensure this is less than afe_devices_max for discovery to stop if all found
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
    
    # tasks.append(uasyncio.create_task(logger.writer_main_loop()))
    # await p.print("logger.writer_main_loop task created.") # Added await

    tasks.append(uasyncio.create_task(periodic_tasks_loop()))
    await p.print("periodic_tasks_loop task created.")
    
    # user_globals.update({'hub': hub, 'p': p, 'server': server})
    # tasks.append(uasyncio.create_task(async_repl()))


loop = uasyncio.get_event_loop()
loop.create_task(main())
# _thread.start_new_thread(loop.run_forever, ()) # allow interactive mode (REPL)
loop.run_forever() # Run withouth REPL
