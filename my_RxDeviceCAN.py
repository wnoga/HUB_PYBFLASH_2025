try:
    import pyb
    import micropython
    import uasyncio
except ImportError:
    import asyncio as uasyncio

from my_utilities import p, millis, is_timeout


class RxDeviceCAN:
    def __init__(
        self,
        can_bus,
        rx_message_buffer_max_len=32,
        use_rxcallback=True,
        use_automatic_restart=True,
    ):
        self.can_bus = can_bus
        self.rx_message_buffer_max_len = rx_message_buffer_max_len
        self.rx_message_buffer_head = 0
        self.rx_message_buffer_tail = 0

        self.use_rxcallback = use_rxcallback
        self.use_automatic_restart = use_automatic_restart

        # CRITICAL: Pre-allocate exactly 4 elements per list item required by pyb.CAN.recv:
        # [id (int), is_ext (bool), rtr (bool), data (bytearray)]
        self.rx_message_buffer = [
            [0, False, False, bytearray(8)]
            for _ in range(self.rx_message_buffer_max_len)
        ]

        self.running = True
        self.yield_ms = 10
        self.error_yield_ms = 100

        # CRITICAL FIX: Pre-bind method references to prevent memory allocation in ISR context
        self.handle_can_rx_ref = self.handle_can_rx
        self.handle_can_rx_irq_ref = self.handle_can_rx_irq

        if self.use_rxcallback:
            # Attach hardware interrupt using bound reference
            self.can_bus.rxcallback(0, self.handle_can_rx_irq_ref)

    def _send(self, args_list):
        """Internal callback invoked via micropython.schedule."""
        toSend, can_address, bus_timeout_ms = args_list
        try:
            self.can_bus.send(toSend, can_address, timeout=bus_timeout_ms)
        except Exception as e:
            p.print_sync("RxDeviceCAN._send Error: %s" % e)

    async def send(self, toSend, can_address, timeout_ms=50):
        """Sends a CAN frame without locking up uasyncio if no node ACKs."""
        try:
            # Pass timeout to prevent hardware lockup when no node ACKs
            self.can_bus.send(toSend, can_address, timeout=timeout_ms)
            return 0  # Success
        except OSError as e:
            # Catch timeout (Errno 110 / ETIMEDOUT) when no device acknowledges
            return -1  # TX failed / Timeout
        except Exception as e:
            p.print_sync("RxDeviceCAN.send error: %s" % str(e))
            return -1

    async def get(self):
        """
        Retrieves the oldest received frame from the ring buffer.
        Returns [can_id, is_ext, rtr, payload_bytearray] or None if empty.
        """
        if self.rx_message_buffer_head == self.rx_message_buffer_tail:
            return None

        slot = self.rx_message_buffer[self.rx_message_buffer_tail]
        can_id, is_ext, rtr, payload = slot

        # Deep-copy payload bytearray so consumer modifications don't corrupt buffer
        copied_payload = bytearray(payload)

        # Advance tail pointer
        self.rx_message_buffer_tail = (
            self.rx_message_buffer_tail + 1
        ) % self.rx_message_buffer_max_len

        return [can_id, is_ext, rtr, copied_payload]

    def handle_can_rx(self, _=None):
        """Scheduled worker that drains hardware FIFO into ring buffer."""
        try:
            while self.can_bus.any(0):
                # Fetch pre-allocated 4-element list slot
                slot = self.rx_message_buffer[self.rx_message_buffer_head]

                # Read directly into slot using FIFO 0
                self.can_bus.recv(0, slot, timeout=0)

                next_head = (
                    self.rx_message_buffer_head + 1
                ) % self.rx_message_buffer_max_len

                # Handle ring buffer overflow
                if next_head == self.rx_message_buffer_tail:
                    self.rx_message_buffer_tail = (
                        self.rx_message_buffer_tail + 1
                    ) % self.rx_message_buffer_max_len

                self.rx_message_buffer_head = next_head

        except Exception as e:
            # Explicit string extraction for MicroPython exceptions
            err_type = e.__class__.__name__
            err_args = e.args if hasattr(e, "args") else "None"
            p.print_sync(
                "RxDeviceCAN.handle_can_rx error: Type=%s, Args=%s"
                % (err_type, err_args)
            )

    def handle_can_rx_irq(self, bus, reason=None):
        """Hard ISR callback. Defer execution to micropython scheduler."""
        try:
            micropython.schedule(self.handle_can_rx_ref, 0)
        except RuntimeError:
            pass  # Queue full; poll loop or next IRQ will handle pending frames

    async def _poll_and_schedule_rx(self):
        """Async polling check used as fallback or main receiver mechanism."""
        if self.can_bus.any(0):
            try:
                micropython.schedule(self.handle_can_rx_ref, 0)
            except RuntimeError:
                pass

    async def main_loop(self):
        """Background task for maintaining CAN interface polling."""
        while self.running:
            await self._poll_and_schedule_rx()
            await uasyncio.sleep_ms(self.yield_ms)

    def state(self):
        """Returns hardware CAN state (pyb.CAN.STOPPED, ERROR_ACTIVE, ERROR_WARNING, BUS_OFF, etc.)."""
        return self.can_bus.state()

    def restart(self):
        """Triggers a controller soft restart to clear BUS_OFF or STOPPED conditions."""
        p.print_sync("RxDeviceCAN: Initiating hardware CAN bus restart...")
        try:
            self.can_bus.restart()
            if self.use_rxcallback:
                # Re-bind interrupt after hardware reset using bound reference
                self.can_bus.rxcallback(0, self.handle_can_rx_irq_ref)
        except Exception as e:
            p.print_sync("RxDeviceCAN: Hardware restart failed: %s" % e)