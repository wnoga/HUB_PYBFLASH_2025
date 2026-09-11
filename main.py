# main.py
import pyb
import uasyncio
import micropython
import sys
import select

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
    TX_DELAY_MS = 1


async def periodic_tasks_loop(logger):
    """Background loop to feed hardware watchdog and service asynchronous logging queues."""
    await p.print("Periodic background task started.")
    while True:
        wdt.feed()
        await logger.machine()  # Service buffered file system writes
        await p.machine()       # Service print buffer
        await uasyncio.sleep_ms(HardwareConfig.POLL_INTERVAL_MS)


async def async_repl(user_globals):
    """Non-blocking interactive REPL task over stdin/stdout."""
    print("Async REPL initialized. Type 'exit()' or 'quit()' to detach.")
    line = ""
    
    while True:
        if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
            char = sys.stdin.read(1)
            
            if char in ("\n", "\r"):
                cmd = line.strip()
                if cmd in ("exit()", "quit()"):
                    print("Exiting REPL session.")
                    return
                
                if cmd:
                    try:
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
            elif char == "\x7f":  # Backspace handling
                if line:
                    line = line[:-1]
                    print("\b \b", end="")
            elif char == "\x1b":  # Drop escape sequences (arrow keys)
                if sys.stdin.read(1) == "[":
                    sys.stdin.read(1)
            else:
                line += char
                print(char, end="")

        await uasyncio.sleep_ms(50)


async def main():
    await p.print("Initializing system components...")

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
    hub.discovery_active = True
    hub.rx_process_active = True
    hub.use_tx_delay = True
    hub.afe_manage_active = True
    hub.tx_delay_ms = HardwareConfig.TX_DELAY_MS
    hub.afe_id_min = HardwareConfig.AFE_ID_MIN
    hub.afe_id_max = HardwareConfig.AFE_ID_MAX
    await p.print("HUB initialized and configured.")

    # Task Registration
    tasks = [
        uasyncio.create_task(hub.main_loop()),
        uasyncio.create_task(rx_device_can.main_loop()),
        uasyncio.create_task(periodic_tasks_loop(logger)),
    ]

    if USE_ASYNC_SERVER:
        server = AsyncWebServer(hub)
        tasks.append(uasyncio.create_task(server.start()))
        await p.print("Async Web Server task detached.")

    # Optional REPL attach setup:
    # repl_globals = {"hub": hub, "p": p, "logger": logger}
    # tasks.append(uasyncio.create_task(async_repl(repl_globals)))

    await p.print("All runtime tasks scheduled.")

    # Block indefinitely while tasks execute concurrently
    await uasyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        uasyncio.run(main())
    except KeyboardInterrupt:
        print("\nSystem execution interrupted by user.")