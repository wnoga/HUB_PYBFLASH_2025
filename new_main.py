from stream_utilities import (
    send_raw,
    send_chunk_raw,
    send_chunk_str,
    stream_json_value,
    stream_json_key_by_key,
    finish_chunked_stream,
    send_json_dict_chunked
)
import pyb
import uasyncio as asyncio
import time
import network
import socket
import struct
import json
import io
import gc
import uos
import sys

from my_webpage import handle_log_download, send_control_web_page_raw

from my_utilities import (
    AFECommand,
    AFECommandAverage,
    AFECommandChannel,
    AFECommandChannelMask,
    AFECommandSubdevice,
    AFECommandGPIO,
    e_ADC_CHANNEL,
    millis,
    is_timeout,
    is_delay,
)
# Fast mapping: byte value -> string name (e.g., 0x01 -> "STANDARD")
REVERSE_AVG_LOOKUP = {v: k for k, v in AFECommandAverage.items()}

can_tx_sleep_ms = 1
can_rx_sleep_ms = 0
sdlogger_sleep_ms = 200
hub_router_loop_sleep_ms = 0
hub_discovery_loop_sleep_ms = 250

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

    @micropython.native
    def empty(self):
        return self._count == 0

    @micropython.native
    def full(self):
        return self._count >= self.maxsize

    @micropython.native
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

    @micropython.native
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

    @micropython.native
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

class AsyncPrinter:
    def __init__(self, maxsize=64):
        self.queue = Queue(maxsize)

    async def print(self, *args, **kwargs):
        # Format args into a single string safely
        msg = " ".join(str(arg) for arg in args)
        if self.queue.full():
            try:
                self.queue.get_nowait()  # Drop oldest if overflowing
            except IndexError:
                pass
        await self.queue.put(msg)

    async def worker(self):
        while True:
            try:
                msg = await self.queue.get()
                sys.stdout.write(msg + "\n")
                # Yield briefly to let other tasks run smoothly
                await asyncio.sleep_ms(2)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep_ms(10)


# Global printer instance
p = AsyncPrinter()


def robust_worker(error_delay_ms=100):
    """Decorator to make individual class worker methods resilient."""
    def decorator(func):
        async def wrapper(self, *args, **kwargs):
            while True:
                try:
                    await func(self, *args, **kwargs)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print("[{0}] Unhandled exception: {1}".format(
                        func.__name__, exc))
                    await asyncio.sleep_ms(error_delay_ms)
        return wrapper
    return decorator

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

    @micropython.native
    def empty(self):
        return self._count == 0

    @micropython.native
    def full(self):
        return self._count >= self.maxsize

    @micropython.native
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

    @micropython.native
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

    @micropython.native
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

    def to_dict(self):
        """Helper to serialize allowed slots into a clean dictionary."""
        return {
            slot: getattr(self, slot)
            for slot in self.__slots__
            # Skip un-serializable objects like events and functions/callbacks
            if slot not in ("completion_event", "callback", "callback_error", "frame")
        } | {
            # Convert bytearray frame to a list of integers for JSON serialization
            "frame": list(self.frame) if self.frame else []
        }

    def to_json(self):
        """Return JSON string representation."""
        return json.dumps(self.to_dict())


class CommandRequest:
    __slots__ = ("cmd_id", "payload", "timeout_ms",
                 "event", "result", "exception")

    def __init__(self, cmd_id, payload=b"", timeout_ms=2000):
        self.cmd_id = cmd_id
        self.payload = payload
        self.timeout_ms = timeout_ms
        self.event = asyncio.Event()
        self.result = None
        self.exception = None


class ChannelDataPoint:
    __slots__ = ("value", "value_bytes", "timestamp_ms")

    def __init__(self, value: float | int = 0, value_bytes: float = 0.0, timestamp_ms: int = 0):
        self.value = value
        self.value_bytes = value_bytes
        self.timestamp_ms = timestamp_ms

    def to_dict(self):
        """Maintains clean dictionary structures for the JSON API."""
        return {
            "value": self.value,
            "value_bytes": self.value_bytes,
            "timestamp_ms": self.timestamp_ms
        }

    async def write_json(self, writer):
        """Asynchronously streams the data point key-by-key directly to the writer."""
        await writer.write('{"value":')
        await writer.write(str(self.value))
        await writer.write(',"value_bytes":')
        await writer.write(str(self.value_bytes))
        await writer.write(',"timestamp_ms":')
        await writer.write(str(self.timestamp_ms))
        await writer.write('}')

    def to_json_str(self):
        """Returns the JSON representation as a string (synchronously)."""
        buf = io.StringIO()
        buf.write('{"value":')
        buf.write(str(self.value))
        buf.write(',"value_bytes":')
        buf.write(str(self.value_bytes))
        buf.write(',"timestamp_ms":')
        buf.write(str(self.timestamp_ms))
        buf.write('}')
        return buf.getvalue()


class PeriodicDataContainer:
    __slots__ = ("last_data", "average_data", "timestamp_ms")

    def __init__(self):
        self.last_data = {uch: ChannelDataPoint() for uch in range(8)}
        self.average_data = {uch: ChannelDataPoint() for uch in range(8)}
        self.timestamp_ms = 0

    def to_dict(self):
        """Returns the container structure as a clean nested dictionary."""
        return {
            "timestamp_ms": self.timestamp_ms,
            "last_data": {str(e_ADC_CHANNEL[uch]): dp.to_dict() for uch, dp in self.last_data.items()},
            "average_data": {str(e_ADC_CHANNEL[uch]): dp.to_dict() for uch, dp in self.average_data.items()}
        }

    async def write_json(self, writer):
        """Asynchronously streams the entire container structure key-by-key."""
        await writer.write('{"timestamp_ms":')
        await writer.write(str(self.timestamp_ms))

        # Stream last_data dictionary
        await writer.write(',"last_data":{')
        first = True
        for uch, dp in self.last_data.items():
            if not first:
                await writer.write(',')
            await writer.write('"%s":' % e_ADC_CHANNEL[uch])
            await dp.write_json(writer)
            first = False
        await writer.write('}')

        # Stream average_data dictionary
        await writer.write(',"average_data":{')
        first = True
        for uch, dp in self.average_data.items():
            if not first:
                await writer.write(',')
            await writer.write('"%s":' % e_ADC_CHANNEL[uch])
            await dp.write_json(writer)
            first = False
        await writer.write('}}')

    def to_json_str(self):
        """Returns the JSON representation of the container as a string (synchronously)."""
        buf = io.StringIO()
        buf.write('{"timestamp_ms":')
        buf.write(str(self.timestamp_ms))

        buf.write(',"last_data":{')
        first = True
        for uch, dp in self.last_data.items():
            if not first:
                buf.write(',')
            buf.write('"%s":' % e_ADC_CHANNEL[uch])
            buf.write(dp.to_json_str())
            first = False
        buf.write('}')

        buf.write(',"average_data":{')
        first = True
        for uch, dp in self.average_data.items():
            if not first:
                buf.write(',')
            buf.write('"%s":' % e_ADC_CHANNEL[uch])
            buf.write(dp.to_json_str())
            first = False
        buf.write('}}')
        return buf.getvalue()

