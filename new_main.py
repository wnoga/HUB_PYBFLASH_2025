import pyb
import uasyncio as asyncio
import time
import network
import socket
import struct
import json

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

can_tx_sleep_ms = 5
can_rx_sleep_ms = 5
sdlogger_sleep_ms = 200
hub_router_loop_sleep_ms = 0
hub_discovery_loop_sleep_ms = 250

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
                    print("[{0}] Unhandled exception: {1}".format(func.__name__, exc))
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
                # print("CANTX:",(msg_id >> 2) & 0xFF,data)
            except Exception:
                pass
            await asyncio.sleep_ms(can_tx_sleep_ms)

    async def rx_loop(self):
        while True:
            if self.can.any(0):
                try:
                    msg_id, rtr, fmi, data = self.can.recv(0)
                    for queue in self.rx_listeners:
                        await queue.put((msg_id, data))
                        # print("CANRX:",(msg_id >> 2) & 0xFF, data)
                except Exception:
                    pass
            await asyncio.sleep_ms(can_rx_sleep_ms)


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

        self.channels = {uch: {} for uch in range(8)}
        self.periodic_data = {
            "last_data": {uch: {} for uch in range(8)},
            "average_data": {uch: {} for uch in range(8)}
        }
        self.adc_run = False

        self.debug_request_average_ms = 0
        self.debug_request_average_ms_period = 1500

    @micropython.native
    def _parse_payload_value(self, data_bytes, data_type):
        """Converts raw trailing payload bytes into specific scalar types safely."""
        if not data_bytes:
            return None
        if data_type == "float" and len(data_bytes) >= 4:
            return struct.unpack("<f", data_bytes[:4])[0]
        elif data_type == "u32" and len(data_bytes) >= 4:
            return struct.unpack("<I", data_bytes[:4])[0]
        elif data_type == "u16" and len(data_bytes) >= 2:
            return struct.unpack("<H", data_bytes[:2])[0]
        elif data_type == "avg_mode":
            return REVERSE_AVG_LOOKUP.get(data_bytes[0], "NONE")
        return None

    @micropython.native
    def _apply_channel_config(self, mask_byte, trailing_bytes, data_type, config_key):
        """Helper to extract unmasked channels and apply parsed values to them."""
        parsed_value = self._parse_payload_value(trailing_bytes, data_type)
        for uch in unmask_channel(mask_byte):
            # print("A {}:{} -> {}".format(config_key, e_ADC_CHANNEL.get(uch), parsed_value))
            # Check if using dictionary keys (like your e_ADC_CHANNEL strings) or raw index keys
            # If your self.channels dictionary uses string names (e.g., 'TEMP_LOCAL'),
            # make sure to wrap 'uch' with: channel_key = e_ADC_CHANNEL.get(uch, uch)
            # if uch in self.channels:
            self.channels[uch][config_key] = parsed_value
            # print(uch)

    @micropython.native
    def _handle_full_subdevice_status(self, target_status_list, full_payload):
        """Parses a completely stitched multi-chunk status payload into a targeted status dictionary."""
        # Detect segment size by looking at your data types:
        # Float (1 mask + 4 data = 5), Boolean (1 mask + 1 data = 2), U32 (1 mask + 4 data = 5)

        offset = 0
        payload_len = len(full_payload)
        # Replicates the original sequential chunk order (0 to 12)
        chunk_counter = 0
        print("$", full_payload)
        while offset < payload_len:
            chunk_id_mod = chunk_counter % 13

            # 1. Peek at the channel mask byte
            mask_byte = full_payload[offset]
            channels = unmask_channel(mask_byte)

            # 2. Determine step sizes based on the expected chunk type sequence
            if chunk_id_mod < 10:
                data_size = 4  # Float length
                step_size = 1 + data_size
                if offset + step_size > payload_len:
                    break
                payload_data = full_payload[offset + 1: offset + step_size]
                value = struct.unpack("<f", payload_data)[0]
                key = self._STATUS_KEYS[chunk_id_mod]

            elif chunk_id_mod in (10, 11):
                data_size = 1  # Boolean byte length
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

            else:  # chunk_id_mod == 12
                data_size = 4  # U32 length
                step_size = 1 + data_size
                if offset + step_size > payload_len:
                    break
                payload_data = full_payload[offset + 1: offset + step_size]
                value = struct.unpack("<I", payload_data)[0]
                key = "timestamp_ms"

            # 3. If there are valid targeted channels, distribute the decoded fields
            if channels:
                for uch in channels:
                    # Reset or initialize subdevice dictionary structure on sequence boundaries
                    if chunk_id_mod == 0 or uch not in target_status_list:
                        target_status_list[uch] = {
                            "channel": "master" if uch == 0 else "slave"}

                    target_status_list[uch][key] = value

            # Move the pointer to the start of the next concatenated chunk segment
            offset += step_size
            chunk_counter += 1

    def _handle_full_periodic_sensor_data(self, full_payload):
        """Parses a completely stitched multi-chunk periodic sensor data stream."""
        try:
            # Re-initialize state safely matched exactly to your schema structures
            self.periodic_data = {
                "last_data": {uch: {} for uch in range(8)},
                "average_data": {uch: {} for uch in range(8)},
                "timestamp_ms": millis()
            }

            offset = 0
            payload_len = len(full_payload)
            chunk_counter = 0

            # Step through continuous chunks: 1 byte mask + 4 bytes data payload = 5 bytes
            while offset < payload_len:
                if offset + 5 > payload_len:
                    break  # Shield against short/dangling fragments

                mask_byte = full_payload[offset]
                data_bytes = full_payload[offset + 1: offset + 5]
                unmasked_channels = unmask_channel(mask_byte)

                if chunk_counter == 0:  # Last data: data value
                    val_float = struct.unpack("<f", data_bytes)[0]
                    for uch in unmasked_channels:
                        if uch in self.periodic_data["last_data"]:
                            self.periodic_data["last_data"][uch]["value"] = val_float

                elif chunk_counter == 1:  # Last data as raw byte representation
                    val_float = struct.unpack("<f", data_bytes)[0]
                    for uch in unmasked_channels:
                        if uch in self.periodic_data["last_data"]:
                            self.periodic_data["last_data"][uch]["value_bytes"] = val_float

                elif chunk_counter == 2:  # Last data: raw measurement timestamp
                    val_u32 = struct.unpack("<I", data_bytes)[0]
                    for uch in unmasked_channels:
                        if uch in self.periodic_data["last_data"]:
                            self.periodic_data["last_data"][uch]["timestamp_ms"] = val_u32

                elif chunk_counter == 3:  # Average data: calculated arithmetic value
                    val_float = struct.unpack("<f", data_bytes)[0]
                    for uch in unmasked_channels:
                        if uch in self.periodic_data["average_data"]:
                            self.periodic_data["average_data"][uch]["value"] = val_float

                elif chunk_counter == 4:  # Average data: loop processing timestamp
                    val_u32 = struct.unpack("<I", data_bytes)[0]
                    for uch in unmasked_channels:
                        if uch in self.periodic_data["average_data"]:
                            self.periodic_data["average_data"][uch]["timestamp_ms"] = val_u32

                offset += 5
                chunk_counter += 1

            return self.periodic_data

        except Exception as e:
            if hasattr(self, "hub") and self.hub.logger:
                asyncio.create_task(self.hub.logger.log(
                    "ERROR", "Periodic parse loop failure: {}".format(e)))
            else:
                print("Periodic parse loop failure:", e)
            return None

        except Exception as e:
            # Replaced fallback logger pointer safely using class hub hooks
            if hasattr(self, "hub") and self.hub.logger:
                asyncio.create_task(self.hub.logger.log(
                    "ERROR", "Error parsing getSensorDataSi_periodic: {}".format(e)))
            else:
                print("Error parsing getSensorDataSi_periodic:", e)
            return None

    @micropython.native
    def _handle_full_sensor_data_block(self, full_payload, target_key):
        """Parses stitched last or average sensor data payload streams directly into integer channel keys."""
        # Ensure your periodic tracking schema is safely initialized
        if not hasattr(self, "periodic_data") or self.periodic_data is None:
            self.periodic_data = {
                "last_data": {uch: {} for uch in range(8)},
                "average_data": {uch: {} for uch in range(8)}
            }

        offset = 0
        payload_len = len(full_payload)
        target_dict = self.periodic_data[target_key]

        # Step through unified 5-byte chunks (1 byte mask + 4 bytes data payload)
        while offset < payload_len:
            if offset + 5 > payload_len:
                break  # Protect against malformed trailing boundary fragments

            mask_byte = full_payload[offset]
            data_bytes = full_payload[offset + 1: offset + 5]

            # The last 5-byte block in the stitched buffer stream is the timestamp (U32)
            if offset + 5 == payload_len:
                timestamp_val = struct.unpack("<I", data_bytes)[0]
                target_dict["timestamp_ms"] = timestamp_val
            else:
                # All intermediate blocks are telemetry metrics values (Float)
                float_val = struct.unpack("<f", data_bytes)[0]
                for uch in unmask_channel(mask_byte):
                    if uch in target_dict:
                        target_dict[uch]["value"] = float_val

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

            full_can_id = (self.afe_id << 2)  # | (payload.command & 0x0F)

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
        """Send a command and wait for its matching CAN response.

        AFE responses may arrive in any order. Completion is correlated by
        command ID. The optional third positional value preserves the
        configure() helper calling convention: command, channel, value OR command, [mask, state], value.
        """
        if args:
            if len(args) != 1:
                raise TypeError("expected command, channel/mask_state, value")
            
            # Determine if data is a single channel or a list/tuple of [mask, state]
            is_sequence = isinstance(data, (list, tuple))
            base_payload = list(data) if is_sequence else [data]
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
                base_payload.extend(packed)  # Appends the 4 bytes of the float
            elif command == AFECommand.setAD8402Value_byte_byMask:
                base_payload.append(int(value) & 0xFF)
            else:
                # Handles 32-bit integers (like your 250 value)
                base_payload.extend([
                    int(value) & 0xFF,
                    (int(value) >> 8) & 0xFF,
                    (int(value) >> 16) & 0xFF,
                    (int(value) >> 24) & 0xFF,
                ])
            
            # Convert back to a tuple if that's what enqueue_command expects
            data = tuple(base_payload)

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

    def callback_afe_error(self, payload, error):
        print("AFE callback error:", error)

    async def _safe_configure(self):
        try:
            await self.configure()
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
                # print("Configuring", self.afe_id, group, key, value)

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
        await self.send_command_and_wait(
            AFECommand.setTemperatureLoopForChannelState_byMask_asStatus,
            [0x03, 1],
            250,
            **command_kwargs
        )
        # await self.send_command_and_wait(
        #     AFECommand.setSensorDataSiAndTimestamp_periodic_average,
        #     0xFF,
        #     1000,
        #     **command_kwargs
        # )
        # await afe.enqueue_command(AFECommand.setTemperatureLoopForChannelState_byMask_asStatus, [
        #     subdevice, 1 if status else 0], **commandKwargs)
        self.is_configured = True
        print("AFE {} configured!".format(self.afe_id))

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

        # print("^^^ 0x{:02X}".format(cmd_id), payload_bytes, exception, specific_payload)

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
            # if request is None and exception is None:
            #     await self.hub.logger.log(
            #         "INFO",
            #         "AFE-{} spontaneous data for cmd 0x{:X}".format(
            #             self.afe_id, cmd_id
            #         ),
            #     )
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

        # Default retval to raw payload bytes, can be overridden below
        payload.retval = full_payload

        # --- Inside your full_payload command parsing logic ---
        if command == AFECommand.getSerialNumber:
            print("0x00")
            # payload.retval = ... # Set specific return value if needed
        elif command == AFECommand.setAD8402Value_byte_byMask:
            mask_byte = full_payload[0]
            offset_val = self._parse_payload_value(full_payload[1:], "u16")
            for uch in unmask_channel(mask_byte):
                group = "M" if uch == 0 else "S"
                self.configuration[group]["offset [bit]"] = offset_val

                if len(full_payload) > 2 and (0x01 & (full_payload[2] >> uch)):
                    await self.hub.logger.log("ERROR", "AFE {}: error setting offset for CH{}".format(device_id, uch))
                    self.configuration[group]["offset [bit]"] = None

        elif command == AFECommand.setAveragingMode_byMask:
            self._apply_channel_config(
                full_payload[0], full_payload[1:], "avg_mode", "averaging_mode")

        elif command == AFECommand.setAveragingAlpha_byMask:
            self._apply_channel_config(
                full_payload[0], full_payload[1:], "float", "alpha")

        elif command == AFECommand.setChannel_dt_ms_byMask:
            self._apply_channel_config(
                full_payload[0], full_payload[1:], "u32", "time_interval_ms")

        elif command == AFECommand.setChannel_a_byMask:
            self._apply_channel_config(
                full_payload[0], full_payload[1:], "float", "a")

        elif command == AFECommand.setChannel_b_byMask:
            self._apply_channel_config(
                full_payload[0], full_payload[1:], "float", "b")

        elif command == AFECommand.setRegulator_T_opt_byMask:
            self._apply_channel_config(
                full_payload[0], full_payload[1:], "float", "T_opt")

        elif command == AFECommand.setRegulator_dT_byMask:
            self._apply_channel_config(
                full_payload[0], full_payload[1:], "float", "dT")

        elif command == AFECommand.setRegulator_a_dac_byMask:
            self._apply_channel_config(
                full_payload[0], full_payload[1:], "float", "a")

        elif command == AFECommand.setRegulator_b_dac_byMask:
            self._apply_channel_config(
                full_payload[0], full_payload[1:], "float", "b")

        elif command == AFECommand.setRegulator_dV_dT_byMask:
            self._apply_channel_config(
                full_payload[0], full_payload[1:], "float", "dV_dT")

        elif command == AFECommand.setRegulator_V_opt_byMask:
            self._apply_channel_config(
                full_payload[0], full_payload[1:], "float", "V_opt")

        elif command == AFECommand.setRegulator_V_offset_byMask:
            self._apply_channel_config(
                full_payload[0], full_payload[1:], "float", "V_offset")

        elif command == AFECommand.setChannel_period_ms_byMask:
            self._apply_channel_config(
                full_payload[0], full_payload[1:], "u32", "period_ms")
        elif command == AFECommand.setAveraging_max_dt_ms_byMask:
            self._apply_channel_config(
                full_payload[0], full_payload[1:], "u32", "averaging_max_dt_ms")
        elif command == AFECommand.setTemperatureLoop_loop_every_ms:
            print("setTemperatureLoop_loop_every_ms", full_payload)
            self._apply_channel_config(
                full_payload[0], full_payload[1:], "u32", "temperatreloop_every_ms")
        elif command == AFECommand.startADC:
            self.adc_run = True

        elif command == AFECommand.getSensorDataSi_last_byMask:
            self._handle_full_sensor_data_block(full_payload, "last_data")
            payload.retval = self.periodic_data["last_data"]

        elif command == AFECommand.getSensorDataSi_average_byMask:
            self._handle_full_sensor_data_block(full_payload, "average_data")
            payload.retval = self.periodic_data["average_data"]

        else:
            print("Not handled function 0x{:02X}: {}".format(
                command, full_payload))

        # Completion event and success status are now handled strictly after parsing
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

    async def periodic_debug_loop(self):
        """Dedicated loop that manages periodic data collection requests independently."""
        # Wait until the device finishes configuring before starting requests
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
                    # Pointing to the corrected spelling parameter name
                    await self.send_command_and_wait(
                        AFECommand.getSensorDataSi_average_byMask,
                        0xFF,
                        **command_kwargs
                    )
                except Exception as exc:
                    print("Periodic average request failed:", exc)

                self.debug_request_average_ms = millis()

            # High accuracy polling window: yields control back to CPU cleanly
            await asyncio.sleep_ms(10)

    # @robust_worker(error_delay_ms=100)
    async def process_loop(self):
        while True:
            # print("process loop", millis())
            if self.request_configuration:
                self.request_configuration = False
                asyncio.create_task(self._safe_configure())
                await asyncio.sleep_ms(10)
                continue

            # There is blocked until new msg arrive
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

            # 1. Initialize the buffer first so we have a place to store data
            if buffer_info is None:
                buffer_info = {
                    "max_chunks": max_chunks,
                    "chunks": {},
                    "last_updated": now,
                }
                self._assembly_buffers[buffer_key] = buffer_info

            # 2. Store the chunk immediately into the buffer
            buffer_info["chunks"][chunk_id] = bytes(chunk_payload)
            buffer_info["last_updated"] = now

            # 3. Run boundary validation check against max_chunks
            if chunk_id > max_chunks:
                print("Malformed...", device_id, self.afe_id, " : ", chunk_id, max_chunks, command, )
                if chunk_id in buffer_info["chunks"]:
                    del buffer_info["chunks"][chunk_id]
                continue

            if chunk_id == max_chunks:
                try:
                    # 1. Grab and sort the dictionary keys sequentially (0, 1, 2...)
                    sorted_keys = sorted(buffer_info["chunks"].keys())

                    # 2. Extract and join the byte payloads in the correct order
                    full_payload = b"".join(
                        buffer_info["chunks"][idx] for idx in sorted_keys)

                except Exception as e:
                    # Consider logging 'e' here so bugs aren't completely silenced
                    return
                finally:
                    # Housekeeping: safely remove buffer from memory map
                    self._assembly_buffers.pop(buffer_key, None)

                # Forward the completed message
                await self._handle_complete_message(command, full_payload)

    async def run(self):
        await asyncio.gather(
            self.process_loop(),
            self.command_queue_worker(),
            self.buffer_cleanup_loop(),
            self.periodic_debug_loop(),
        )


