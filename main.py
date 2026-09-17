# main.py

import pyb
import uasyncio
import micropython
import sys
import gc

from my_utilities import p, wdt
from HUB import initialize_can_hub
from my_simple_server import AsyncWebServer
from my_logger import JSONLogger

# ------------------------------------------------------------
# MicroPython IRQ safety
# ------------------------------------------------------------

micropython.alloc_emergency_exception_buf(100)


# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

USE_ASYNC_SERVER = True
USE_RX_CALLBACK = True


class HardwareConfig:
    CAN_BUS_ID = 1

    # Background service interval.
    PERIODIC_INTERVAL_MS = 50

    # CAN configuration.
    CAN_STATE_INTERVAL_MS = 100

    # AFE configuration.
    AFE_ID_MIN = 1
    AFE_ID_MAX = 99

    # Application-level TX spacing.
    TX_DELAY_MS = 1

    # Software RX queue capacity.
    RX_BUFFER_LEN = 32

    # Payload size for classic CAN.
    CAN_PAYLOAD_MAX_LEN = 8


# ------------------------------------------------------------
# Task supervision
# ------------------------------------------------------------

async def supervised_task(name, coroutine_factory, restart_delay_ms=100):
    """
    Keep an important service alive.

    If its coroutine exits because of an exception, log the exception and
    restart it instead of silently losing the service.
    """

    while True:
        try:
            await coroutine_factory()

        except uasyncio.CancelledError:
            raise

        except Exception as e:
            print(
                "TASK CRASHED: {}: {}".format(
                    name,
                    e,
                )
            )

            try:
                sys.print_exception(e)
            except Exception:
                pass

            await uasyncio.sleep_ms(restart_delay_ms)


# ------------------------------------------------------------
# Periodic services
# ------------------------------------------------------------

async def periodic_tasks_loop(logger):
    """
    Feed watchdog, service logger/print queues and periodically collect GC.
    """

    await p.print(
        "Periodic background task started."
    )

    gc_counter = 0

    while True:
        # Feed before the services.
        wdt.feed()

        # Service buffered filesystem logging.
        await logger.machine()

        # Service buffered printing.
        await p.machine()

        # Feed again after service work.
        wdt.feed()

        gc_counter += 1

        if gc_counter >= 100:
            gc.collect()
            gc_counter = 0

        await uasyncio.sleep_ms(
            HardwareConfig.PERIODIC_INTERVAL_MS
        )


# ------------------------------------------------------------
# Optional interactive REPL
# ------------------------------------------------------------

async def async_repl(user_globals):
    """
    Optional non-blocking interactive REPL.

    Not enabled by default.
    """

    import select

    print(
        "Async REPL initialized. "
        "Type 'exit()' or 'quit()' to detach."
    )

    line = ""

    while True:

        if sys.stdin in select.select(
            [sys.stdin],
            [],
            [],
            0
        )[0]:

            char = sys.stdin.read(1)

            if char in ("\n", "\r"):
                cmd = line.strip()

                if cmd in ("exit()", "quit()"):
                    print("Exiting REPL session.")
                    return

                if cmd:
                    try:
                        result = eval(
                            cmd,
                            user_globals,
                        )

                        if result is not None:
                            print(repr(result))

                    except SyntaxError:
                        try:
                            exec(
                                cmd,
                                user_globals,
                            )

                        except Exception as e:
                            print(
                                "Exec Error:",
                                e,
                            )

                    except Exception as e:
                        print(
                            "Eval Error:",
                            e,
                        )

                line = ""
                print(
                    ">>> ",
                    end=""
                )

            elif char == "\x7f":
                if line:
                    line = line[:-1]
                    print(
                        "\b \b",
                        end=""
                    )

            elif char == "\x1b":
                # Consume common ANSI escape sequence.
                try:
                    if sys.stdin.read(1) == "[":
                        sys.stdin.read(1)
                except Exception:
                    pass

            else:
                line += char
                print(
                    char,
                    end=""
                )

        await uasyncio.sleep_ms(50)


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