# -----------------------------------------------------------------------------
# CAN frame helpers.
# -----------------------------------------------------------------------------


@micropython.native
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


@micropython.native
def unmask_channel(masked_channel):
    masked_channel &= 0xFF
    return [i for i in range(8) if (masked_channel >> i) & 1]


@micropython.native
def get_subdevice_ch_id(group):
    if group == "M":
        return AFECommandSubdevice.AFECommandSubdevice_master
    return AFECommandSubdevice.AFECommandSubdevice_slave


@micropython.native
def get_t_measured_ch_id(group):
    if group == "M":
        return AFECommandChannel.AFECommandChannel_7
    return AFECommandChannel.AFECommandChannel_6


@micropython.native
def get_u_measured_ch_id(group):
    if group == "M":
        return AFECommandChannel.AFECommandChannel_2
    return AFECommandChannel.AFECommandChannel_3


@micropython.native
def get_i_measured_ch_id(group):
    if group == "M":
        return AFECommandChannel.AFECommandChannel_4
    return AFECommandChannel.AFECommandChannel_5


@micropython.native
def get_general_ch_id_mask(group):
    if group == "M":
        return AFECommandChannelMask.master
    return AFECommandChannelMask.slave


@micropython.native
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
    """Feeds the MCU watchdog independently of CAN/network activity with self-healing execution."""

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

    @robust_worker(error_delay_ms=1000)
    async def run(self):
        while True:
            if self.enabled and self.wdt is not None:
                # Feeding the hardware watchdog is atomic, but wrapping it
                # in the robust worker ensures any unexpected hardware faults
                # are caught and recovered automatically.
                self.wdt.feed()

            # Yield control back to the event loop
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

    @robust_worker(error_delay_ms=200)
    async def tx_loop(self):
        while True:
            msg_id, data = await self.tx_queue.get()
            try:
                # Transmit over CAN bus with a timeout
                self.can.send(data, msg_id, timeout=2)
                # Use async printer instead of direct prints
                # await p.print("CANTX:", (msg_id >> 2) & 0xFF, data)
            except Exception as exc:
                await p.print("[CAN TX Error]:", exc)

            # Yield control back to event loop
            await asyncio.sleep_ms(can_tx_sleep_ms)

    @robust_worker(error_delay_ms=200)
    async def rx_loop(self):
        while True:
            # Check if messages are available on FIFO 0
            if self.can.any(0):
                try:
                    msg_id, rtr, fmi, data = self.can.recv(0)
                    for queue in self.rx_listeners:
                        # Non-blocking or safe put to prevent listener stalls
                        if not queue.full():
                            await queue.put((msg_id, data))
                        # await p.print("CANRX:", (msg_id >> 2) & 0xFF, data)
                except Exception as exc:
                    await p.print("[CAN RX Error]:", exc)

            # Yield control to allow other tasks to process
            await asyncio.sleep_ms(can_rx_sleep_ms)


# -----------------------------------------------------------------------------
# AFE device.
# -----------------------------------------------------------------------------

# ==========================================
# CENTRALIZED CONFIGURATION & COMMAND MAPS
# ==========================================

# Maps command enums to their value types and config keys
CONFIG_COMMAND_MAP = {
    # Float configurations
    AFECommand.setAveragingAlpha_byMask: ("float", "alpha"),
    AFECommand.setChannel_a_byMask: ("float", "a"),
    AFECommand.setChannel_b_byMask: ("float", "b"),
    AFECommand.setRegulator_T_opt_byMask: ("float", "T_opt"),
    AFECommand.setRegulator_dT_byMask: ("float", "dT"),
    AFECommand.setRegulator_a_dac_byMask: ("float", "a"),
    AFECommand.setRegulator_b_dac_byMask: ("float", "b"),
    AFECommand.setRegulator_dV_dT_byMask: ("float", "dV_dT"),
    AFECommand.setRegulator_V_opt_byMask: ("float", "V_opt"),
    AFECommand.setRegulator_V_offset_byMask: ("float", "V_offset"),

    # U32 configurations
    AFECommand.setChannel_dt_ms_byMask: ("u32", "time_interval_ms"),
    AFECommand.setChannel_period_ms_byMask: ("u32", "period_ms"),
    AFECommand.setAveraging_max_dt_ms_byMask: ("u32", "averaging_max_dt_ms"),
    AFECommand.setTemperatureLoop_loop_every_ms: ("u32", "temperatreloop_every_ms"),
}

FLOAT_COMMANDS = {
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
}


async def _apply_setting(instance, group: str, setting: str, value):
    """Helper function to apply a single configuration setting outside the class."""
    command_kwargs = {
        "timeout_ms": 10220,
        "preserve": False,
        "callback_error": instance.callback_afe_error,
    }

    # Standard command mapping: setting_name: (AFECommand, channel_id_getter_function)
    standard_command_map = {
        "T_measured_a": (AFECommand.setChannel_a_byMask, get_t_measured_ch_id),
        "T_measured_b": (AFECommand.setChannel_b_byMask, get_t_measured_ch_id),
        "U_measured_a": (AFECommand.setChannel_a_byMask, get_u_measured_ch_id),
        "U_measured_b": (AFECommand.setChannel_b_byMask, get_u_measured_ch_id),
        "I_measured_a": (AFECommand.setChannel_a_byMask, get_i_measured_ch_id),
        "I_measured_b": (AFECommand.setChannel_b_byMask, get_i_measured_ch_id),
        "offset": (AFECommand.setAD8402Value_byte_byMask, get_subdevice_ch_id),
        "U_set_a": (AFECommand.setRegulator_a_dac_byMask, get_subdevice_ch_id),
        "U_set_b": (AFECommand.setRegulator_b_dac_byMask, get_subdevice_ch_id),
        "V_opt": (AFECommand.setRegulator_V_opt_byMask, get_subdevice_ch_id),
        "dV/dT": (AFECommand.setRegulator_dV_dT_byMask, get_subdevice_ch_id),
        "T_opt": (AFECommand.setRegulator_T_opt_byMask, get_subdevice_ch_id),
        "dT": (AFECommand.setRegulator_dT_byMask, get_subdevice_ch_id),
        "V_offset": (AFECommand.setRegulator_V_offset_byMask, get_subdevice_ch_id),
    }

    if setting in standard_command_map:
        cmd, ch_getter = standard_command_map[setting]
        cmd_value = int(value) if setting == "offset" else value
        await instance.send_command_and_wait(
            cmd, ch_getter(group), cmd_value, **command_kwargs
        )
        return None

    # Handle special settings
    if setting == "avg_number":
        return int(round(value)) if value else 256

    if setting == "avg_mode":
        val_str = value if value else "NONE"
        avg_mode = AFECommandAverage[val_str]
        await instance.send_command_and_wait(
            AFECommand.setAveragingMode_byMask,
            [get_subdevice_ch_id(group), avg_mode],
            **command_kwargs
        )
    elif setting == "avg_alpha":
        alpha = value if value else (1.0 / 1000000.0)
        await instance.send_command_and_wait(
            AFECommand.setAveragingAlpha_byMask,
            get_general_ch_id_mask(group),
            alpha,
            **command_kwargs
        )
    elif setting == "time_sample":
        time_sample_ms = int(round(value * 1000)) if value else 1000
        await instance.send_command_and_wait(
            AFECommand.setChannel_dt_ms_byMask,
            get_general_ch_id_mask(group),
            time_sample_ms,
            **command_kwargs
        )
        return time_sample_ms

    return None


