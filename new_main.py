import pyb
import uasyncio as asyncio
import time
import network
import socket
import struct

from my_utilities import (
    AFECommand,
    AFECommandAverage,
    AFECommandChannel,
    AFECommandChannelMask,
    AFECommandSubdevice,
    millis,
)


# -----------------------------------------------------------------------------
# Small uasyncio-compatible bounded FIFO.
# -----------------------------------------------------------------------------
class Queue:
    def __init__(self, maxsize=32):
        if maxsize < 1:
            raise ValueError("maxsize must be > 0")
        self.maxsize = maxsize
        self._items = [None] * maxsize
        self._head = 0
        self._tail = 0
        self._count = 0
        self._not_empty = asyncio.Event()
        self._not_full = asyncio.Event()
        self._not_full.set()

    def empty(self):
        return self._count == 0

    def full(self):
        return self._count >= self.maxsize

    def qsize(self):
        return self._count

    async def put(self, item):
        while self._count >= self.maxsize:
            self._not_full.clear()
            if self._count >= self.maxsize:
                await self._not_full.wait()

        self._items[self._tail] = item
        self._tail += 1
        if self._tail >= self.maxsize:
            self._tail = 0
        self._count += 1
        self._not_empty.set()

    def put_nowait(self, item):
        if self.full():
            raise IndexError("queue is full")
        self._items[self._tail] = item
        self._tail += 1
        if self._tail >= self.maxsize:
            self._tail = 0
        self._count += 1
        self._not_empty.set()

    async def get(self):
        while self._count == 0:
            self._not_empty.clear()
            if self._count == 0:
                await self._not_empty.wait()

        item = self._items[self._head]
        self._items[self._head] = None
        self._head += 1
        if self._head >= self.maxsize:
            self._head = 0
        self._count -= 1
        self._not_full.set()
        return item

    def get_nowait(self):
        if self.empty():
            raise IndexError("queue is empty")
        item = self._items[self._head]
        self._items[self._head] = None
        self._head += 1
        if self._head >= self.maxsize:
            self._head = 0
        self._count -= 1
        self._not_full.set()
        return item


# -----------------------------------------------------------------------------
# Exceptions and command containers.
# -----------------------------------------------------------------------------
class AFETimeoutError(Exception):
    pass


class CommandStatus:
    NONE = 0
    SUCCESS = 1
    ERROR = -1


class AFECommandPayload:
    __slots__ = (
        "command",
        "frame",
        "device_id",
        "can_address",
        "timeout_ms",
        "timestamp_ms",
        "timestamp_ms_enqueued",
        "can_timeout_ms",
        "status",
        "preserve",
        "timeout_start_on_send_ms",
        "retval",
        "callback",
        "callback_error",
        "completion_event",
    )

    def __init__(
        self,
        command,
        frame,
        device_id,
        can_address,
        timeout_ms,
        timestamp_ms,
        can_timeout_ms,
        preserve,
        callback,
        callback_error,
    ):
        self.command = command
        self.frame = frame
        self.device_id = device_id
        self.can_address = can_address
        self.timeout_ms = timeout_ms
        self.timestamp_ms = timestamp_ms
        self.timestamp_ms_enqueued = timestamp_ms
        self.can_timeout_ms = can_timeout_ms
        self.status = CommandStatus.NONE
        self.preserve = preserve
        self.timeout_start_on_send_ms = None
        self.retval = None
        self.callback = callback
        self.callback_error = callback_error
        self.completion_event = asyncio.Event()


class CommandRequest:
    __slots__ = ("cmd_id", "payload", "timeout_ms", "event", "result", "exception")

    def __init__(self, cmd_id, payload=b"", timeout_ms=2000):
        self.cmd_id = cmd_id
        self.payload = payload
        self.timeout_ms = timeout_ms
        self.event = asyncio.Event()
        self.result = None
        self.exception = None


