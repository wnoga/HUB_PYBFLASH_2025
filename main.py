# main.py
import pyb
import uasyncio
import micropython
import sys
import select
import gc

from my_utilities import p, wdt
from HUB import initialize_can_hub
from my_simple_server import AsyncWebServer
from my_logger import JSONLogger

# Allocate emergency exception buffer for ISR and async exceptions
micropython.alloc_emergency_exception_buf(100)

# Configuration flags
USE_ASYNC_SERVER = True
USE_RX_CALLBACK = True


class HardwareConfig:
    CAN_BUS_ID = 1
    POLL_INTERVAL_MS = 50
    AFE_ID_MIN = 1
    AFE_ID_MAX = 99
    TX_DELAY_MS = 50  # Increased from 1ms to prevent CAN bus flooding (State 1 warnings)


async def periodic_tasks_loop(logger):
    """Background loop to feed hardware watchdog, perform GC, and service logging queues."""
    await p.print("Periodic background task started.")
    gc_counter = 0
    while True:
        wdt.feed()
        if logger:
            await logger.machine()  # Service buffered file system writes
        await p.machine()           # Service print buffer
        
        gc_counter += 1
        if gc_counter >= 100:  # Perform garbage collection (~every 5 seconds)
            gc.collect()
            gc_counter = 0

        await uasyncio.sleep_ms(HardwareConfig.POLL_INTERVAL_MS)


async def async_repl(user_globals):
    """Non-blocking interactive REPL task over stdin/stdout."""
    print("Async REPL initialized. Type 'exit()' or 'quit()' to detach.")
    line = ""
    print(">>> ", end="")
    
    while True:
        # Non-blocking poll on stdin
        if select.select([sys.stdin], [], [], 0)[0]:
            char = sys.stdin.read(1)
            
            if char in ("\n", "\r"):
                print()  # Newline echo
                cmd = line.strip()
                if cmd in ("exit()", "quit()"):
                    print("Exiting REPL session.")
                    return
                
                if cmd:
                    try:
                        # Try evaluation first
                        result = eval(cmd, user_globals)
                        if result is not None:
                            print(repr(result))
                    except SyntaxError:
                        try:
                            exec(cmd, user_globals)
                        except Exception as e:
                            print("Exec Error:", e)
                    except Exception as e:
                        print("Eval Error:", e)
                
                line = ""
                print(">>> ", end="")

            elif char in ("\x08", "\x7f"):  # Backspace handling
                if line:
                    line = line[:-1]
                    print("\b \b", end="")

            elif char == "\x1b":  # Safely drain escape sequences without blocking loop
                await uasyncio.sleep_ms(10)
                while select.select([sys.stdin], [], [], 0)[0]:
                    sys.stdin.read(1)

            else:
                line += char
                print(char, end="")

        await uasyncio.sleep_ms(50)


def handle_exception(loop, context):
    """Global uasyncio exception handler to prevent silent task crashes."""
    exception = context.get('exception')
    print("Unhandled uasyncio exception:", exception)


async def main():
    await p.print("Initializing system components...")

    # Set exception handler for uasyncio loop
    loop = uasyncio.get_event_loop()
    loop.set_exception_handler(handle_exception)

    # Hardware & Subsystem Initialization
    can_bus = pyb.CAN(HardwareConfig.CAN_BUS_ID)
    logger = JSONLogger(keep_file_open=True)

    can, hub, rx_device_can = await initialize_can_hub(
        can_bus=can_bus,
        logger=logger,
        use_rxcallback=USE_RX_CALLBACK,
        use_automatic_restart=True,
    )

    # Hub Configuration
    hub.rx_process_active = True
    hub.use_tx_delay = True
    hub.afe_manage_active = True
    hub.tx_delay_ms = HardwareConfig.TX_DELAY_MS
    hub.afe_id_min = HardwareConfig.AFE_ID_MIN
    hub.afe_id_max = HardwareConfig.AFE_ID_MAX
    await p.print("HUB initialized and configured.")

    # Schedule Core Tasks
    uasyncio.create_task(rx_device_can.main_loop())
    uasyncio.create_task(periodic_tasks_loop(logger))
    uasyncio.create_task(hub.main_loop())

    if USE_ASYNC_SERVER:
        server = AsyncWebServer(hub)
        uasyncio.create_task(server.start())
        await p.print("Async Web Server task detached.")

    # Optional REPL attach setup (Uncomment to enable):
    # repl_globals = {"hub": hub, "p": p, "logger": logger}
    # uasyncio.create_task(async_repl(repl_globals))

    await p.print("All runtime tasks scheduled.")
    await hub.start_discovery()

    # Keep main task alive indefinitely
    while True:
        await uasyncio.sleep(3600)


if __name__ == "__main__":
    try:
        uasyncio.run(main())
    except KeyboardInterrupt:
        print("\nSystem execution interrupted by user.")