async def _process_command_payload(instance, command, full_payload, device_id):
    """Helper function to process specific command payloads outside the class."""
    if command == AFECommand.getSerialNumber:
        return None

    if command == AFECommand.startADC:
        instance.adc_run = True
        return None

    if command == AFECommand.getSensorDataSi_last_byMask:
        instance._handle_full_sensor_data_block(full_payload, "last_data")
        return instance.periodic_data.last_data

    if command == AFECommand.getSensorDataSi_average_byMask:
        instance._handle_full_sensor_data_block(full_payload, "average_data")
        return instance.periodic_data.average_data

    if command == AFECommand.setAD8402Value_byte_byMask:
        mask_byte = full_payload[0]
        offset_val = instance._parse_payload_value(full_payload[1:], "u16")
        for uch in unmask_channel(mask_byte):
            group = "M" if uch == 0 else "S"
            instance.configuration[group]["offset [bit]"] = offset_val

            if len(full_payload) > 2 and (0x01 & (full_payload[2] >> uch)):
                await instance.hub.logger.log("ERROR", "AFE {}: error setting offset for CH{}".format(device_id, uch))
                instance.configuration[group]["offset [bit]"] = None
        return None

    if command == AFECommand.setAveragingMode_byMask:
        instance._apply_channel_config(
            full_payload[0], full_payload[1:], "avg_mode", "averaging_mode")
        return None

    # Handle standard mapped configurations (floats, u32s, temperature loops)
    if command in CONFIG_COMMAND_MAP:
        val_type, config_key = CONFIG_COMMAND_MAP[command]
        instance._apply_channel_config(
            full_payload[0], full_payload[1:], val_type, config_key)
        return None

    await p.print("Not handled function 0x{:02X}: {}".format(command, full_payload))
    return None


def _pack_command_payload(command, data, args):
    """Helper function to pack command payload data outside the class."""
    if not args:
        return data

    if len(args) != 1:
        raise TypeError("expected command, channel/mask_state, value")

    is_sequence = isinstance(data, (list, tuple))
    base_payload = list(data) if is_sequence else [data]
    value = args[0]

    if command in FLOAT_COMMANDS:
        base_payload.extend(struct.pack("<f", value))
    elif command == AFECommand.setAD8402Value_byte_byMask:
        base_payload.append(int(value) & 0xFF)
    elif command == AFECommand.setDACValueRaw_bySubdeviceMask:
        # Explicit 16-bit (2 byte) layout
        int_val = int(value)
        base_payload.extend([int_val & 0xFF, (int_val >> 8) & 0xFF])
    else:
        # 32-bit (4 byte) layout fallback
        int_val = int(value)
        base_payload.extend([
            int_val & 0xFF,
            (int_val >> 8) & 0xFF,
            (int_val >> 16) & 0xFF,
            (int_val >> 24) & 0xFF,
        ])

    return tuple(base_payload)


def _parse_payload_value_helper(data_bytes, data_type, reverse_avg_lookup):
    """Helper function to convert raw trailing payload bytes into specific scalar types safely."""
    if not data_bytes:
        return None
    if data_type == "float" and len(data_bytes) >= 4:
        return struct.unpack("<f", data_bytes[:4])[0]
    if data_type == "u32" and len(data_bytes) >= 4:
        return struct.unpack("<I", data_bytes[:4])[0]
    if data_type == "u16" and len(data_bytes) >= 2:
        return struct.unpack("<H", data_bytes[:2])[0]
    if data_type == "avg_mode":
        return reverse_avg_lookup.get(data_bytes[0], "NONE")
    return None


def _parse_full_subdevice_status_helper(target_status_list, full_payload, status_keys, unmask_func):
    """Helper to parse a completely stitched multi-chunk status payload into a targeted status dictionary."""
    offset = 0
    payload_len = len(full_payload)
    chunk_counter = 0

    while offset < payload_len:
        chunk_id_mod = chunk_counter % 13
        mask_byte = full_payload[offset]
        channels = unmask_func(mask_byte)

        if chunk_id_mod < 10:
            data_size = 4
            step_size = 1 + data_size
            if offset + step_size > payload_len:
                break
            payload_data = full_payload[offset + 1: offset + step_size]
            value = struct.unpack("<f", payload_data)[0]
            key = status_keys[chunk_id_mod]

        elif chunk_id_mod in (10, 11):
            data_size = 1
            step_size = 1 + data_size
            if offset + step_size > payload_len:
                break
            payload_data = full_payload[offset + 1: offset + step_size]

            if chunk_id_mod == 10:
                value = "enabled" if payload_data[0] else "disabled"
                key = "temp_loop"
            else:
                value = "true" if payload_data[0] else "false"
                key = "ramp_target_reached"

        else:
            data_size = 4
            step_size = 1 + data_size
            if offset + step_size > payload_len:
                break
            payload_data = full_payload[offset + 1: offset + step_size]
            value = struct.unpack("<I", payload_data)[0]
            key = "timestamp_ms"

        if channels:
            for uch in channels:
                if chunk_id_mod == 0 or uch not in target_status_list:
                    target_status_list[uch] = {
                        "channel": "master" if uch == 0 else "slave"}
                target_status_list[uch][key] = value

        offset += step_size
        chunk_counter += 1


def _parse_full_periodic_sensor_data_helper(full_payload, periodic_data, unmask_func, current_millis):
    """Helper to parse a completely stitched multi-chunk periodic sensor data stream."""
    periodic_data.timestamp_ms = current_millis

    offset = 0
    payload_len = len(full_payload)
    chunk_counter = 0

    while offset < payload_len:
        if offset + 5 > payload_len:
            break

        mask_byte = full_payload[offset]
        data_bytes = full_payload[offset + 1: offset + 5]
        unmasked_channels = unmask_func(mask_byte)

        if chunk_counter in (0, 1, 3):
            val_float = struct.unpack("<f", data_bytes)[0]
            target_dict = periodic_data.last_data if chunk_counter in (
                0, 1) else periodic_data.average_data
            for uch in unmasked_channels:
                if uch in target_dict:
                    if chunk_counter == 1:
                        target_dict[uch].value_bytes = val_float
                    else:
                        target_dict[uch].value = val_float
        elif chunk_counter in (2, 4):
            val_u32 = struct.unpack("<I", data_bytes)[0]
            target_dict = periodic_data.last_data if chunk_counter == 2 else periodic_data.average_data
            for uch in unmasked_channels:
                if uch in target_dict:
                    target_dict[uch].timestamp_ms = val_u32

        offset += 5
        chunk_counter += 1

    return periodic_data