# -----------------------------------------------------------------------------
# CAN frame helpers.
# -----------------------------------------------------------------------------
def parse_can_frame(msg_id, data, expected_device_id):
    if data is None or len(data) < 2:
        return None

    device_id = (msg_id >> 2) & 0xFF
    msg_from_slave = (msg_id >> 10) & 0x01

    if msg_from_slave != 1 or device_id != expected_device_id:
        return None

    # CAN payload is already bytes/bytearray. Avoid list(bytes(...)) allocation.
    command = data[0]
    chunk_info = data[1]
    chunk_id = chunk_info & 0x0F
    max_chunks = (chunk_info >> 4) & 0x0F
    chunk_payload = data[2:]

    return (
        device_id,
        command,
        chunk_id,
        max_chunks,
        data,
        chunk_payload,
    )


def get_subdevice_ch_id(group):
    if group == "M":
        return AFECommandSubdevice.AFECommandSubdevice_master
    return AFECommandSubdevice.AFECommandSubdevice_slave


def get_t_measured_ch_id(group):
    if group == "M":
        return AFECommandChannel.AFECommandChannel_7
    return AFECommandChannel.AFECommandChannel_6


def get_u_measured_ch_id(group):
    if group == "M":
        return AFECommandChannel.AFECommandChannel_2
    return AFECommandChannel.AFECommandChannel_3


def get_i_measured_ch_id(group):
    if group == "M":
        return AFECommandChannel.AFECommandChannel_4
    return AFECommandChannel.AFECommandChannel_5


def get_general_ch_id_mask(group):
    if group == "M":
        return AFECommandChannelMask.master
    return AFECommandChannelMask.slave


def is_awaitable(value):
    return value is not None and hasattr(value, "__await__")


async def call_callback(callback, *args):
    if callback is None:
        return
    result = callback(*args)
    if is_awaitable(result):
        await result


# -----------------------------------------------------------------------------
# Hardware watchdog.
# -----------------------------------------------------------------------------
class WatchdogManager:
    """Feeds the MCU watchdog independently of CAN/network activity."""

    def __init__(self, timeout_ms=20000, feed_interval_ms=5000):
        self.feed_interval_ms = feed_interval_ms
        self.enabled = False
        self.wdt = None

        try:
            from machine import WDT
            self.wdt = WDT(timeout=timeout_ms)
            self.enabled = True
            print("Hardware watchdog enabled: {} ms".format(timeout_ms))
        except (ImportError, AttributeError):
            print("Hardware watchdog unavailable")
        except Exception as exc:
            print("Hardware watchdog init failed: {}".format(exc))

    async def run(self):
        while True:
            if self.enabled and self.wdt is not None:
                try:
                    self.wdt.feed()
                except Exception as exc:
                    print("Watchdog feed error: {}".format(exc))
            await asyncio.sleep_ms(self.feed_interval_ms)


# -----------------------------------------------------------------------------
# CAN controller.
# -----------------------------------------------------------------------------
class CANController:
    def __init__(self, can_id=1):
        self.can = pyb.CAN(can_id)
        self.can.init(
            pyb.CAN.NORMAL,
            extframe=False,
            prescaler=54,
            sjw=1,
            bs1=7,
            bs2=2,
            auto_restart=True,
        )
        self.can.setfilter(0, self.can.MASK16, 0, (0, 0, 0, 0))
        self.tx_queue = Queue(maxsize=32)
        self.rx_listeners = []

    def register_listener(self, queue):
        self.rx_listeners.append(queue)

    async def send_frame(self, msg_id, data):
        await self.tx_queue.put((msg_id, data))

    async def tx_loop(self):
        while True:
            msg_id, data = await self.tx_queue.get()
            try:
                self.can.send(data, msg_id, timeout=2)
            except Exception:
                pass
            await asyncio.sleep_ms(0)

    async def rx_loop(self):
        while True:
            if self.can.any(0):
                try:
                    msg_id, rtr, fmi, data = self.can.recv(0)
                    for queue in self.rx_listeners:
                        await queue.put((msg_id, data))
                except Exception:
                    pass
            await asyncio.sleep_ms(1)


