try:
    import pyb
    import micropython
    import uasyncio
except Import:
    import asyncio as uasyncio

from my_utilities import p
from my_utilities import millis
from my_utilities import is_timeout
from my_utilities import is_delay


class RxDeviceCAN:
    def __init__(self, can_bus, use_rxcallback=True, buffer_max_len=int(32*4), payload_max_len=int(8*4)):
        self.can_bus: pyb.CAN = can_bus
        self.use_rxcallback = use_rxcallback
        self.rx_timeout_ms = 5000
        self.rx_message_buffer_max_len = buffer_max_len
        self.rx_message_buffer_head = 0
        self.rx_message_buffer_tail = 0
        self.rx_message_buffer = [
            [0, 0, 0, memoryview(bytearray(payload_max_len))]
            for _ in range(self.rx_message_buffer_max_len)
        ]

        self.running = True
        self.yielld_ms = 10
        self.error_yielld_ms = 100
        self.irq_flag = False

        # PRE-BIND references during __init__ to prevent heap allocation inside IRQs
        self._send_ref = self._send
        self.handle_can_rx_ref = self.handle_can_rx
        self.handle_can_rx_irq_ref = self.handle_can_rx_irq

        if self.use_rxcallback:
            # Pass the pre-bound method reference directly
            self.can_bus.rxcallback(0, self.handle_can_rx_irq_ref)

    # Pre-allocated log formatting templates
    _ERR_FMT = "Error in RxDeviceCAN._send (scheduled for %d): %s"
    _ERR_SCHED_FMT = "Error during micropython.schedule in RxDeviceCAN.send: %s"
    _ERR_TIMEOUT_FMT = "Timeout: RxDeviceCAN failed to schedule send to %d within %dms"
    _ERR_RX_FMT = "handle_can_rx: %s"
    _ERR_SCHED_RX_FMT = "RxDeviceCAN._poll_and_schedule_rx: Error scheduling handle_can_rx: %s"
    _MSG_STOPPED = "RxDeviceCAN.main_loop: CAN BUS STOPPED"

    @micropython.native
    def _log(self, msg):
        """Helper to safely execute async p.print from non-async contexts."""
        try:
            # uasyncio.create_task(p.print(msg))
            print(msg) # use standard print
        except Exception:
            pass

    @micropython.native
    def _send(self, args_tuple):
        toSend, can_address, bus_timeout_ms = args_tuple
        try:
            self.can_bus.send(toSend, can_address, timeout=bus_timeout_ms)
        except Exception as e:
            self._log(self._ERR_FMT % (can_address, e))

    async def send(self, toSend: bytearray, can_address, timeout_ms):
        sleep = uasyncio.sleep_ms

        args = (toSend, can_address, timeout_ms)
        timestamp_ms = millis()

        while True:
            try:
                micropython.schedule(self._send_ref, args)
                return None
            except RuntimeError:
                pass
            except Exception as e:
                self._log(self._ERR_SCHED_FMT % e)

            if is_timeout(timestamp_ms, timeout_ms):
                self._log(self._ERR_TIMEOUT_FMT % (can_address, timeout_ms))
                return -1

            await sleep(1)

    @micropython.native
    def get(self, out_msg=None):
        tail = self.rx_message_buffer_tail
        if self.rx_message_buffer_head == tail:
            return None

        src_slot = self.rx_message_buffer[tail]

        self.rx_message_buffer_tail = (tail + 1) % self.rx_message_buffer_max_len

        if out_msg is not None:
            out_msg[0] = src_slot[0]
            out_msg[1] = src_slot[1]
            out_msg[2] = src_slot[2]
            payload_len = len(src_slot[3])
            out_msg[3][:payload_len] = src_slot[3]
            return out_msg

        return [src_slot[0], src_slot[1], src_slot[2], bytearray(src_slot[3])]

    @micropython.native
    def handle_can_rx(self, _=None):
        try:
            can_bus = self.can_bus
            buffer = self.rx_message_buffer
            max_len = self.rx_message_buffer_max_len
            head = self.rx_message_buffer_head
            tail = self.rx_message_buffer_tail
            timeout = self.rx_timeout_ms

            while can_bus.any(0):
                can_bus.recv(0, buffer[head], timeout=timeout)
                head = (head + 1) % max_len

                if head == tail:
                    tail = (tail + 1) % max_len

            self.rx_message_buffer_head = head
            self.rx_message_buffer_tail = tail

        except Exception as e:
            self._log(self._ERR_RX_FMT % e)

    @micropython.native
    def handle_can_rx_irq(self, bus, reason=None):
        try:
            # Use pre-bound reference to prevent allocation in IRQ
            micropython.schedule(self.handle_can_rx_ref, 0)
        except RuntimeError:
            pass

    @micropython.native
    def _poll_and_schedule_rx(self):
        if self.can_bus.any(0):
            try:
                micropython.schedule(self.handle_can_rx_ref, 0)
            except RuntimeError:
                pass
            except Exception as e_sched:
                self._log(self._ERR_SCHED_RX_FMT % e_sched)
    
    async def main_loop(self, reason=None):
        while self.running:
            state = self.can_bus.state()
            if state == pyb.CAN.STOPPED:
                self._log(self._MSG_STOPPED)
            # Fix: state > 0 triggers on normal state 1 (ERROR_ACTIVE). Only alert on actual warning/bus-off state >= 2.
            elif state >= pyb.CAN.ERROR_WARNING:
                self._log("RxDeviceCAN.main_loop: CAN BUS ERROR state: %d" % state)
                await uasyncio.sleep_ms(self.error_yielld_ms)

            # Polling mechanism
            if not self.use_rxcallback:
                self._poll_and_schedule_rx()

            await uasyncio.sleep_ms(self.yielld_ms)

    @micropython.native
    def state(self):
        return self.can_bus.state()

    @micropython.native
    def restart(self):
        self.can_bus.restart()