# -----------------------------------------------------------------------------
# Main hub.
# -----------------------------------------------------------------------------
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
        # await self.logger.log("INFO",
        #                       {
        #     "device_id": 0,
        #     "timestamp_ms": millis(),
        #     "info": "powerOn"
        # })
        pyb.Pin.cpu.E12.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
        pyb.Pin.cpu.E12.value(1)
        pyb.Pin.cpu.E10.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
        pyb.Pin.cpu.E10.value(0)
        

    async def powerOff(self):  # Changed to async def
        # await self.logger.log("INFO",
        #                       json.dumps({
        #     "device_id": 0,
        #     "timestamp_ms": millis(),
        #     "info": "powerOff"
        # }))
        pyb.Pin.cpu.E12.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
        pyb.Pin.cpu.E12.value(0)
        pyb.Pin.cpu.E10.init(pyb.Pin.OUT_PP, pyb.Pin.PULL_NONE)
        pyb.Pin.cpu.E10.value(1)

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

            # print("HUB puting", afe_id, ":",data)
            await self.afes[afe_id].rx_queue.put((msg_id, data))
            await asyncio.sleep_ms(hub_router_loop_sleep_ms)

    async def run(self):
        await self.powerOn()
        tasks = [self.router_loop(), self.discovery_loop()]
        for afe in self.afes.values():
            tasks.append(afe.run())
        await asyncio.gather(*tasks)

    def procedure_get_all_afe_id(self):
        return {"test": millis()}