# -----------------------------------------------------------------------------
# AFE device.
# -----------------------------------------------------------------------------
class AFEDevice:
    def __init__(
        self,
        afe_id,
        can_controller,
        hub,
        stale_buffer_ttl_ms=3000,
        max_queue_size=32,
    ):
        self.afe_id = afe_id
        self.can = can_controller
        self.hub = hub
        self.stale_buffer_ttl_ms = stale_buffer_ttl_ms

        self.can_address = afe_id << 2
        self.default_command_timeout_ms = 1000
        self.default_can_timeout_ms = 500

        self.rx_queue = Queue(maxsize=max_queue_size)
        self.to_execute = Queue(maxsize=max_queue_size)

        self._pending_requests = {}
        self._active_payloads = {}
        self._assembly_buffers = {}

        self.request_configuration = True
        self.is_configured = False
        self.configuration = None

    def prepare_command(
        self,
        command,
        data=None,
        chunk=1,
        max_chunks=1,
        timeout_ms=None,
        preserve=False,
        can_timeout_ms=None,
        callback=None,
        callback_error=None,
        **kwargs
    ):
        if data is None:
            data = ()
        elif isinstance(data, int):
            data = (data,)

        data_len = len(data)
        if data_len > 6:
            data_len = 6

        frame = bytearray(data_len + 2)
        frame[0] = command & 0xFF
        frame[1] = ((max_chunks & 0x0F) << 4) | (chunk & 0x0F)

        for index in range(data_len):
            frame[index + 2] = int(data[index]) & 0xFF

        now = millis()

        if timeout_ms is None:
            timeout_ms = self.default_command_timeout_ms
        if can_timeout_ms is None:
            can_timeout_ms = self.default_can_timeout_ms

        return AFECommandPayload(
            command,
            frame,
            self.afe_id,
            self.can_address,
            timeout_ms,
            now,
            can_timeout_ms,
            preserve,
            callback,
            callback_error,
        )

    async def enqueue_command(self, command, data=None, **kwargs):
        payload = self.prepare_command(command, data, **kwargs)
        await self.to_execute.put(payload)
        return payload

    async def send_command_and_wait(self, command, data=None, *args, **kwargs):
        """Send a command and wait for its matching CAN response.

        AFE responses may arrive in any order. Completion is correlated by
        command ID. The optional third positional value preserves the
        configure() helper calling convention: command, channel, value.
        """
        if args:
            if len(args) != 1:
                raise TypeError("expected command, channel, value")
            channel = data
            value = args[0]

            float_commands = (
                AFECommand.setChannel_a_byMask,
                AFECommand.setChannel_b_byMask,
                AFECommand.setRegulator_a_dac_byMask,
                AFECommand.setRegulator_b_dac_byMask,
                AFECommand.setRegulator_V_opt_byMask,
                AFECommand.setRegulator_dV_dT_byMask,
                AFECommand.setRegulator_T_opt_byMask,
                AFECommand.setAveragingAlpha_byMask,
                AFECommand.setRegulator_dT_byMask,
                AFECommand.setRegulator_V_offset_byMask,
            )
            if command in float_commands:
                packed = struct.pack("<f", value)
                data = (channel, packed[0], packed[1], packed[2], packed[3])
            elif command == AFECommand.setAD8402Value_byte_byMask:
                data = (channel, int(value) & 0xFF)
            else:
                data = (
                    channel,
                    int(value) & 0xFF,
                    (int(value) >> 8) & 0xFF,
                    (int(value) >> 16) & 0xFF,
                    (int(value) >> 24) & 0xFF,
                )

        timeout_ms = kwargs.get("timeout_ms")
        if timeout_ms is None:
            timeout_ms = self.default_command_timeout_ms
            kwargs["timeout_ms"] = timeout_ms

        payload = await self.enqueue_command(command, data, **kwargs)

        try:
            await asyncio.wait_for_ms(
                payload.completion_event.wait(),
                timeout_ms,
            )
        except asyncio.TimeoutError:
            message = "AFE-{} cmd 0x{:X} timed out".format(
                self.afe_id, command
            )
            raise AFETimeoutError(message)

        if payload.status != CommandStatus.SUCCESS:
            if payload.retval is not None and isinstance(payload.retval, Exception):
                raise payload.retval
            raise RuntimeError(
                "AFE-{} cmd 0x{:X} transfer failed".format(
                    self.afe_id, command
                )
            )

        return payload.retval

    async def command_queue_worker(self):
        """Transmit queued commands and track them until their CAN response arrives."""
        while True:
            payload = await self.to_execute.get()

            payload.timeout_start_on_send_ms = millis()

            active = self._active_payloads.get(payload.command)
            if active is None:
                active = []
                self._active_payloads[payload.command] = active
            active.append(payload)

            full_can_id = (self.afe_id << 2) | (payload.command & 0x0F)

            try:
                await self.can.send_frame(full_can_id, payload.frame)
            except Exception as exc:
                await self._handle_complete_message(
                    payload.command,
                    payload_bytes=None,
                    exception=exc,
                    specific_payload=payload,
                )

            await asyncio.sleep_ms(0)

    async def enqueue_gpio_set(self, gpio, state, **kwargs):
        return await self.enqueue_command(
            AFECommand.writeGPIO,
            (gpio.port, gpio.pin, state),
            **kwargs
        )

    async def enqueue_float_for_channel(self, command, channel, value, **kwargs):
        packed = struct.pack("<f", value)
        return await self.enqueue_command(
            command,
            (channel, packed[0], packed[1], packed[2], packed[3]),
            **kwargs
        )

    async def enqueue_u8_for_channel(self, command, channel, value, **kwargs):
        return await self.enqueue_command(
            command,
            (channel, value & 0xFF),
            **kwargs
        )

    async def enqueue_u16_for_channel(self, command, channel, value, **kwargs):
        return await self.enqueue_command(
            command,
            (channel, value & 0xFF, (value >> 8) & 0xFF),
            **kwargs
        )

    async def enqueue_u32_for_channel(self, command, channel, value, **kwargs):
        return await self.enqueue_command(
            command,
            (
                channel,
                value & 0xFF,
                (value >> 8) & 0xFF,
                (value >> 16) & 0xFF,
                (value >> 24) & 0xFF,
            ),
            **kwargs
        )

    def callback_afe_error(self, payload, error):
        print("AFE callback error:", error)

    async def configure(self):
        from my_utilities import (
            get_configuration_from_files,
            extract_bracketed,
            convert_to_si,
        )

        self.configuration = await get_configuration_from_files(self.afe_id)
        self.request_configuration = False
        self.is_configured = True

        command_kwargs = {
            "timeout_ms": 10220,
            "preserve": False,
            "callback_error": self.callback_afe_error,
        }

        for group in ("M", "S"):
            avg_number = 256
            time_sample_ms = 1000

            group_config = self.configuration.get(group, {})
            for key, value in group_config.items():
                print("Configuring", group, key, value)

                parts = key.split(" ")
                setting = parts[0]
                unit = None
                if len(parts) > 1:
                    unit_parts = extract_bracketed(parts[1])
                    if unit_parts:
                        unit = unit_parts[0]

                if unit:
                    value = convert_to_si(value, unit)

                if setting == "T_measured_a":
                    await self.send_command_and_wait(
                        AFECommand.setChannel_a_byMask,
                        get_t_measured_ch_id(group),
                        value,
                        **command_kwargs
                    )
                elif setting == "T_measured_b":
                    await self.send_command_and_wait(
                        AFECommand.setChannel_b_byMask,
                        get_t_measured_ch_id(group),
                        value,
                        **command_kwargs
                    )
                elif setting == "offset":
                    await self.send_command_and_wait(
                        AFECommand.setAD8402Value_byte_byMask,
                        get_subdevice_ch_id(group),
                        int(value),
                        **command_kwargs
                    )
                elif setting == "U_measured_a":
                    await self.send_command_and_wait(
                        AFECommand.setChannel_a_byMask,
                        get_u_measured_ch_id(group),
                        value,
                        **command_kwargs
                    )
                elif setting == "U_measured_b":
                    await self.send_command_and_wait(
                        AFECommand.setChannel_b_byMask,
                        get_u_measured_ch_id(group),
                        value,
                        **command_kwargs
                    )
                elif setting == "I_measured_a":
                    await self.send_command_and_wait(
                        AFECommand.setChannel_a_byMask,
                        get_i_measured_ch_id(group),
                        value,
                        **command_kwargs
                    )
                elif setting == "I_measured_b":
                    await self.send_command_and_wait(
                        AFECommand.setChannel_b_byMask,
                        get_i_measured_ch_id(group),
                        value,
                        **command_kwargs
                    )
                elif setting == "U_set_a":
                    await self.send_command_and_wait(
                        AFECommand.setRegulator_a_dac_byMask,
                        get_subdevice_ch_id(group),
                        value,
                        **command_kwargs
                    )
                elif setting == "U_set_b":
                    await self.send_command_and_wait(
                        AFECommand.setRegulator_b_dac_byMask,
                        get_subdevice_ch_id(group),
                        value,
                        **command_kwargs
                    )
                elif setting == "V_opt":
                    await self.send_command_and_wait(
                        AFECommand.setRegulator_V_opt_byMask,
                        get_subdevice_ch_id(group),
                        value,
                        **command_kwargs
                    )
                elif setting == "dV/dT":
                    await self.send_command_and_wait(
                        AFECommand.setRegulator_dV_dT_byMask,
                        get_subdevice_ch_id(group),
                        value,
                        **command_kwargs
                    )
                elif setting == "T_opt":
                    await self.send_command_and_wait(
                        AFECommand.setRegulator_T_opt_byMask,
                        get_subdevice_ch_id(group),
                        value,
                        **command_kwargs
                    )
                elif setting == "avg_number":
                    if value:
                        avg_number = int(round(value))
                    else:
                        avg_number = 256
                elif setting == "avg_mode":
                    if not value:
                        value = "NONE"
                    avg_mode = AFECommandAverage[value]
                    await self.send_command_and_wait(
                        AFECommand.setAveragingMode_byMask,
                        [get_subdevice_ch_id(group), avg_mode],
                        **command_kwargs
                    )
                elif setting == "avg_alpha":
                    if value:
                        alpha = value
                    else:
                        alpha = 1.0 / 1000000.0
                    await self.send_command_and_wait(
                        AFECommand.setAveragingAlpha_byMask,
                        get_general_ch_id_mask(group),
                        alpha,
                        **command_kwargs
                    )
                elif setting == "time_sample":
                    if value:
                        time_sample_ms = int(round(value * 1000))
                    else:
                        time_sample_ms = 1000
                    await self.send_command_and_wait(
                        AFECommand.setChannel_dt_ms_byMask,
                        get_general_ch_id_mask(group),
                        time_sample_ms,
                        **command_kwargs
                    )
                elif setting == "dT":
                    await self.send_command_and_wait(
                        AFECommand.setRegulator_dT_byMask,
                        get_subdevice_ch_id(group),
                        value,
                        **command_kwargs
                    )
                elif setting == "V_offset":
                    await self.send_command_and_wait(
                        AFECommand.setRegulator_V_offset_byMask,
                        get_subdevice_ch_id(group),
                        value,
                        **command_kwargs
                    )

            await self.send_command_and_wait(
                AFECommand.setAveraging_max_dt_ms_byMask,
                get_general_ch_id_mask(group),
                int(round(time_sample_ms * avg_number)),
                **command_kwargs
            )
            await self.send_command_and_wait(
                AFECommand.setTemperatureLoop_loop_every_ms,
                get_general_ch_id_mask(group),
                100,
                **command_kwargs
            )

        await self.send_command_and_wait(
            AFECommand.startADC,
            0xFF,
            250,
            **command_kwargs
        )

    async def buffer_cleanup_loop(self):
        while True:
            await asyncio.sleep_ms(1000)
            now = millis()

            stale_buffers = []
            for key, buffer_info in self._assembly_buffers.items():
                if time.ticks_diff(now, buffer_info["last_updated"]) > self.stale_buffer_ttl_ms:
                    stale_buffers.append(key)

            for key in stale_buffers:
                del self._assembly_buffers[key]
                await self.hub.logger.log(
                    "WARN",
                    "AFE-{} purged stale buffer for cmd 0x{:X}".format(
                        self.afe_id, key[0]
                    ),
                )

            stale_payloads = []
            for cmd_id, payloads in self._active_payloads.items():
                for payload in payloads:
                    start = payload.timeout_start_on_send_ms
                    if start is not None and time.ticks_diff(now, start) > payload.timeout_ms:
                        stale_payloads.append((cmd_id, payload))

            for cmd_id, payload in stale_payloads:
                error = AFETimeoutError(
                    "AFE-{} command 0x{:X} timed out waiting for response bus.".format(
                        self.afe_id, cmd_id
                    )
                )
                await self._handle_complete_message(
                    cmd_id,
                    payload_bytes=None,
                    exception=error,
                    specific_payload=payload,
                )

    async def process_loop(self):
        while True:
            if self.request_configuration:
                try:
                    await self.configure()
                except Exception as exc:
                    self.request_configuration = True
                    await self.hub.logger.log(
                        "ERROR",
                        "AFE-{} configuration failed: {}".format(self.afe_id, exc),
                    )
                    await asyncio.sleep_ms(1000)
                    continue

            msg_id, data = await self.rx_queue.get()
            parsed = parse_can_frame(msg_id, data, self.afe_id)
            if parsed is None:
                continue

            (
                device_id,
                command,
                chunk_id,
                max_chunks,
                data_bytes,
                chunk_payload,
            ) = parsed

            total_msgs = max_chunks + 1
            if chunk_id >= total_msgs:
                continue

            now = millis()
            buffer_key = (command, total_msgs)
            buffer_info = self._assembly_buffers.get(buffer_key)

            if buffer_info is None:
                buffer_info = {
                    "total": total_msgs,
                    "chunks": {},
                    "last_updated": now,
                }
                self._assembly_buffers[buffer_key] = buffer_info

            buffer_info["chunks"][chunk_id] = bytes(chunk_payload)
            buffer_info["last_updated"] = now

            if len(buffer_info["chunks"]) == buffer_info["total"]:
                chunks = buffer_info["chunks"]
                try:
                    full_payload = b"".join(
                        chunks[index] for index in range(buffer_info["total"])
                    )
                except KeyError:
                    # Count can only reach total with valid unique IDs, but keep
                    # the guard in case malformed frames arrive.
                    continue

                del self._assembly_buffers[buffer_key]
                await self._handle_complete_message(command, full_payload)

            await asyncio.sleep_ms(0)

    async def _handle_complete_message(
        self,
        cmd_id,
        payload_bytes=None,
        exception=None,
        specific_payload=None,
    ):
        request = self._pending_requests.get(cmd_id)
        if request is not None:
            request.result = payload_bytes
            request.exception = exception
            request.event.set()

        payloads = self._active_payloads.get(cmd_id)
        payload = specific_payload

        if payloads is not None:
            if payload is None:
                payload = payloads.pop(0)
            else:
                try:
                    payloads.remove(payload)
                except ValueError:
                    payload = None

            if not payloads:
                del self._active_payloads[cmd_id]

        if payload is None:
            if request is None and exception is None:
                await self.hub.logger.log(
                    "INFO",
                    "AFE-{} spontaneous data for cmd 0x{:X}".format(
                        self.afe_id, cmd_id
                    ),
                )
            return

        payload.retval = payload_bytes

        if exception is not None:
            payload.status = CommandStatus.ERROR
            payload.completion_event.set()
            if payload.callback_error is not None:
                try:
                    await call_callback(
                        payload.callback_error,
                        payload,
                        exception,
                    )
                except Exception as callback_error:
                    await self.hub.logger.log(
                        "ERROR",
                        "Error-callback crashed: {}".format(callback_error),
                    )
            return

        payload.status = CommandStatus.SUCCESS
        payload.completion_event.set()
        if payload.callback is not None:
            try:
                await call_callback(payload.callback, payload)
            except Exception as callback_error:
                await self.hub.logger.log(
                    "ERROR",
                    "Callback crashed: {}".format(callback_error),
                )
                if payload.callback_error is not None:
                    try:
                        await call_callback(
                            payload.callback_error,
                            payload,
                            callback_error,
                        )
                    except Exception:
                        pass

    async def run(self):
        await asyncio.gather(
            self.process_loop(),
            self.command_queue_worker(),
            self.buffer_cleanup_loop(),
        )


