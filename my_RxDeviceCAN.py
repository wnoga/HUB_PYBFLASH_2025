# candevice.py

import pyb
import uasyncio

from my_utilities import millis, is_timeout


class RxDeviceCAN:
    """
    Async CAN device.

    RX:
        CAN IRQ -> ThreadSafeFlag -> asyncio task -> software RX ring

    TX:
        asyncio task -> CAN hardware TX queue

    Diagnostics:
        CAN.state() + CAN.info()

    The RX/TX paths do not use micropython.schedule().
    """

    def __init__(
        self,
        can_bus,
        use_rxcallback=True,
        buffer_max_len=32,
        payload_max_len=8,
    ):
        self.can_bus = can_bus
        self.use_rxcallback = use_rxcallback

        # --------------------------------------------------------
        # RX software ring
        # --------------------------------------------------------

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

        # --------------------------------------------------------
        # Runtime
        # --------------------------------------------------------

        self.running = True

        self.state_check_interval_ms = 100
        self.rx_poll_interval_ms = 10

        self._last_can_state = None

        # Log repeated warning diagnostics at most this often.
        self.warning_log_interval_ms = 5000
        self._last_warning_log_ms = 0

        # --------------------------------------------------------
        # CAN diagnostics
        #
        # Reuse this list so CAN.info() does not allocate every time.
        #
        # [0] TEC
        # [1] REC
        # [2] warning count
        # [3] passive count
        # [4] bus-off count
        # [5] pending TX
        # [6] pending RX FIFO0
        # [7] pending RX FIFO1
        # --------------------------------------------------------

        self._can_info = [0] * 8

        # --------------------------------------------------------
        # RX IRQ synchronization
        # --------------------------------------------------------

        self._rx_flag = uasyncio.ThreadSafeFlag()

        # Pre-bind references so IRQ does not create bound methods.
        self._rx_irq_ref = self.handle_can_rx_irq
        self._rx_flag_set_ref = self._rx_flag.set

        if self.use_rxcallback:
            self.can_bus.rxcallback(
                0,
                self._rx_irq_ref,
            )

    # ============================================================
    # Logging
    # ============================================================

    def _log(self, msg):
        try:
            print(msg)
        except Exception:
            pass

    # ============================================================
    # CAN diagnostics
    # ============================================================

    def _read_can_info(self):
        """
        Read CAN diagnostics into the preallocated list.

        Returns the same list every time.
        """

        info = self._can_info

        try:
            self.can_bus.info(info)
            return info

        except TypeError:
            # Compatibility with firmware that doesn't accept
            # the destination list argument.
            result = self.can_bus.info()

            for i in range(8):
                info[i] = result[i]

            return info

    def _log_can_state_change(self, state, info):
        tec = info[0]
        rec = info[1]
        warning_count = info[2]
        passive_count = info[3]
        bus_off_count = info[4]
        pending_tx = info[5]
        pending_rx0 = info[6]
        pending_rx1 = info[7]

        if state == pyb.CAN.STOPPED:
            self._log(
                "CAN STOPPED"
            )
            return

        if state == pyb.CAN.ERROR_ACTIVE:
            self._log(
                "CAN ACTIVE: "
                "TEC=%d REC=%d TX=%d RX0=%d RX1=%d"
                % (
                    tec,
                    rec,
                    pending_tx,
                    pending_rx0,
                    pending_rx1,
                )
            )
            return

        if state == pyb.CAN.ERROR_WARNING:
            self._log(
                "CAN WARNING: "
                "TEC=%d REC=%d "
                "warn=%d passive=%d busoff=%d "
                "TX=%d RX0=%d RX1=%d"
                % (
                    tec,
                    rec,
                    warning_count,
                    passive_count,
                    bus_off_count,
                    pending_tx,
                    pending_rx0,
                    pending_rx1,
                )
            )
            return

        if state == pyb.CAN.ERROR_PASSIVE:
            self._log(
                "CAN PASSIVE: "
                "TEC=%d REC=%d "
                "warn=%d passive=%d busoff=%d "
                "TX=%d RX0=%d RX1=%d"
                % (
                    tec,
                    rec,
                    warning_count,
                    passive_count,
                    bus_off_count,
                    pending_tx,
                    pending_rx0,
                    pending_rx1,
                )
            )
            return

        if state == pyb.CAN.BUS_OFF:
            self._log(
                "CAN BUS-OFF: "
                "TEC=%d REC=%d "
                "warn=%d passive=%d busoff=%d "
                "TX=%d RX0=%d RX1=%d"
                % (
                    tec,
                    rec,
                    warning_count,
                    passive_count,
                    bus_off_count,
                    pending_tx,
                    pending_rx0,
                    pending_rx1,
                )
            )
            return

        self._log(
            "CAN STATE %d: "
            "TEC=%d REC=%d TX=%d RX0=%d RX1=%d"
            % (
                state,
                tec,
                rec,
                pending_tx,
                pending_rx0,
                pending_rx1,
            )
        )

    def _monitor_can_state(self):
        """
        Read state and error counters.

        Logs:
            - every state transition
            - repeated WARNING diagnostics every warning_log_interval_ms
            - every transition to PASSIVE/BUS-OFF
        """

        try:
            state = self.can_bus.state()
            info = self._read_can_info()

            now_ms = millis()

            state_changed = (
                state != self._last_can_state
            )

            if state_changed:
                self._last_can_state = state
                self._last_warning_log_ms = now_ms

                self._log_can_state_change(
                    state,
                    info,
                )
                return

            # ----------------------------------------------------
            # State remains WARNING.
            #
            # Log diagnostics periodically so we can see whether
            # TEC/REC is getting better or worse.
            # ----------------------------------------------------

            if state == pyb.CAN.ERROR_WARNING:

                if is_timeout(
                    self._last_warning_log_ms,
                    self.warning_log_interval_ms,
                ):
                    self._last_warning_log_ms = now_ms

                    tec = info[0]
                    rec = info[1]
                    warning_count = info[2]
                    passive_count = info[3]
                    bus_off_count = info[4]
                    pending_tx = info[5]
                    pending_rx0 = info[6]
                    pending_rx1 = info[7]

                    self._log(
                        "CAN WARNING still active: "
                        "TEC=%d REC=%d "
                        "warn=%d passive=%d busoff=%d "
                        "TX=%d RX0=%d RX1=%d"
                        % (
                            tec,
                            rec,
                            warning_count,
                            passive_count,
                            bus_off_count,
                            pending_tx,
                            pending_rx0,
                            pending_rx1,
                        )
                    )

            # ----------------------------------------------------
            # PASSIVE/BUS-OFF should remain visible.
            # ----------------------------------------------------

            elif state == pyb.CAN.ERROR_PASSIVE:
                if is_timeout(
                    self._last_warning_log_ms,
                    self.warning_log_interval_ms,
                ):
                    self._last_warning_log_ms = now_ms

                    self._log(
                        "CAN PASSIVE still active: "
                        "TEC=%d REC=%d TX=%d RX0=%d RX1=%d"
                        % (
                            info[0],
                            info[1],
                            info[5],
                            info[6],
                            info[7],
                        )
                    )

            elif state == pyb.CAN.BUS_OFF:
                if is_timeout(
                    self._last_warning_log_ms,
                    self.warning_log_interval_ms,
                ):
                    self._last_warning_log_ms = now_ms

                    self._log(
                        "CAN BUS-OFF still active: "
                        "TEC=%d REC=%d TX=%d RX0=%d RX1=%d"
                        % (
                            info[0],
                            info[1],
                            info[5],
                            info[6],
                            info[7],
                        )
                    )

        except Exception as e:
            self._log(
                "RxDeviceCAN: CAN diagnostics error: %s"
                % e
            )

    # ============================================================
    # TX
    # ============================================================

    async def send(
        self,
        toSend: bytearray,
        can_address,
        timeout_ms,
    ):
        """
        Put a CAN frame into the hardware TX queue.

        Returns:
            None -> accepted by CAN controller
            -1   -> timeout/failure

        timeout=0 is intentional and prevents a blocking CAN send
        from stalling the asyncio scheduler.

        This means SUCCESS means:
            "accepted by CAN TX queue"

        It does not mean:
            "frame has physically completed transmission".
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

            except OSError:

                if timeout_ms <= 0:
                    return -1

                if is_timeout(
                    start_ms,
                    timeout_ms,
                ):
                    self._log(
                        "CAN TX timeout: "
                        "ID=%d timeout=%dms"
                        % (
                            can_address,
                            timeout_ms,
                        )
                    )
                    return -1

                await uasyncio.sleep_ms(1)

            except Exception as e:

                # Programming/configuration errors should not
                # silently turn into endless TX retries.
                self._log(
                    "CAN TX exception: "
                    "ID=%d error=%s"
                    % (
                        can_address,
                        e,
                    )
                )
                return -1

    # ============================================================
    # RX IRQ
    # ============================================================

    def handle_can_rx_irq(
        self,
        bus,
        reason=None,
    ):
        """
        IRQ handler.

        Keep this extremely small.
        """

        try:
            self._rx_flag_set_ref()
        except Exception:
            pass

    # ============================================================
    # RX drain
    # ============================================================

    def _drain_rx(self):
        """
        Drain hardware FIFO into the software ring buffer.

        This runs only from asyncio/task context.
        """

        can_bus = self.can_bus

        buffer = self.rx_message_buffer
        buffer_slots = self.rx_buffer_slots

        head = self.rx_message_buffer_head
        tail = self.rx_message_buffer_tail

        while can_bus.any(0):

            slot = buffer[head]

            can_bus.recv(
                0,
                slot,
                timeout=0,
            )

            head = (
                head + 1
            ) % buffer_slots

            # Buffer full -> discard oldest.
            if head == tail:
                tail = (
                    tail + 1
                ) % buffer_slots

        self.rx_message_buffer_head = head
        self.rx_message_buffer_tail = tail

    # ============================================================
    # RX get
    # ============================================================

    async def get(self, out_msg=None):
        """
        Retrieve one message.

        Interface:
            [id, rtr, fmi, payload]

        Returns:
            None if empty.
            out_msg if provided.
            Otherwise a newly allocated result.
        """

        tail = self.rx_message_buffer_tail
        head = self.rx_message_buffer_head

        if head == tail:
            return None

        src_slot = (
            self.rx_message_buffer[tail]
        )

        self.rx_message_buffer_tail = (
            tail + 1
        ) % self.rx_buffer_slots

        if out_msg is not None:

            out_msg[0] = src_slot[0]
            out_msg[1] = src_slot[1]
            out_msg[2] = src_slot[2]

            payload_len = len(
                src_slot[3]
            )

            out_msg[3][:payload_len] = (
                src_slot[3]
            )

            return out_msg

        return [
            src_slot[0],
            src_slot[1],
            src_slot[2],
            bytearray(src_slot[3]),
        ]

    # ============================================================
    # Main loop
    # ============================================================

    async def main_loop(self, reason=None):
        """
        RX worker + CAN diagnostics.

        Callback mode:
            IRQ -> ThreadSafeFlag -> worker

        Polling mode:
            periodic CAN.any()
        """

        can_bus = self.can_bus

        sleep_ms = uasyncio.sleep_ms

        state_interval = (
            self.state_check_interval_ms
        )

        poll_interval = (
            self.rx_poll_interval_ms
        )

        while self.running:

            # ----------------------------------------------------
            # Wait for RX activity or timeout.
            #
            # The timeout lets us inspect CAN state even when
            # there is no RX traffic.
            # ----------------------------------------------------

            if self.use_rxcallback:

                try:
                    await uasyncio.wait_for_ms(
                        self._rx_flag.wait(),
                        state_interval,
                    )

                except uasyncio.TimeoutError:
                    pass

            else:
                await sleep_ms(
                    poll_interval
                )

            # ----------------------------------------------------
            # Drain RX FIFO completely.
            # ----------------------------------------------------

            if can_bus.any(0):

                try:
                    self._drain_rx()

                except Exception as e:
                    self._log(
                        "CAN RX error: %s"
                        % e
                    )

                    await sleep_ms(10)

            # ----------------------------------------------------
            # CAN diagnostics.
            # ----------------------------------------------------

            self._monitor_can_state()

    # ============================================================
    # Utility
    # ============================================================

    def state(self):
        return self.can_bus.state()

    def info(self):
        """
        Return the current CAN diagnostic counters.

        Returns the internal preallocated list.

        Layout:
            [TEC, REC, warning, passive, bus_off,
             pending_tx, pending_rx0, pending_rx1]
        """

        return self._read_can_info()

    def restart(self):
        self.can_bus.restart()

    def stop(self):
        self.running = False

    def start(self):
        self.running = True