async def main():

    await p.print(
        "Initializing system components..."
    )

    # --------------------------------------------------------
    # CAN hardware
    # --------------------------------------------------------

    can_bus = pyb.CAN(
        HardwareConfig.CAN_BUS_ID
    )

    # --------------------------------------------------------
    # Logger
    # --------------------------------------------------------

    logger = JSONLogger(
        keep_file_open=True
    )

    # --------------------------------------------------------
    # HUB / CAN initialization
    # --------------------------------------------------------

    can, hub, rx_device_can = await initialize_can_hub(
        can_bus=can_bus,
        logger=logger,
        use_rxcallback=USE_RX_CALLBACK,
        use_automatic_restart=True,
    )

    # --------------------------------------------------------
    # HUB configuration
    # --------------------------------------------------------

    hub.discovery_active = True
    hub.rx_process_active = True

    hub.use_tx_delay = True
    hub.tx_delay_ms = HardwareConfig.TX_DELAY_MS

    hub.afe_manage_active = True
    hub.afe_id_min = HardwareConfig.AFE_ID_MIN
    hub.afe_id_max = HardwareConfig.AFE_ID_MAX

    await p.print(
        "HUB initialized and configured."
    )

    # --------------------------------------------------------
    # Configure CAN device
    # --------------------------------------------------------

    rx_device_can.state_check_interval_ms = (
        HardwareConfig.CAN_STATE_INTERVAL_MS
    )

    rx_device_can.rx_poll_interval_ms = (
        HardwareConfig.PERIODIC_INTERVAL_MS
    )

    # --------------------------------------------------------
    # Create application tasks
    # --------------------------------------------------------

    tasks = []

    # HUB main processing.
    tasks.append(
        uasyncio.create_task(
            supervised_task(
                "HUB",
                hub.main_loop,
            )
        )
    )

    # CAN RX + CAN state monitoring.
    tasks.append(
        uasyncio.create_task(
            supervised_task(
                "CAN",
                rx_device_can.main_loop,
            )
        )
    )

    # Watchdog / logging / GC.
    tasks.append(
        uasyncio.create_task(
            supervised_task(
                "PERIODIC",
                lambda: periodic_tasks_loop(logger),
            )
        )
    )

    # --------------------------------------------------------
    # Async web server
    # --------------------------------------------------------

    server = None

    if USE_ASYNC_SERVER:
        server = AsyncWebServer(hub)

        tasks.append(
            uasyncio.create_task(
                supervised_task(
                    "WEB SERVER",
                    server.start,
                )
            )
        )

        await p.print(
            "Async Web Server task started."
        )

    # --------------------------------------------------------
    # Optional REPL
    # --------------------------------------------------------

    # Uncomment if required.
    #
    # repl_globals = {
    #     "hub": hub,
    #     "can": can,
    #     "rx_device_can": rx_device_can,
    #     "logger": logger,
    #     "p": p,
    # }
    #
    # tasks.append(
    #     uasyncio.create_task(
    #         supervised_task(
    #             "REPL",
    #             lambda: async_repl(repl_globals),
    #         )
    #     )
    # )

    await p.print(
        "All runtime tasks started."
    )

    # --------------------------------------------------------
    # Keep all tasks alive
    # --------------------------------------------------------

    # These are supervised indefinitely, so gather() normally
    # never returns.
    await uasyncio.gather(*tasks)


# ------------------------------------------------------------
# Boot
# ------------------------------------------------------------

if __name__ == "__main__":

    try:
        uasyncio.run(main())

    except KeyboardInterrupt:
        print(
            "\nSystem execution interrupted by user."
        )

    except Exception as e:
        print(
            "\nFATAL SYSTEM ERROR:",
            e,
        )

        try:
            sys.print_exception(e)
        except Exception:
            pass