# -----------------------------------------------------------------------------
# Main hub.
# -----------------------------------------------------------------------------
class HUBDevice:
    def __init__(self, can, logger):
        self.can = can
        self.logger = logger
        self.rx_queue = Queue(maxsize=32)
        self.can.register_listener(self.rx_queue)

        self.afes = {}
        self.discover_active = 1
        self.discover_current_id_min = 40
        self.discover_current_id_max = 45
        self.discover_current_id = self.discover_current_id_min

    async def discover(self):
        afe_id = self.discover_current_id
        if afe_id not in self.afes:
            try:
                await self.can.send_frame(afe_id << 2, b"\x00\x11")
            except Exception:
                pass

        self.discover_current_id += 1
        if self.discover_current_id > self.discover_current_id_max:
            self.discover_current_id = self.discover_current_id_min

    async def discovery_loop(self):
        while True:
            if self.discover_active:
                await self.discover()
            await asyncio.sleep_ms(250)

    async def router_loop(self):
        while True:
            msg_id, data = await self.rx_queue.get()
            afe_id = (msg_id >> 2) & 0xFF

            if afe_id not in self.afes:
                await self.logger.log(
                    "SYS",
                    "Discovered new hardware AFE-{}. Initializing...".format(afe_id),
                )
                afe = AFEDevice(
                    afe_id=afe_id,
                    can_controller=self.can,
                    hub=self,
                )
                self.afes[afe_id] = afe
                asyncio.create_task(afe.run())

            await self.afes[afe_id].rx_queue.put((msg_id, data))
            await asyncio.sleep_ms(0)

    async def run(self):
        tasks = [self.router_loop(), self.discovery_loop()]
        for afe in self.afes.values():
            tasks.append(afe.run())
        await asyncio.gather(*tasks)


