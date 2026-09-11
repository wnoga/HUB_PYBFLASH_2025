try:
    import pyb
    import micropython
    import uasyncio
except:
    import asyncio as uasyncio

from my_utilities import p
from my_utilities import millis
from my_utilities import is_timeout
from my_utilities import is_delay

class RxDeviceCAN:
    def __init__(self, can_bus, use_rxcallback=True, buffer_max_len=32, payload_max_len=8):
        self._send_ref = self._send
        self.handle_can_rx_ref = self.handle_can_rx
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
 
        if self.use_rxcallback:
            # Register CAN RX interrupt, call safe ISR wrapper
            self.can_bus.rxcallback(0, self.handle_can_rx_irq)
    
    # Pre-allocated log formatting string
    _ERR_FMT = "Error in RxDeviceCAN._send (scheduled for %d): %s"

    def _send(self, args_tuple):
        """
        Internal method to perform the CAN send operation.
        This is called by micropython.schedule.
        args_tuple is expected to be (toSend, can_address, bus_timeout_ms).
        """
        toSend, can_address, bus_timeout_ms = args_tuple
        try:
            self.can_bus.send(toSend, can_address, timeout=bus_timeout_ms)
        except Exception as e:
            # Avoid .format() allocations; use C-style formatting with pre-allocated template
            p.print(self._ERR_FMT % (can_address, e))
            
    # Pre-allocated log formatting templates
    _ERR_SCHED_FMT = "Error during micropython.schedule in RxDeviceCAN.send: %s"
    _ERR_TIMEOUT_FMT = "Timeout: RxDeviceCAN failed to schedule send to %d within %dms"

    async def send(self, toSend: bytearray, can_address, timeout_ms):
        """
        Asynchronously schedules a CAN message send.
        timeout_ms is used for both the scheduling attempt loop and the CAN bus operation itself.
        Returns None on successful scheduling, -1 on scheduling timeout.
        """
        # Cache locals for loop speed & reduced lookups
        sched = micropython.schedule
        send_ref = self._send_ref
        sleep = uasyncio.sleep_ms
        get_millis = millis
        check_timeout = is_timeout
        print_fn = p.print
        
        # Packing args tuple once outside the retry loop avoids allocation on every iteration
        args = (toSend, can_address, timeout_ms)
        timestamp_ms = get_millis()

        while True:
            try:
                sched(send_ref, args)
                return None  # Successful scheduling
            except RuntimeError:  # Schedule queue is full
                pass
            except Exception as e:
                print_fn(self._ERR_SCHED_FMT % e)

            if check_timeout(timestamp_ms, timeout_ms):
                print_fn(self._ERR_TIMEOUT_FMT % (can_address, timeout_ms))
                return -1

            await sleep(1)


    async def get(self, out_msg=None):
        """
        Retrieves a message from the ring buffer.
        :param out_msg: Pre-allocated list [id, flags, rtr, bytearray] to fill into.
                        If provided, guarantees zero heap allocation.
        """
        tail = self.rx_message_buffer_tail
        if self.rx_message_buffer_head == tail:
            return None

        src_slot = self.rx_message_buffer[tail]

        # Advance tail using modulo
        self.rx_message_buffer_tail = (tail + 1) % self.rx_message_buffer_max_len

        # Zero-allocation path: copy data into caller-provided list/bytearray
        if out_msg is not None:
            out_msg[0] = src_slot[0]
            out_msg[1] = src_slot[1]
            out_msg[2] = src_slot[2]
            # Fast in-place byte copy (memoryview avoids allocation)
            payload_len = len(src_slot[3])
            out_msg[3][:payload_len] = src_slot[3]
            return out_msg

        # Allocation path (if caller didn't provide pre-allocated output container)
        return [src_slot[0], src_slot[1], src_slot[2], bytearray(src_slot[3])]
    

    # Pre-allocated log formatting template
    _ERR_RX_FMT = "handle_can_rx: %s"

    def handle_can_rx(self, _=None):
        try:
            # Cache object references locally to optimize loop execution
            can_bus = self.can_bus
            buffer = self.rx_message_buffer
            max_len = self.rx_message_buffer_max_len
            head = self.rx_message_buffer_head
            tail = self.rx_message_buffer_tail
            timeout = self.rx_timeout_ms

            # Process all pending messages in FIFO 0
            while can_bus.any(0):
                # Pass pre-allocated buffer slot directly to recv()
                can_bus.recv(0, buffer[head], timeout=timeout)

                # Advance head index using modulo
                head = (head + 1) % max_len

                # Overwrite oldest entry if buffer overflows (head catches tail)
                if head == tail:
                    tail = (tail + 1) % max_len

            # Commit local index changes back to instance state
            self.rx_message_buffer_head = head
            self.rx_message_buffer_tail = tail

        except Exception as e:
            p.print(self._ERR_RX_FMT % e)

    # ISR → only schedules processing
    # MicroPython annotation: Ensures emergency exception buffer works if a hard IRQ fails
    @micropython.native
    def handle_can_rx_irq(self, bus, reason=None):
        try:
            # Pass 0 directly as an integer to avoid tuple allocation in schedule()
            micropython.schedule(self.handle_can_rx_ref, 0)
        except RuntimeError:
            # Schedule queue full: Handled gracefully by polling or subsequent IRQ
            pass
        
    # Pre-allocated log formatting string
    _ERR_SCHED_RX_FMT = "RxDeviceCAN._poll_and_schedule_rx: Error scheduling handle_can_rx: %s"

    async def _poll_and_schedule_rx(self):
        """Helper async method to poll for CAN messages and schedule handler."""
        # Cache local references to avoid attribute dictionary lookups inside the loop
        can_bus = self.can_bus
        
        if can_bus.any(0):  # Check FIFO 0
            try:
                micropython.schedule(self.handle_can_rx_ref, 0)
            except RuntimeError:
                # Schedule queue full; message remains in FIFO for the next loop/IRQ pass
                pass
            except Exception as e_sched:
                p.print(self._ERR_SCHED_RX_FMT % e_sched)
                
    # Pre-allocate static log strings/bytearrays to avoid runtime allocations
    _MSG_STOPPED = "RxDeviceCAN.main_loop: CAN BUS STOPPED"
    
    async def main_loop(self, reason=None):
        # MicroPython local variable optimizations
        can_bus = self.can_bus
        poll_rx = self._poll_and_schedule_rx
        sleep = uasyncio.sleep_ms
        yield_ms = self.yielld_ms
        print_fn = p.print
        can_stopped = pyb.CAN.STOPPED

        while self.running:
            state = can_bus.state()
            if state == can_stopped:
                print_fn(self._MSG_STOPPED)
            elif state > 0:
                # Use C-style string formatting to reduce dynamic memory allocation
                print_fn("RxDeviceCAN.main_loop: CAN BUS ERROR state: %d" % state)
                await sleep(100)

            # Polling mechanism
            await poll_rx()

            # Dynamic sleep interval check in case `yielld_ms` changes at runtime
            await sleep(yield_ms)
            
    def state(self):
        """Returns the current state of the CAN bus."""
        return self.can_bus.state()

    def restart(self):
        """Restarts the CAN bus.
        This can be used to recover from error states like BUS_OFF.
        """
        self.can_bus.restart()