# -----------------------------------------------------------------------------
# SD logger.
# -----------------------------------------------------------------------------
class SDLogger:
    MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1024 MB
    MAX_LINES = 10000

    def __init__(self, mount_point="/sd"):
        self.mount_point = mount_point
        self.queue = Queue(maxsize=64)
        self.sequence_num = self._get_next_sequence_number()
        self.time_synced = False
        self.current_datetime_str = None
        
        # Track active file stats
        self.current_filepath = None
        self.current_file_lines = 0
        self.current_file_size = 0
        self._update_filepath(rename_existing=False)

    @micropython.native
    def _get_next_sequence_number(self):
        """Scans the SD card to find the highest sequence number used so far."""
        max_seq = 0
        try:
            files = uos.listdir(self.mount_point)
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
    def _update_filepath(self, rename_existing=False, old_file_to_rename=None):
        """Generates the correct filename based on time-sync status."""
        if self.time_synced and self.current_datetime_str:
            filename = "log_{}_{}.jsonl".format(self.current_datetime_str, self.sequence_num)
        else:
            filename = "log_{}.jsonl".format(self.sequence_num)
        
        new_path = "{}/{}".format(self.mount_point, filename)

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
        tm = time.localtime()
        self.current_datetime_str = "{:04d}{:02d}{:02d}_{:02d}{:02d}{:02d}".format(
            tm[0], tm[1], tm[2], tm[3], tm[4], tm[5]
        )
        
        old_path = self.current_filepath
        was_default_name = "log_{}.jsonl".format(self.sequence_num) in old_path
        
        self.time_synced = True
        
        if was_default_name:
            self._update_filepath(rename_existing=True, old_file_to_rename=old_path)

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
                    
                    # Support both sync and async writer interfaces (.write or .send)
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
                    
                    # Yield control briefly to keep the event loop responsive
                    await asyncio.sleep_ms(10)
        except Exception as exc:
            print("SD Stream Error for {}: {}".format(filename, exc))

    async def log(self, level, message):
        timestamp = time.time()
        entry = "{{\"timestamp\": {}, \"level\": \"{}\", \"message\": \"{}\"}}\n".format(
            timestamp, level, message
        )
        print(entry, end="")
        await self.queue.put(entry)

    async def writer_loop(self):
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

            except Exception as exc:
                print("SD Write Error: {}".format(exc))
            
            await asyncio.sleep_ms(sdlogger_sleep_ms)