# -----------------------------------------------------------------------------
# SD logger.
# -----------------------------------------------------------------------------
class SDLogger:
    def __init__(self, mount_point="/sd", filename="system.log"):
        self.filepath = "{}/{}".format(mount_point, filename)
        self.queue = Queue(maxsize=64)

    async def log(self, level, message):
        timestamp = time.time()
        entry = "[{}] [{}] {}\n".format(timestamp, level, message)
        print(entry, end="")
        await self.queue.put(entry)

    async def writer_loop(self):
        while True:
            entry = await self.queue.get()
            try:
                with open(self.filepath, "a") as file_obj:
                    file_obj.write(entry)
                    while not self.queue.empty():
                        file_obj.write(self.queue.get_nowait())
            except Exception as exc:
                print("SD Write Error: {}".format(exc))
            await asyncio.sleep_ms(200)


# -----------------------------------------------------------------------------
# Minimal HTTP server and RTC adjustment.
# -----------------------------------------------------------------------------
class WebServer:
    def __init__(self, hub_device, host="0.0.0.0", port=80):
        self.hub = hub_device
        self.host = host
        self.port = port

    async def start(self):
        await asyncio.start_server(self.handle_client, self.host, self.port)

    async def handle_client(self, reader, writer):
        try:
            request_line = await reader.readline()
            if not request_line:
                return

            request_line = request_line.decode("utf-8")
            content_length = 0

            while True:
                header = await reader.readline()
                if not header or header == b"\r\n":
                    break

                header_text = header.decode("utf-8").lower()
                if header_text.startswith("content-length:"):
                    content_length = int(header_text.split(":", 1)[1].strip())

            if request_line.startswith("POST /api/time") and content_length > 0:
                body = await reader.read(content_length)
                body_text = body.decode("utf-8")
                new_time = None

                for pair in body_text.split("&"):
                    if "=" in pair:
                        key, value = pair.split("=", 1)
                        if key.strip() == "epoch":
                            new_time = int(value.strip())
                            break

                if new_time is None:
                    response = b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n"
                else:
                    tm = time.localtime(new_time)
                    pyb.RTC().datetime(
                        (
                            tm[0],
                            tm[1],
                            tm[2],
                            tm[6] + 1,
                            tm[3],
                            tm[4],
                            tm[5],
                            0,
                        )
                    )
                    await self.hub.logger.log(
                        "SYS",
                        "Manual RTC adjust: {}".format(new_time),
                    )
                    response = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Connection: close\r\n\r\n"
                        b'{"status":"ok"}'
                    )

            elif request_line.startswith("GET /"):
                html = (
                    "<html><body><h1>HUB Controller</h1>"
                    "<p>Status: Running</p></body></html>"
                )
                body = html.encode("utf-8")
                response = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/html\r\n"
                    "Content-Length: {}\r\n"
                    "Connection: close\r\n\r\n".format(len(body))
                ).encode("utf-8") + body
            else:
                response = b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n"

            writer.write(response)
            await writer.drain()
        except Exception as exc:
            print("Web server transaction error: {}".format(exc))
        finally:
            try:
                writer.close()
            except Exception:
                pass


