import pyb
import micropython
import uasyncio
# try:
from my_utilities import p
from my_utilities import millis
from my_utilities import is_timeout
from my_utilities import is_delay
# except ImportError:
#     # from micropython_sim import p
#     # from micropython_sim import millis
#     # from micropython_sim import is_timeout
#     # from micropython_sim import is_delay
#     pass


class RxDeviceCAN:
    def __init__(self, can_bus: pyb.CAN, use_rxcallback=True):
        self._send_ref = self._send
        self.handle_can_rx_ref = self.handle_can_rx
        self.can_bus: pyb.CAN = can_bus
        self.use_rxcallback = use_rxcallback
        self.rx_timeout_ms = 5000
        self.rx_message_buffer_max_len = 32
        self.rx_message_buffer_head = 0
        self.rx_message_buffer_tail = 0
        self.rx_message_buffer = [
            [0, 0, 0, memoryview(bytearray(8))]
            for _ in range(self.rx_message_buffer_max_len)
        ]

        self.running = True
        self.yielld_ms = 10
        self.error_yielld_ms = 100
        self.irq_flag = False
 
        if self.use_rxcallback:
            # Register CAN RX interrupt, call safe ISR wrapper
            self.can_bus.rxcallback(0, self.handle_can_rx_irq)
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
            p.print("Error in RxDeviceCAN._send (scheduled for {}): {}".format(can_address, e))

    async def send(self, toSend: bytearray, can_address, timeout_ms):
        """
        Asynchronously schedules a CAN message send.
        timeout_ms is used for both the scheduling attempt loop and the CAN bus operation itself.
        Returns None on successful scheduling, -1 on scheduling timeout.
        """
        timestamp_ms = millis()
        while True:
            try:
                micropython.schedule(self._send_ref, (toSend, can_address, timeout_ms))
                return None  # Successful scheduling
            except RuntimeError:  # micropython.schedule queue is full
                pass  # Will retry after a short sleep
            except Exception as e: # Other unexpected error during scheduling
                p.print("Error during micropython.schedule in RxDeviceCAN.send: {}".format(e))
                pass # Will retry
            if is_timeout(timestamp_ms, timeout_ms):
                p.print("Timeout: RxDeviceCAN failed to schedule send to {} within {}ms".format(can_address, timeout_ms))
                return -1  # Scheduling failed due to timeout
            await uasyncio.sleep_ms(1) # Yield before retrying schedule


    async def get(self):
        # while self.irq_flag:
        #     await uasyncio.sleep_ms(0) # Yield
        if self.rx_message_buffer_head == self.rx_message_buffer_tail:
            return None
        # irq_state = pyb.disable_irq()
        tmp = [self.rx_message_buffer[self.rx_message_buffer_tail][0],
            self.rx_message_buffer[self.rx_message_buffer_tail][1],
            self.rx_message_buffer[self.rx_message_buffer_tail][2],
            bytearray(self.rx_message_buffer[self.rx_message_buffer_tail][3])]
        self.rx_message_buffer_tail += 1
        if self.rx_message_buffer_tail >= self.rx_message_buffer_max_len:
            self.rx_message_buffer_tail = 0
        # pyb.enable_irq(irq_state)
        return tmp
    

    def handle_can_rx(self,_=None):
        try:
            while self.can_bus.any(0):
                self.can_bus.recv(0, self.rx_message_buffer[self.rx_message_buffer_head], timeout=self.rx_timeout_ms)
                self.rx_message_buffer_head += 1
                if self.rx_message_buffer_head >= self.rx_message_buffer_max_len:
                    self.rx_message_buffer_head = 0
                if self.rx_message_buffer_head == self.rx_message_buffer_tail:
                    self.rx_message_buffer_tail += 1
                    if self.rx_message_buffer_tail >= self.rx_message_buffer_max_len:
                        self.rx_message_buffer_tail = 0
        except Exception as e:
            p.print("handle_can_rx: {}",e)
            pass

    # def handle_can_rx_polling(self):
    #     try:
    #         if self.can_bus.any(0):
    #             self.handle_can_rx()
    #             return True
    #     except Exception as e:
    #         print("handle_can_rx_polling: {}".format(e))
    #     return False

    # # SCHEDULED version, safe in main thread
    # def handle_can_rx_polling_schedule(self, _):
    #     self.handle_can_rx_polling()

    # ISR → only schedules processing
    def handle_can_rx_irq(self, bus: pyb.CAN, reason=None):
        try:
            micropython.schedule(self.handle_can_rx_ref, 0)
        except RuntimeError:
            # This can happen if the schedule queue is full.
            # Depending on the application, you might want to log this or take other action.
            pass # Silently ignore if queue is full, as the task will be picked up by polling or next IRQ.
        # self.irq_flag = True
        # # self.handle_can_rx()
        # # while self.can_bus.any(0):
        # self.can_bus.recv(0, self.rx_message_buffer[self.rx_message_buffer_head], timeout=self.rx_timeout_ms)
        # self.rx_message_buffer_head += 1
        # if self.rx_message_buffer_head >= self.rx_message_buffer_max_len:
        #     self.rx_message_buffer_head = 0
        # if self.rx_message_buffer_head == self.rx_message_buffer_tail:
        #     self.rx_message_buffer_tail += 1
        #     if self.rx_message_buffer_tail >= self.rx_message_buffer_max_len:
        #         self.rx_message_buffer_tail = 0
        # self.irq_flag = False
        # # except:
        # #     pass  # Too many scheduled tasks or already pending

    async def _poll_and_schedule_rx(self):
        """Helper async method to poll for CAN messages and schedule handler."""
        # This method is called repeatedly by main_loop.
        # It should check once if messages are available and schedule if so.
        # The handle_can_rx method itself has a loop to drain the FIFO.
        if self.can_bus.any(0): # Check if any message is pending
            try:
                micropython.schedule(self.handle_can_rx_ref, 0)
            except RuntimeError:
                # Schedule queue is full. Message will hopefully be picked up
                # by a subsequent IRQ or this poll's next attempt.
                pass # pragma: no cover
            except Exception as e_sched:
                p.print("RxDeviceCAN._poll_and_schedule_rx: Error scheduling handle_can_rx: {}".format(e_sched)) # pragma: no cover
            # No await uasyncio.sleep_ms(1) here, main_loop handles the polling interval.

    async def main_loop(self, reason=None):
        while self.running:
            # try:
            state = self.can_bus.state()
            if state == pyb.CAN.STOPPED:
                p.print("RxDeviceCAN.main_loop: CAN BUS STOPPED")
            elif state > 0: # CAN bus error (e.g., WARNING, ERROR_PASSIVE, BUS_OFF) # pragma: no cover
                p.print("RxDeviceCAN.main_loop: CAN BUS ERROR state: {}".format(state))
                # Loop continues to monitor and allow for potential auto-restart or external restart.
                # Higher-level logic (e.g., in HUB.py) might attempt can_bus.restart().

            # Perform polling for CAN messages.
            # This acts as a primary mechanism if use_rxcallback is False,
            # or as a backup/general check if use_rxcallback is True.
            await self._poll_and_schedule_rx() # Call the simplified polling method

            # except Exception as e:
            #     p.print("RxDeviceCAN.main_loop: Exception: {}".format(e)) # pragma: no cover
            await uasyncio.sleep_ms(self.yielld_ms) # This controls the polling frequency

    def state(self):
        """Returns the current state of the CAN bus."""
        return self.can_bus.state()

    def restart(self):
        """Restarts the CAN bus.
        This can be used to recover from error states like BUS_OFF.
        """
        self.can_bus.restart()