# -----------------------------------------------------------------------------
# Minimal HTTP server and RTC adjustment.
# -----------------------------------------------------------------------------
class ProcedureRequest:
    """Helper wrapper to hold request data and wait for the background worker response."""

    def __init__(self, procedure, data):
        self.procedure = procedure
        self.data = data
        self.event = asyncio.Event()
        self.result = None
        self.error = None


class WebServer:
    def __init__(self, hub_device, host="0.0.0.0", port=5555, queue_size=32):
        self.hub = hub_device
        self.host = host
        self.port = port
        self.queue = Queue(maxsize=queue_size)
        self._worker_task = None

    async def start(self):
        # Start the background worker that processes the queue sequentially
        self._worker_task = asyncio.create_task(self._worker_loop())

        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        await server.wait_closed()

    async def _worker_loop(self):
        """Background task that executes procedures one-by-one from the queue."""
        while True:
            req = await self.queue.get()
            try:
                # Execute the procedure
                req.result = await self.handle_procedure(req.procedure, req.data)
            except Exception as e:
                print("Worker procedure execution error: {}".format(e))
                req.error = str(e)
            finally:
                # Signal the waiting client handler that execution is complete
                req.event.set()

    async def handle_client(self, reader, writer):
        response_body = '{"status":"error","message":"Unknown error"}'
        status_code = "500 Internal Server Error"
        content_type_resp = "application/json"

        try:
            request_line = await reader.readline()
            if not request_line:
                return

            request_line = request_line.decode("utf-8").strip()
            content_length = 0
            content_type = ""

            # Read headers until empty line
            while True:
                header = await reader.readline()
                if not header or header == b"\r\n" or header == b"\n":
                    break

                header_text = header.decode("utf-8").lower()
                if header_text.startswith("content-length:"):
                    content_length = int(header_text.split(":", 1)[1].strip())
                elif header_text.startswith("content-type:"):
                    content_type = header_text.split(":", 1)[1].strip()

            # --- Periodic SSE Stream Endpoint (/stream) ---
            if request_line.startswith("GET /stream"):
                header = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/event-stream\r\n"
                    "Cache-Control: no-cache\r\n"
                    "Connection: keep-alive\r\n\r\n"
                )
                writer.write(header.encode("utf-8"))
                await writer.drain()

                while True:
                    current_millis = time.ticks_ms() if hasattr(
                        time, "ticks_ms") else int(time.time() * 1000)
                    payload = json.dumps({"millis": current_millis})
                    message = "data: {}\r\n\r\n".format(payload)
                    writer.write(message.encode("utf-8"))
                    await writer.drain()
                    await asyncio.sleep(1)

            # --- Standard JSON POST Procedures via Queue ---
            elif request_line.startswith("POST") and content_length > 0:
                body = await reader.read(content_length)

                if "json" in content_type or body.lstrip().startswith(b"{"):
                    try:
                        data = json.loads(body.decode("utf-8"))
                        procedure = data.get("procedure")

                        if not procedure:
                            status_code = "400 Bad Request"
                            response_body = json.dumps(
                                {"status": "error", "message": "Missing procedure field"})
                        else:
                            req = ProcedureRequest(procedure, data)
                            await self.queue.put(req)

                            # Wait for worker to finish this specific request
                            await req.event.wait()

                            if req.error:
                                status_code = "500 Internal Server Error"
                                response_body = json.dumps(
                                    {"status": "error", "message": req.error})
                            else:
                                status_code = "200 OK"
                                response_body = json.dumps(req.result)

                    except Exception as e:
                        status_code = "400 Bad Request"
                        response_body = json.dumps(
                            {"status": "error", "message": str(e)})

                elif "/api/time" in request_line:
                    body_text = body.decode("utf-8")
                    new_time = None
                    for pair in body_text.split("&"):
                        if "=" in pair:
                            key, value = pair.split("=", 1)
                            if key.strip() == "epoch":
                                new_time = int(value.strip())
                                break

                    if new_time is None:
                        status_code = "400 Bad Request"
                        response_body = '{"status":"error","message":"Missing epoch"}'
                    else:
                        tm = time.localtime(new_time)
                        if pyb:
                            pyb.RTC().datetime(
                                (tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))
                        if hasattr(self.hub, "logger"):
                            await self.hub.logger.log("SYS", "Manual RTC adjust: {}".format(new_time))
                        status_code = "200 OK"
                        response_body = '{"status":"ok"}'
                else:
                    status_code = "404 Not Found"
                    response_body = '{"status":"error","message":"Endpoint not found"}'

            elif request_line.startswith("GET /"):
                status_code = "200 OK"
                content_type_resp = "text/html"
                response_body = (
                    "<html><body><h1>HUB Controller</h1>"
                    "<p>Status: Running</p>"
                    "<p>Stream Endpoint: /stream</p></body></html>"
                )
            else:
                status_code = "404 Not Found"
                content_type_resp = "text/html"
                response_body = "<h1>404 Not Found</h1>"

        except Exception as exc:
            print("Web server transaction error: {}".format(exc))
            status_code = "500 Internal Server Error"
            response_body = json.dumps(
                {"status": "error", "message": str(exc)})

        finally:
            try:
                body_bytes = response_body.encode("utf-8")
                response = (
                    "HTTP/1.1 {}\r\n"
                    "Content-Type: {}\r\n"
                    "Content-Length: {}\r\n"
                    "Connection: close\r\n\r\n".format(
                        status_code, content_type_resp, len(body_bytes))
                ).encode("utf-8") + body_bytes
                writer.write(response)
                await writer.drain()
            except Exception:
                pass

            try:
                await writer.aclose() if hasattr(writer, 'aclose') else writer.close()
            except Exception:
                pass

    async def handle_procedure(self, procedure, data):
        """Dispatches procedures and returns data back to the client."""

        # 1. Handle Global Hub Procedures
        if procedure == "get_all_afe_id":
            if hasattr(self.hub, "procedure_get_all_afe_id"):
                func = self.hub.procedure_get_all_afe_id
                if hasattr(asyncio, "iscoroutinefunction") and asyncio.iscoroutinefunction(func):
                    hub_result = await func()
                else:
                    hub_result = func()
                    if hasattr(asyncio, "iscoroutine") and asyncio.iscoroutine(hub_result):
                        hub_result = await hub_result
                return {"status": "ok", "procedure": procedure, "result": hub_result}
            else:
                return {"status": "error", "message": "procedure_get_all_afe_id not found on hub"}

        # 2. Handle Specific AFE Device Procedures (e.g., AFE 41)
        elif procedure == "get_afe_data":
            afe_id = int(data.get("afe_id", 41))

            # Check if hub stores afes dict or list
            afes = getattr(self.hub, "afes", None)
            if afes and afe_id in afes:
                afe_device = afes[afe_id]
                # Return current collected periodic / sensor data from the AFE instance
                return {
                    "status": "ok",
                    "afe_id": afe_id,
                    "periodic_data": getattr(afe_device, "periodic_data", {}),
                    "channels": getattr(afe_device, "channels", {})
                }
            else:
                return {"status": "error", "message": "AFE device {} not found".format(afe_id)}

        elif procedure == "set_afe_gpio":
            afe_id = int(data.get("afe_id", 41))
            gpio_obj = data.get("gpio")  # e.g., mapping port/pin
            state = data.get("state")

            afes = getattr(self.hub, "afes", None)
            if afes and afe_id in afes:
                afe_device = afes[afe_id]
                # Calls your async enqueue_gpio_set method on the AFEDevice
                await afe_device.enqueue_gpio_set(gpio_obj, state)
                return {"status": "ok", "afe_id": afe_id}
            else:
                return {"status": "error", "message": "AFE device {} not found".format(afe_id)}

        elif procedure == "get_afe_sensor_data":
            afe_id = int(data.get("afe_id", 41))
            # Channel mask (e.g., 0xFF for all channels)
            mask = int(data.get("mask", 0xFF))

            afes = getattr(self.hub, "afes", None)
            if afes and afe_id in afes:
                afe_device = afes[afe_id]
                try:
                    # send_command_and_wait waits for completion and returns payload.retval
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
                    print(toreturn)
                    return toreturn
                except Exception as e:
                    return {
                        "status": "error",
                        "message": "AFE-{} command failed: {}".format(afe_id, str(e))
                    }
            else:
                return {
                    "status": "error",
                    "message": "AFE device {} not found".format(afe_id)
                }
                
        elif procedure == "get_all_latest_status":
            toreturn = {
                "status": "ok",
                "procedure": procedure,
                "afe_id": 0,
                "result": {afe.afe_id: afe.last_data for afe in afes}
            }
            print(toreturn)


        elif procedure == "set_time":
            new_time = data.get("epoch")
            if new_time is not None:
                tm = time.localtime(int(new_time))
                if pyb:
                    pyb.RTC().datetime(
                        (tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))
                if hasattr(self.hub, "logger"):
                    logger_log = self.hub.logger.log
                    if hasattr(asyncio, "iscoroutinefunction") and asyncio.iscoroutinefunction(logger_log):
                        await logger_log("SYS", "Manual RTC adjust: {}".format(new_time))
                    else:
                        logger_log(
                            "SYS", "Manual RTC adjust: {}".format(new_time))
                return {"status": "ok", "procedure": procedure}
            return {"status": "error", "message": "Missing epoch parameter"}

        else:
            return {"status": "error", "message": "Unknown procedure: {}".format(procedure)}


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
    logger = SDLogger(mount_point="/sd")
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