# -----------------------------------------------------------------------------
# Ethernet and NTP.
# -----------------------------------------------------------------------------
class NetworkManager:
    def __init__(self, logger):
        self.logger = logger
        self.nic = network.LAN()
        self.nic.active(True)
        self.is_connected = False

    async def connection_loop(self):
        while True:
            if not self.nic.isconnected():
                if self.is_connected:
                    self.is_connected = False
                    await self.logger.log(
                        "NET",
                        "Link down, resetting interface...",
                    )

                self.nic.active(False)
                await asyncio.sleep_ms(200)
                self.nic.active(True)

                for _ in range(10):
                    if self.nic.isconnected():
                        self.is_connected = True
                        await self.logger.log(
                            "NET",
                            "IP Bound: {}".format(self.nic.ifconfig()),
                        )
                        break
                    await asyncio.sleep_ms(1000)
            else:
                self.is_connected = True

            await asyncio.sleep_ms(5000)


class TimeSyncManager:
    NTP_EPOCH_OFFSET = 2208988800

    def __init__(
        self,
        network_manager,
        logger,
        server="pool.ntp.org",
        sync_interval_sec=3600,
    ):
        self.net = network_manager
        self.logger = logger
        self.server = server
        self.sync_interval_sec = sync_interval_sec

    async def sync_loop(self):
        query = bytearray(48)
        query[0] = 0x1B

        while True:
            if self.net.is_connected:
                sock = None
                try:
                    addr = socket.getaddrinfo(self.server, 123)[0][-1]
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.settimeout(2)
                    sock.sendto(query, addr)
                    message, address = sock.recvfrom(48)

                    if len(message) < 44:
                        raise ValueError("short NTP response")

                    ntp_seconds = struct.unpack("!I", message[40:44])[0]
                    unix_time = ntp_seconds - self.NTP_EPOCH_OFFSET
                    tm = time.localtime(unix_time)
                    pyb.RTC().datetime(
                        (
                            tm[0],
                            tm[1],
                            tm[2],
                            tm[6] + 1,
                            tm[3],
                            tm[4],
                            tm[5],
                            0,
                        )
                    )
                    await self.logger.log(
                        "SYS",
                        "NTP Synced: {}".format(unix_time),
                    )
                    await asyncio.sleep_ms(self.sync_interval_sec * 1000)
                except Exception as exc:
                    await self.logger.log("WARN", "NTP Err: {}".format(exc))
                    await asyncio.sleep_ms(15000)
                finally:
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:
                            pass
            else:
                await asyncio.sleep_ms(15000)


# -----------------------------------------------------------------------------
# Application entry point.
# -----------------------------------------------------------------------------
async def main():
    can_bus = CANController(can_id=1)
    logger = SDLogger()
    hub = HUBDevice(can=can_bus, logger=logger)
    web_server = WebServer(hub_device=hub)
    net_manager = NetworkManager(logger=logger)
    time_sync = TimeSyncManager(network_manager=net_manager, logger=logger)
    watchdog = WatchdogManager(timeout_ms=20000, feed_interval_ms=5000)

    await asyncio.gather(
        watchdog.run(),
        can_bus.tx_loop(),
        can_bus.rx_loop(),
        logger.writer_loop(),
        net_manager.connection_loop(),
        time_sync.sync_loop(),
        hub.run(),
        web_server.start(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("System stopped.")
