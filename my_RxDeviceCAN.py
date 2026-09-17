# candevice.py

import pyb
import uasyncio

from my_utilities import millis, is_timeout


class RxDeviceCAN:
    """
    Async CAN device.

    RX:
        CAN IRQ -> ThreadSafeFlag -> asyncio task -> software ring buffer

    TX:
        asyncio task -> CAN hardware TX buffer

    The class intentionally does not use micropython.schedule() for normal
    CAN RX/TX traffic.
    """

    # Your original firmware/API uses:
    #   [id, rtr, fmi, payload]
    #
    # Newer MicroPython pyb.CAN versions may use:
    #   [id, extframe, rtr, fmi, payload]
    #
    # This implementation preserves your existing 4-element interface.
    RECV_FORMAT_4 = 4

    def __init__(
        self,
        can_bus,
        use_rxcallback=True,
        buffer_max_len=32,
        payload_max_len=8,
    ):
        self.can_bus = can_bus
        self.use_rxcallback = use_rxcallback

        self.rx_timeout_ms = 0

        # Allocate one extra physical slot so the requested buffer_max_len
        # represents the actual number of messages that can be stored.
        self.rx_buffer_capacity = buffer_max_len
        self.rx_buffer_slots = buffer_max_len + 1

        self.rx_message_buffer_head = 0
        self.rx_message_buffer_tail = 0

        self.rx_message_buffer = [
            [
                0,
                0,
                0,
                memoryview(bytearray(payload_max_len)),
            ]
            for _ in range(self.rx_buffer_slots)
        ]

        self.running = True

        # CAN state monitoring.
        self.state_check_interval_ms = 100
        self._last_can_state = None

        # RX polling interval used only if callback mode is disabled.
        self.rx_poll_interval_ms = 10

        # ThreadSafeFlag is safe to set from IRQ context.
        self._rx_flag = uasyncio.ThreadSafeFlag()

        # Pre-bind references during normal Python execution.
        # This avoids creating bound-method objects inside the IRQ.
        self._rx_irq_ref = self.handle_can_rx_irq
        self._rx_flag_set_ref = self._rx_flag.set

        if self.use_rxcallback:
            self.can_bus.rxcallback(0, self._rx_irq_ref)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(self, msg):
        try:
            print(msg)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # TX
    # ------------------------------------------------------------------

    async def send(self, toSend: bytearray, can_address, timeout_ms):
        """
        Try to put a CAN frame into the hardware TX queue.

        Returns:
            None on success
            -1 on timeout/failure

        IMPORTANT:
            This does NOT wait until the frame has physically completed
            transmission on the CAN bus.

            timeout=0 is intentional: it prevents a blocking CAN operation
            from stopping the asyncio scheduler.
        """

        start_ms = millis()

        while True:
            try:
                self.can_bus.send(
                    toSend,
                    can_address,
                    timeout=0,
                )
                return None

            except Exception as e:
                # If timeout is zero/negative, do not retry forever.
                if timeout_ms <= 0:
                    self._log(
                        "RxDeviceCAN.send: CAN send failed: %s" % e
                    )
                    return -1

                if is_timeout(start_ms, timeout_ms):
                    self._log(
                        "RxDeviceCAN.send: timeout sending CAN ID "
                        "%d after %dms: %s"
                        % (can_address, timeout_ms, e)
                    )
                    return -1

                # Hardware TX buffers may be temporarily full.
                await uasyncio.sleep_ms(1)

    # ------------------------------------------------------------------
    # RX IRQ
    # ------------------------------------------------------------------

    def handle_can_rx_irq(self, bus, reason=None):
        """
        Very small IRQ handler.

        Do not touch the software RX ring buffer here.
        Do not print here.
        Do not allocate here.

        Just wake the asyncio RX worker.
        """
        try:
            self._rx_flag_set_ref()
        except Exception:
            # Emergency exception buffer is allocated in main.py.
            pass

    # ------------------------------------------------------------------
    # RX buffer
    # ------------------------------------------------------------------

    def _drain_rx(self):
        """
        Drain the hardware CAN FIFO into the preallocated software ring
        buffer.

        This function runs in normal asyncio/task context, never directly
        from the CAN IRQ.
        """

        can_bus = self.can_bus
        buffer = self.rx_message_buffer
        buffer_slots = self.rx_buffer_slots

        head = self.rx_message_buffer_head
        tail = self.rx_message_buffer_tail

        while can_bus.any(0):
            slot = buffer[head]

            # timeout=0 keeps this operation non-blocking.
            can_bus.recv(
                0,
                slot,
                timeout=0,
            )

            head = (head + 1) % buffer_slots

            # Ring full: discard the oldest message.
            if head == tail:
                tail = (tail + 1) % buffer_slots

        # These are only modified by this asyncio worker and get().
        self.rx_message_buffer_head = head
        self.rx_message_buffer_tail = tail

    async def get(self, out_msg=None):
        """
        Retrieve one message from the software RX buffer.

        Existing interface is retained:

            [id, rtr, fmi, payload]

        Returns:
            None if no message is available.
            out_msg if supplied.
            Otherwise a newly allocated list/bytearray.
        """

        tail = self.rx_message_buffer_tail
        head = self.rx_message_buffer_head

        if head == tail:
            return None

        src_slot = self.rx_message_buffer[tail]

        self.rx_message_buffer_tail = (
            (tail + 1) % self.rx_buffer_slots
        )

        if out_msg is not None:
            out_msg[0] = src_slot[0]
            out_msg[1] = src_slot[1]
            out_msg[2] = src_slot[2]

            payload_len = len(src_slot[3])
            out_msg[3][:payload_len] = src_slot[3]

            return out_msg

        return [
            src_slot[0],
            src_slot[1],
            src_slot[2],
            bytearray(src_slot[3]),
        ]

    # ------------------------------------------------------------------
    # Main CAN worker
    # ------------------------------------------------------------------

    async def main_loop(self, reason=None):
        """
        Combined CAN RX worker + CAN state monitor.

        In callback mode:
            waits for ThreadSafeFlag from the CAN IRQ

        Without callback mode:
            periodically polls CAN.any()
        """

        can_bus = self.can_bus
        sleep_ms = uasyncio.sleep_ms

        can_stopped = pyb.CAN.STOPPED
        can_warning = pyb.CAN.ERROR_WARNING

        state_interval = self.state_check_interval_ms
        poll_interval = self.rx_poll_interval_ms

        while self.running:

            # ----------------------------------------------------------
            # Wait for RX activity
            # ----------------------------------------------------------

            if self.use_rxcallback:
                try:
                    await uasyncio.wait_for_ms(
                        self._rx_flag.wait(),
                        state_interval,
                    )
                except uasyncio.TimeoutError:
                    pass

            else:
                await sleep_ms(poll_interval)

            # ----------------------------------------------------------
            # Drain all currently queued CAN messages
            # ----------------------------------------------------------

            if can_bus.any(0):
                try:
                    self._drain_rx()
                except Exception as e:
                    self._log(
                        "RxDeviceCAN.main_loop: RX error: %s" % e
                    )

                    # Avoid a tight failure loop.
                    await sleep_ms(10)

            # ----------------------------------------------------------
            # CAN state monitoring
            # ----------------------------------------------------------

            try:
                state = can_bus.state()

                if state != self._last_can_state:
                    self._last_can_state = state

                    if state == can_stopped:
                        self._log(
                            "RxDeviceCAN: CAN BUS STOPPED"
                        )

                    elif state >= can_warning:
                        self._log(
                            "RxDeviceCAN: CAN BUS ERROR state: %d"
                            % state
                        )

            except Exception as e:
                self._log(
                    "RxDeviceCAN: CAN state error: %s" % e
                )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def state(self):
        return self.can_bus.state()

    def restart(self):
        self.can_bus.restart()

    def stop(self):
        self.running = False

    def start(self):
        self.running = True