def _build_command_frame_bytes(command, data, chunk, max_chunks, current_millis, default_timeout, default_can_timeout, afe_id, can_address):
    """Helper to prepare the raw AFE command frame and instantiate the command payload container."""
    if data is None:
        data = ()
    elif isinstance(data, int):
        data = (data,)

    data_len = min(len(data), 6)

    frame = bytearray(data_len + 2)
    frame[0] = command & 0xFF
    frame[1] = ((max_chunks & 0x0F) << 4) | (chunk & 0x0F)

    for index in range(data_len):
        frame[index + 2] = int(data[index]) & 0xFF

    return AFECommandPayload(
        command,
        frame,
        afe_id,
        can_address,
        default_timeout,
        current_millis,
        default_can_timeout,
        False,
        None,
        None,
    )


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

        self.channels = {uch: {} for uch in range(8)}
        self.periodic_data = PeriodicDataContainer()
        self.adc_run = False

        self.debug_request_average_ms = 0
        self.debug_request_average_ms_period = 1500

    def _parse_payload_value(self, data_bytes, data_type):
        """Converts raw trailing payload bytes into specific scalar types safely."""
        return _parse_payload_value_helper(data_bytes, data_type, REVERSE_AVG_LOOKUP)

    def _apply_channel_config(self, mask_byte, trailing_bytes, data_type, config_key):
        """Helper to extract unmasked channels and apply parsed values to them."""
        parsed_value = self._parse_payload_value(trailing_bytes, data_type)
        for uch in unmask_channel(mask_byte):
            self.channels[uch][config_key] = parsed_value

    def _handle_full_subdevice_status(self, target_status_list, full_payload):
        """Parses a completely stitched multi-chunk status payload into a targeted status dictionary."""
        _parse_full_subdevice_status_helper(
            target_status_list, full_payload, self._STATUS_KEYS, unmask_channel)

    def _handle_full_periodic_sensor_data(self, full_payload):
        """Parses a completely stitched multi-chunk periodic sensor data stream."""
        try:
            if not hasattr(self, "periodic_data") or self.periodic_data is None:
                self.periodic_data = PeriodicDataContainer()

            return _parse_full_periodic_sensor_data_helper(
                full_payload, self.periodic_data, unmask_channel, millis()
            )
        except Exception as e:
            if hasattr(self, "hub") and self.hub.logger:
                asyncio.create_task(self.hub.logger.log(
                    "ERROR", "Periodic parse loop failure: {}".format(e)))
            else:
                print("Periodic parse loop failure:", e)
            return None

    @micropython.native
    def _handle_full_sensor_data_block(self, full_payload, target_key):
        """Parses stitched last or average sensor data payload streams directly into integer channel keys."""
        if not hasattr(self, "periodic_data") or self.periodic_data is None:
            self.periodic_data = PeriodicDataContainer()

        offset = 0
        payload_len = len(full_payload)
        target_dict = getattr(self.periodic_data, target_key)
        updated_channels = []

        while offset < payload_len:
            if offset + 5 > payload_len:
                break

            mask_byte = full_payload[offset]
            data_bytes = full_payload[offset + 1: offset + 5]

            if offset + 5 == payload_len:
                timestamp_val = struct.unpack("<I", data_bytes)[0]
                self.periodic_data.timestamp_ms = timestamp_val
                for uch in updated_channels:
                    target_dict[uch].timestamp_ms = timestamp_val
            else:
                float_val = struct.unpack("<f", data_bytes)[0]
                for uch in unmask_channel(mask_byte):
                    if uch in target_dict:
                        target_dict[uch].value = float_val
                        updated_channels.append(uch)

            offset += 5

    @micropython.native
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
        if timeout_ms is None:
            timeout_ms = self.default_command_timeout_ms
        if can_timeout_ms is None:
            can_timeout_ms = self.default_can_timeout_ms

        payload = _build_command_frame_bytes(
            command, data, chunk, max_chunks, millis(),
            timeout_ms, can_timeout_ms, self.afe_id, self.can_address
        )
        payload.preserve = preserve
        payload.callback = callback
        payload.callback_error = callback_error
        return payload

    async def enqueue_command(self, command, data=None, **kwargs):
        payload = self.prepare_command(command, data, **kwargs)
        await self.to_execute.put(payload)
        return payload

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

            full_can_id = (self.afe_id << 2)

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
        return await self.enqueue_command(command,
                                          (channel, packed[0], packed[1], packed[2], packed[3]), **kwargs
                                          )

    async def enqueue_u8_for_channel(self, command, channel, value, **kwargs):
        return await self.enqueue_command(command,
                                          (channel, value & 0xFF), **kwargs
                                          )

    async def enqueue_u16_for_channel(self, command, channel, value, **kwargs):
        return await self.enqueue_command(command,
                                          (channel, value & 0xFF, (value >> 8) & 0xFF), **kwargs
                                          )

    async def enqueue_u32_for_channel(self, command, channel, value, **kwargs):
        return await self.enqueue_command(command,
                                          (
                                              channel,
                                              value & 0xFF,
                                              (value >> 8) & 0xFF,
                                              (value >> 16) & 0xFF,
                                              (value >> 24) & 0xFF,
                                          ), **kwargs
                                          )

    async def enqueue_channel(self, command, channel, **kwargs):
        return await self.enqueue_command(
            command, (channel), **kwargs
        )

    async def send_command_and_wait(self, command, data=None, *args, **kwargs):
        """Send a command and wait for its matching CAN response."""
        data = _pack_command_payload(command, data, args)

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

    async def callback_afe_error(self, payload, error):
        p.print("AFE callback error:", error)

    async def _safe_configure(self):
        try:
            await self.configure()
            self.is_configured = True
        except asyncio.CancelledError:
            self.is_configured = False
            await self.hub.logger.log(
                "INFO", "AFE-{} configuration was canceled by system.".format(
                    self.afe_id)
            )
            raise
        except Exception as exc:
            self.request_configuration = True
            self.is_configured = False
            await self.hub.logger.log(
                "ERROR",
                "AFE-{} configuration failed: {}".format(self.afe_id, exc),
            )

    async def configure(self):
        from my_utilities import (
            get_configuration_from_files,
            extract_bracketed,
            convert_to_si,
        )

        self.configuration = await get_configuration_from_files(self.afe_id)
        self.request_configuration = False
        self.is_configured = False

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
                parts = key.split(" ")
                setting = parts[0]
                unit = None
                if len(parts) > 1:
                    unit_parts = extract_bracketed(parts[1])
                    if unit_parts:
                        unit = unit_parts[0]

                if unit:
                    value = convert_to_si(value, unit)

                result = await _apply_setting(self, group, setting, value)

                if setting == "avg_number" and result is not None:
                    avg_number = result
                elif setting == "time_sample" and result is not None:
                    time_sample_ms = result

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

        await self.default_set_dac()
        await self.send_command_and_wait(
            AFECommand.setTemperatureLoopForChannelState_byMask_asStatus,
            [0x03, 1],
            250,
            **command_kwargs
        )
        await p.print("Configured", self.afe_id)

    async def default_set_dac(self, dac_master=3000, dac_slave=3000):
        AFEGPIO_EN_HV0 = AFECommandGPIO(port="PORTB", pin=10)
        AFEGPIO_EN_HV1 = AFECommandGPIO(port="PORTB", pin=11)
        AFEGPIO_EN_CAL_IN0 = AFECommandGPIO(port="PORTB", pin=15)
        AFEGPIO_EN_CAL_IN1 = AFECommandGPIO(port="PORTB", pin=14)

        commandKwargs = {
            "timeout_ms": 10220,
            "preserve": False,
            "timeout_start_on_send_ms": 2000
        }

        for g in ["M", "S"]:
            dac_value = dac_master if g == 'M' else dac_slave
            subdevice_id = get_subdevice_ch_id(g)

            await self.send_command_and_wait(
                AFECommand.setDACValueRaw_bySubdeviceMask,
                subdevice_id,
                dac_value,
                **commandKwargs
            )

            await self.send_command_and_wait(
                AFECommand.setDAC_bySubdeviceMask,
                [subdevice_id, 1],
                **commandKwargs
            )

            gpio_obj = AFEGPIO_EN_HV0 if g == 'M' else AFEGPIO_EN_HV1
            await self.send_command_and_wait(
                AFECommand.writeGPIO,
                [gpio_obj.port, gpio_obj.pin, 1],
                **commandKwargs
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
                    "AFE-{} purged stale buffer for cmd 0x{:02X}".format(
                        self.afe_id, key
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
            return

        if exception is not None:
            payload.retval = payload_bytes
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

        command = cmd_id
        full_payload = payload_bytes

        payload.retval = full_payload

        custom_retval = await _process_command_payload(self, command, full_payload, getattr(self, 'afe_id', None))
        if custom_retval is not None:
            payload.retval = custom_retval

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
        p.print(payload)

    async def periodic_debug_loop(self):
        """Dedicated loop that manages periodic data collection requests independently."""
        while not self.is_configured:
            await asyncio.sleep_ms(100)

        while True:
            if is_timeout(self.debug_request_average_ms, self.debug_request_average_ms_period):
                command_kwargs = {
                    "timeout_ms": 10220,
                    "preserve": False,
                    "callback_error": self.callback_afe_error,
                }
                try:
                    p.print("Jeeeeee")
                    await self.send_command_and_wait(
                        AFECommand.getSensorDataSi_last_byMask,
                        0xFF,
                        **command_kwargs
                    )
                    await self.send_command_and_wait(
                        AFECommand.getSensorDataSi_average_byMask,
                        0xFF,
                        **command_kwargs
                    )
                except Exception as exc:
                    await p.print("Periodic average request failed:", exc)

                self.debug_request_average_ms = millis()

            await asyncio.sleep_ms(10)

    async def configuration_loop(self):
        """Dedicated loop to monitor and handle configuration requests."""
        config_task = None
        while True:
            if self.request_configuration:
                await p.print("Start configure", self.afe_id, millis())
                self.request_configuration = False

                if config_task and not config_task.done():
                    await p.print("Canceling ongoing configuration for AFE-{}", self.afe_id)
                    config_task.cancel()
                    try:
                        await config_task
                    except asyncio.CancelledError:
                        pass

                config_task = asyncio.create_task(self._safe_configure())

            await asyncio.sleep_ms(10)

    async def process_loop(self):
        while True:
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
            now = millis()

            buffer_key = command
            buffer_info = self._assembly_buffers.get(buffer_key)

            if buffer_info is None:
                buffer_info = {
                    "max_chunks": max_chunks,
                    "chunks": {},
                    "last_updated": now,
                }
                self._assembly_buffers[buffer_key] = buffer_info

            buffer_info["chunks"][chunk_id] = bytes(chunk_payload)
            buffer_info["last_updated"] = now

            if chunk_id > max_chunks:
                await p.print("Malformed...", device_id, self.afe_id, " : ", chunk_id, max_chunks, command)
                if chunk_id in buffer_info["chunks"]:
                    del buffer_info["chunks"][chunk_id]
                continue

            if chunk_id == max_chunks:
                try:
                    sorted_keys = sorted(buffer_info["chunks"].keys())
                    full_payload = b"".join(
                        buffer_info["chunks"][idx] for idx in sorted_keys)

                except Exception as e:
                    return
                finally:
                    self._assembly_buffers.pop(buffer_key, None)
                await self._handle_complete_message(command, full_payload)

    async def run(self):
        await asyncio.gather(
            self.process_loop(),
            self.configuration_loop(),
            self.command_queue_worker(),
            self.buffer_cleanup_loop(),
            self.periodic_debug_loop(),
        )


# -----------------------------------------------------------------------------
# Main hub.
# -----------------------------------------------------------------------------
def _set_power_pins(e12_val, e10_val):
    """Low-level helper to configure and set the power pin states."""
    pyb.Pin.cpu.E12.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
    pyb.Pin.cpu.E12.value(e12_val)

    pyb.Pin.cpu.E10.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
    pyb.Pin.cpu.E10.value(e10_val)


class HUBDevice:
    def __init__(self, can, logger):
        self.can = can
        self.logger = logger
        self.rx_queue = Queue(maxsize=128)
        self.can.register_listener(self.rx_queue)

        self.afes = {}
        self.discover_active = 1
        self.discover_current_id_min = 1
        self.discover_current_id_max = 99
        self.discover_current_id = self.discover_current_id_min

    async def powerOn(self):
        _set_power_pins(1, 0)

    async def powerOff(self):
        _set_power_pins(0, 1)

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
            await asyncio.sleep_ms(hub_discovery_loop_sleep_ms)

    async def router_loop(self):
        while True:
            msg_id, data = await self.rx_queue.get()
            afe_id = (msg_id >> 2) & 0xFF

            if afe_id not in self.afes:
                await self.logger.log(
                    "SYS",
                    "Discovered new hardware AFE-{}. Initializing...".format(
                        afe_id),
                )
                afe = AFEDevice(
                    afe_id=afe_id,
                    can_controller=self.can,
                    hub=self,
                )
                self.afes[afe_id] = afe
                asyncio.create_task(afe.run())

            await self.afes[afe_id].rx_queue.put((msg_id, data))
            await asyncio.sleep_ms(hub_router_loop_sleep_ms)

    async def run(self):
        await self.powerOn()
        tasks = [self.router_loop(), self.discovery_loop()]
        for afe in self.afes.values():
            tasks.append(afe.run())
        await asyncio.gather(*tasks)

    def procedure_get_all_afe_id(self):
        return {"afe_id_list": [afe.afe_id for afe in self.afes]}

# -----------------------------------------------------------------------------
# SD logger.
# -----------------------------------------------------------------------------


@micropython.native
def _scan_highest_sequence_number(mount_point):
    """Helper to scan the SD card directory and find the highest log sequence number."""
    max_seq = 0
    try:
        files = uos.listdir(mount_point)
        for f in files:
            if f.startswith("log_") and f.endswith(".jsonl"):
                parts = f.split("_")
                seq_part = parts[-1].split(".")[0]
                if seq_part.isdigit():
                    seq = int(seq_part)
                    if seq > max_seq:
                        max_seq = seq
    except Exception:
        pass
    return max_seq + 1


@micropython.native
def _build_log_filepath(mount_point, sequence_num, time_synced, datetime_str):
    """Helper to construct the proper log file path based on time-sync status."""
    if time_synced and datetime_str:
        filename = "log_{}_{}.jsonl".format(datetime_str, sequence_num)
    else:
        filename = "log_{}.jsonl".format(sequence_num)
    return "{}/{}".format(mount_point, filename)


@micropython.native
def _get_formatted_datetime_string():
    """Helper to format the current localtime tuple into a filename-safe string."""
    tm = time.localtime()
    return "{:04d}{:02d}{:02d}_{:02d}{:02d}{:02d}".format(
        tm[0], tm[1], tm[2], tm[3], tm[4], tm[5]
    )


class SDLogger:
    MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1024 MB
    MAX_LINES = 10000

    def __init__(self, mount_point="/sd"):
        self.mount_point = mount_point
        self.queue = Queue(maxsize=64)
        self.sequence_num = _scan_highest_sequence_number(self.mount_point)
        self.time_synced = False
        self.current_datetime_str = None

        # Track active file stats
        self.current_filepath = None
        self.current_file_lines = 0
        self.current_file_size = 0
        self._update_filepath(rename_existing=False)

    @micropython.native
    def _update_filepath(self, rename_existing=False, old_file_to_rename=None):
        """Generates the correct filename based on time-sync status."""
        new_path = _build_log_filepath(
            self.mount_point, self.sequence_num, self.time_synced, self.current_datetime_str
        )

        if rename_existing and old_file_to_rename and old_file_to_rename != new_path:
            try:
                uos.rename(old_file_to_rename, new_path)
            except Exception as exc:
                print("SD Rename Error: {}".format(exc))

        self.current_filepath = new_path
        self.current_file_lines = 0
        self.current_file_size = 0

    @micropython.native
    def notify_time_synced(self):
        """Called by TimeSyncManager when time is synchronized via NTP."""
        self.current_datetime_str = _get_formatted_datetime_string()

        old_path = self.current_filepath
        was_default_name = "log_{}.jsonl".format(self.sequence_num) in old_path

        self.time_synced = True

        if was_default_name:
            self._update_filepath(rename_existing=True,
                                  old_file_to_rename=old_path)

    @micropython.native
    def list_log_files(self):
        """Returns a sorted list of all log filenames found in the mount point."""
        logs = []
        try:
            for f in uos.listdir(self.mount_point):
                if f.startswith("log_") and f.endswith(".jsonl"):
                    logs.append(f)
            logs.sort()
        except Exception as exc:
            print("SD List Error: {}".format(exc))
        return logs

    async def stream_log_file(self, filename, writer, chunk_size=1024):
        """
        Reads a log file chunk by chunk and streams it to a destination writer 
        (e.g., a WebSocket connection or another file object).
        """
        filepath = "{}/{}".format(self.mount_point, filename)
        try:
            with open(filepath, "r") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break

                    if hasattr(writer, "write"):
                        if asyncio.iscoroutinefunction(writer.write):
                            await writer.write(chunk)
                        else:
                            writer.write(chunk)
                    elif hasattr(writer, "send"):
                        if asyncio.iscoroutinefunction(writer.send):
                            await writer.send(chunk)
                        else:
                            writer.send(chunk)

                    await asyncio.sleep_ms(10)
        except Exception as exc:
            await p.print("SD Stream Error for {}: {}".format(filename, exc))

    async def log(self, level, message):
        timestamp = time.time()
        entry = "{{\"timestamp\": {}, \"level\": \"{}\", \"message\": \"{}\"}}\n".format(
            timestamp, level, message
        )
        await p.print(entry, end="")
        await self.queue.put(entry)

    async def writer_loop(self):
        global sdlogger_sleep_ms
        while True:
            entry = await self.queue.get()
            try:
                entry_len = len(entry.encode('utf-8'))

                if self.current_file_lines >= self.MAX_LINES or self.current_file_size + entry_len > self.MAX_FILE_SIZE:
                    self.sequence_num += 1
                    self._update_filepath(rename_existing=False)

                with open(self.current_filepath, "a") as file_obj:
                    file_obj.write(entry)
                    self.current_file_lines += 1
                    self.current_file_size += entry_len

                    while not self.queue.empty():
                        next_entry = self.queue.get_nowait()
                        next_entry_len = len(next_entry.encode('utf-8'))

                        if self.current_file_lines >= self.MAX_LINES or self.current_file_size + next_entry_len > self.MAX_FILE_SIZE:
                            file_obj.close()
                            self.sequence_num += 1
                            self._update_filepath(rename_existing=False)
                            file_obj = open(self.current_filepath, "a")

                        file_obj.write(next_entry)
                        self.current_file_lines += 1
                        self.current_file_size += next_entry_len

                    file_obj.flush()
                    try:
                        uos.sync()
                    except AttributeError:
                        pass

            except Exception as exc:
                await p.print("SD Write Error: {}".format(exc))

            await asyncio.sleep_ms(sdlogger_sleep_ms)

# -----------------------------------------------------------------------------
# Minimal HTTP server and RTC adjustment.
# -----------------------------------------------------------------------------


async def _send_http_or_raw_response(writer, status_code, response_body, content_type="application/json", is_http=True):
    """Helper to send HTTP-formatted or raw responses using non-blocking send_raw."""
    body_bytes = response_body.encode("utf-8")
    if is_http:
        response = (
            "HTTP/1.1 {}\r\n"
            "Content-Type: {}\r\n"
            "Content-Length: {}\r\n"
            "Connection: close\r\n\r\n".format(
                status_code, content_type, len(body_bytes))
        ).encode("utf-8") + body_bytes
    else:
        response = body_bytes + b"\n"

    await send_raw(writer, response)


async def _parse_http_headers(reader):
    """Helper to read and extract content length and type from HTTP headers."""
    content_length = 0
    content_type = ""
    while True:
        header = await reader.readline()
        if not header or header == b"\r\n" or header == b"\n":
            break
        header_text = header.decode("utf-8").lower()
        if header_text.startswith("content-length:"):
            content_length = int(header_text.split(":", 1)[1].strip())
        elif header_text.startswith("content-type:"):
            content_type = header_text.split(":", 1)[1].strip()
    return content_length, content_type


async def _handle_get_all_latest_status_stream(writer, afes, is_http_request):
    """Helper to stream all AFE statuses key-by-key without memory overload."""
    if is_http_request:
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            "Connection: close\r\n\r\n"
        )
        await send_raw(writer, header.encode("utf-8"))
    await send_raw(writer, b'{')

    first = True
    for afe in afes.values():
        if not first:
            await send_raw(writer, b',')
        first = False
        k_json = json.dumps(str(afe.afe_id))
        afe_data = afe.periodic_data.to_dict() if hasattr(
            afe.periodic_data, "to_dict") else afe.periodic_data
        v_json = json.dumps(afe_data)
        await send_raw(writer, (k_json + ":" + v_json).encode("utf-8"))
        gc.collect()

    await send_raw(writer, b'}')
    if not is_http_request:
        await send_raw(writer, b'\r\n')


class ProcedureRequest:
    def __init__(self, procedure, data, reader, writer, is_http_request=True):
        self.procedure = procedure
        self.data = data
        self.reader = reader
        self.writer = writer
        self.is_http_request = is_http_request
        self.event = asyncio.Event()

class WebServer:
    def __init__(self, hub_device, host="0.0.0.0", port=5555, queue_size=32):
        self.hub = hub_device
        self.host = host
        self.port = port
        self.queue = Queue(maxsize=queue_size)
        self._worker_task = None
        self.log_download_sleep_ms = 10  # Optional pacing for downloads

    async def start(self):
        self._worker_task = asyncio.create_task(self._worker_loop())
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        await server.wait_closed()

    async def _worker_loop(self):
        """Background task that executes procedures one-by-one from the queue."""
        while True:
            req = await self.queue.get()
            try:
                await self.handle_procedure(req.procedure, req.data, req.reader, req.writer, req.is_http_request)
            except Exception as e:
                await p.print("Worker procedure execution error: {}".format(e))
                try:
                    err_body = json.dumps(
                        {"status": "error", "message": str(e)})
                    await _send_http_or_raw_response(req.writer, "500 Internal Server Error", err_body, is_http=req.is_http_request)
                except Exception:
                    pass
            finally:
                req.event.set()

    async def handle_client(self, reader, writer):
        try:
            request_line_bytes = await reader.readline()
            if not request_line_bytes:
                return

            clean_bytes = request_line_bytes.lstrip()

            # 1. Handle raw JSON payload starting with '{'
            if clean_bytes.startswith(b"{"):
                try:
                    data = json.loads(clean_bytes.decode("utf-8"))
                    procedure = data.get("procedure")
                    if procedure:
                        await self._handle_procedure_request(reader, writer, procedure, data, is_http=False)
                    else:
                        await _send_http_or_raw_response(writer, "400 Bad Request", json.dumps({"status": "error", "message": "Missing procedure field"}), is_http=False)
                except Exception as e:
                    await _send_http_or_raw_response(writer, "400 Bad Request", json.dumps({"status": "error", "message": "Malformed raw JSON: " + str(e)}), is_http=False)
                return

            request_line = request_line_bytes.decode("utf-8").strip()
            content_length, content_type = await _parse_http_headers(reader)
            
            # 2. Route Dispatching with local OSError protection
            try:
                if request_line.startswith("GET /stream"):
                    await self._handle_stream(reader, writer)
                elif request_line.startswith("GET /download_log"):
                    parts = request_line.split(" ")
                    request_path = parts[1] if len(parts) > 1 else "/"
                    # Updated: pass writer directly to match the stream-based downloader
                    await handle_log_download(self, request_path, writer)
                elif request_line.startswith("GET /") or request_line.startswith("GET /index.html"):
                    await self._handle_root(reader, writer)
                elif request_line.startswith("POST"):
                    body = await reader.read(content_length) if content_length > 0 else b""
                    body_text = body.decode("utf-8")

                    procedure = None
                    data = {}

                    if "json" in content_type or body.lstrip().startswith(b"{"):
                        try:
                            data = json.loads(body_text)
                            procedure = data.get("procedure")
                        except Exception as e:
                            await _send_http_or_raw_response(writer, "400 Bad Request", json.dumps({"status": "error", "message": "Malformed JSON: " + str(e)}))
                            return
                    else:
                        if "procedure=" in body_text:
                            for pair in body_text.split("&"):
                                if "=" in pair:
                                    key, value = pair.split("=", 1)
                                    if key.strip() == "procedure":
                                        procedure = value.strip()
                                    else:
                                        data[key.strip()] = value.strip()
                        else:
                            procedure = body_text.strip()

                    if procedure:
                        await self._handle_procedure_request(reader, writer, procedure, data, is_http=True)
                    else:
                        await _send_http_or_raw_response(writer, "400 Bad Request", json.dumps({"status": "error", "message": "Missing procedure field"}))
                else:
                    await _send_http_or_raw_response(writer, "404 Not Found", "<h1>404 Not Found</h1>", content_type="text/html")
            except OSError:
                # Silently catch socket connection drops during response writing
                pass

        except Exception as exc:
            await p.print("Web server transaction error: {}".format(exc))
        finally:
            try:
                if hasattr(writer, 'aclose'):
                    await writer.aclose()
                elif hasattr(writer, 'close'):
                    writer.close()
            except Exception:
                pass
                
    async def _handle_root(self, reader, writer):
        """Helper for GET / request line - renders the complete HTML dashboard control page safely."""
        try:
            await send_control_web_page_raw(self, writer)
        except OSError:
            # Client disconnected mid-stream; ignore silently
            pass

    async def _handle_stream(self, reader, writer):
        """Helper for GET /stream request line."""
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n\r\n"
        )
        await send_raw(writer, header.encode("utf-8"))

        while True:
            current_millis = utime.ticks_ms() if hasattr(
                utime, "ticks_ms") else int(utime.time() * 1000)
            payload = json.dumps({"millis": current_millis})
            message = "data: {}\r\n\r\n".format(payload)
            await send_raw(writer, message.encode("utf-8"))
            await asyncio.sleep(1)

    async def _handle_procedure_request(self, reader, writer, procedure, data, is_http=True):
        """Helper for procedure requests coming from both POST and raw b'{' inputs."""
        req = ProcedureRequest(procedure, data, reader,
                               writer, is_http_request=is_http)
        await self.queue.put(req)
        await req.event.wait()

    async def handle_procedure(self, procedure, data, reader, writer, is_http_request=True):
        """Dispatches procedures and streams data using non-blocking send_raw utilities."""
        status_code = "200 OK"
        response_body = ""
        await p.print("Handle procedure", procedure)

        if procedure == "get_all_afe_id":
            if hasattr(self.hub, "procedure_get_all_afe_id"):
                func = self.hub.procedure_get_all_afe_id
                if hasattr(asyncio, "iscoroutinefunction") and asyncio.iscoroutinefunction(func):
                    hub_result = await func()
                else:
                    hub_result = func()
                    if hasattr(asyncio, "iscoroutine") and asyncio.iscoroutine(hub_result):
                        hub_result = await hub_result
                res_data = {"status": "ok",
                            "procedure": procedure, "result": hub_result}
                response_body = json.dumps(res_data)
            else:
                status_code = "500 Internal Server Error"
                response_body = json.dumps(
                    {"status": "error", "message": "procedure_get_all_afe_id not found on hub"})
            await _send_http_or_raw_response(writer, status_code, response_body, is_http=is_http_request)

        elif procedure == "get_afe_data":
            afe_id = int(data.get("afe_id", 41))
            afes = getattr(self.hub, "afes", None)
            if afes and afe_id in afes:
                afe_device = afes[afe_id]
                res_data = {
                    "status": "ok",
                    "afe_id": afe_id,
                    "periodic_data": getattr(afe_device, "periodic_data", {}),
                    "channels": getattr(afe_device, "channels", {})
                }
                response_body = json.dumps(res_data)
            else:
                status_code = "404 Not Found"
                response_body = json.dumps(
                    {"status": "error", "message": "AFE device {} not found".format(afe_id)})
            await _send_http_or_raw_response(writer, status_code, response_body, is_http=is_http_request)

        elif procedure == "set_afe_gpio":
            afe_id = int(data.get("afe_id", 41))
            gpio_obj = data.get("gpio")
            state = data.get("state")

            afes = getattr(self.hub, "afes", None)
            if afes and afe_id in afes:
                afe_device = afes[afe_id]
                await afe_device.enqueue_gpio_set(gpio_obj, state)
                response_body = json.dumps({"status": "ok", "afe_id": afe_id})
            else:
                status_code = "404 Not Found"
                response_body = json.dumps(
                    {"status": "error", "message": "AFE device {} not found".format(afe_id)})
            await _send_http_or_raw_response(writer, status_code, response_body, is_http=is_http_request)

        elif procedure == "get_afe_sensor_data":
            afe_id = int(data.get("afe_id", 41))
            mask = int(data.get("mask", 0xFF))

            afes = getattr(self.hub, "afes", None)
            if afes and afe_id in afes:
                afe_device = afes[afe_id]
                try:
                    result_data = await afe_device.send_command_and_wait(
                        AFECommand.getSensorDataSi_last_byMask,
                        mask,
                        timeout_ms=3000
                    )
                    toreturn = {
                        "status": "ok",
                        "procedure": procedure,
                        "afe_id": afe_id,
                        "result": result_data
                    }
                    response_body = json.dumps(toreturn)
                except Exception as e:
                    status_code = "500 Internal Server Error"
                    response_body = json.dumps({
                        "status": "error",
                        "message": "AFE-{} command failed: {}".format(afe_id, str(e))
                    })
            else:
                status_code = "404 Not Found"
                response_body = json.dumps({
                    "status": "error",
                    "message": "AFE device {} not found".format(afe_id)
                })
            await _send_http_or_raw_response(writer, status_code, response_body, is_http=is_http_request)

        elif procedure == "get_all_latest_status":
            afes = getattr(self.hub, "afes", {})
            await _handle_get_all_latest_status_stream(writer, afes, is_http_request)
            return

        elif procedure == "shift_voltage":
            await p.print("Procedure", procedure, "data:", data)

            afe_id_raw = data.get('id')
            group = data.get('type')
            value_raw = data.get('value')

            if afe_id_raw is None or group is None or value_raw is None:
                await _send_http_or_raw_response(
                    writer,
                    "400 Bad Request",
                    json.dumps(
                        {"status": "error", "message": "Missing required parameters (id, type, value)"}),
                    is_http=is_http_request
                )
                return

            try:
                afe_id = int(afe_id_raw)
                value = float(value_raw)
            except (ValueError, TypeError) as parse_err:
                await _send_http_or_raw_response(
                    writer,
                    "400 Bad Request",
                    json.dumps(
                        {"status": "error", "message": "Invalid parameter type: {}".format(parse_err)}),
                    is_http=is_http_request
                )
                return

            afes = getattr(self.hub, "afes", {})
            if afe_id not in afes:
                await _send_http_or_raw_response(
                    writer,
                    "404 Not Found",
                    json.dumps(
                        {"status": "error", "message": "AFE device {} not found".format(afe_id)}),
                    is_http=is_http_request
                )
                return

            async def _callback(msg=None):
                await p.print("Failed set_offset")

            command_kwargs = {
                "timeout_ms": 10220,
                "preserve": False,
                "callback_error": _callback,
            }

            afe = afes[afe_id]
            retval = await afe.send_command_and_wait(
                AFECommand.setRegulator_V_offset_byMask,
                get_subdevice_ch_id(group),
                value,
                **command_kwargs
            )
            await p.print(retval)
            await send_raw(writer, b'{"status":"OK"}\r\n')

        elif procedure == "set_time":
            new_time = data.get("epoch")
            if new_time is not None:
                tm = utime.localtime(int(new_time))
                if "pyb" in globals() and pyb:
                    pyb.RTC().datetime(
                        (tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))
                if hasattr(self.hub, "logger") and self.hub.logger:
                    await self.hub.logger.log("SYS", "Manual RTC adjust: {}".format(new_time))
                response_body = json.dumps(
                    {"status": "ok", "procedure": procedure})
            else:
                status_code = "400 Bad Request"
                response_body = json.dumps(
                    {"status": "error", "message": "Missing epoch parameter"})
            await _send_http_or_raw_response(writer, status_code, response_body, is_http=is_http_request)

        else:
            status_code = "400 Bad Request"
            response_body = json.dumps(
                {"status": "error", "message": "Unknown procedure: {}".format(procedure)})
            await _send_http_or_raw_response(writer, status_code, response_body, is_http=is_http_request)
   
# -----------------------------------------------------------------------------
# Ethernet and NTP.
# -----------------------------------------------------------------------------


async def _reset_and_wait_for_lan(nic):
    """Helper to reset the LAN interface and poll for connection readiness."""
    nic.active(False)
    await asyncio.sleep_ms(200)
    nic.active(True)

    for _ in range(10):
        if nic.isconnected():
            return True
        await asyncio.sleep_ms(1000)
    return False


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

                # Delegate interface reset and reconnection polling to helper
                connected = await _reset_and_wait_for_lan(self.nic)
                if connected:
                    self.is_connected = True
                    await self.logger.log(
                        "NET",
                        "IP Bound: {}".format(self.nic.ifconfig()),
                    )
            else:
                self.is_connected = True

            await asyncio.sleep_ms(5000)


def _fetch_and_apply_ntp_time(server, ntp_epoch_offset):
    """Helper to query NTP server via UDP, parse time, and update system RTC."""
    query = bytearray(48)
    query[0] = 0x1B

    sock = None
    try:
        addr = socket.getaddrinfo(server, 123)[0][-1]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2)
        sock.sendto(query, addr)
        message, address = sock.recvfrom(48)

        if len(message) < 44:
            raise ValueError("short NTP response")

        ntp_seconds = struct.unpack("!I", message[40:44])[0]
        unix_time = ntp_seconds - ntp_epoch_offset
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
        return unix_time
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


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
        while True:
            if self.net.is_connected:
                try:
                    # Delegate NTP query and RTC updating to the helper function
                    unix_time = _fetch_and_apply_ntp_time(
                        self.server, self.NTP_EPOCH_OFFSET)

                    if hasattr(self.logger, "notify_time_synced"):
                        self.logger.notify_time_synced()

                    await self.logger.log(
                        "SYS",
                        "NTP Synced: {}".format(unix_time),
                    )
                    await asyncio.sleep_ms(self.sync_interval_sec * 1000)
                except Exception as exc:
                    await self.logger.log("WARN", "NTP Err: {}".format(exc))
                    await asyncio.sleep_ms(15000)
            else:
                await asyncio.sleep_ms(15000)

# -----------------------------------------------------------------------------
# Application entry point.
# -----------------------------------------------------------------------------


async def main():
    can_bus = CANController(can_id=1)
    logger = SDLogger(mount_point="/sd")
    hub = HUBDevice(can=can_bus, logger=logger)
    web_server = WebServer(hub_device=hub)
    net_manager = NetworkManager(logger=logger)
    time_sync = TimeSyncManager(network_manager=net_manager, logger=logger)
    watchdog = WatchdogManager(timeout_ms=20000, feed_interval_ms=5000)

    await asyncio.gather(
        p.worker(),  # Dedicated non-blocking print worker
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
