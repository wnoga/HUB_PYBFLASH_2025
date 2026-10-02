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
    AFECommandGPIO,
    e_ADC_CHANNEL,
    millis,
)
# Fast mapping: byte value -> string name (e.g., 0x01 -> "STANDARD")
REVERSE_AVG_LOOKUP = {v: k for k, v in AFECommandAverage.items()}


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
            await asyncio.sleep_ms(0)

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
        
        self.channels = {uch: {} for uch in range(8)}
        self.periodic_data = {
            "last_data": {uch: {} for uch in range(8)},
            "average_data": {uch: {} for uch in range(8)}
        }
        self.adc_run = False

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
        """Helper to extract unmasked channels and apply parsed values directly."""
        parsed_value = self._parse_payload_value(trailing_bytes, data_type)
        if parsed_value is not None:
            for uch in self.unmask_channel(mask_byte):
                if uch in self.channels:
                    # Updates config directly inside your numeric channel integer maps (0-7)
                    self.channels[uch][config_key] = parsed_value

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
        chunk_counter = 0  # Replicates the original sequential chunk order (0 to 12)
        print("$",full_payload)
        while offset < payload_len:
            chunk_id_mod = chunk_counter % 13
            
            # 1. Peek at the channel mask byte
            mask_byte = full_payload[offset]
            channels = self.unmask_channel(mask_byte)
            
            # 2. Determine step sizes based on the expected chunk type sequence
            if chunk_id_mod < 10:
                data_size = 4  # Float length
                step_size = 1 + data_size
                if offset + step_size > payload_len:
                    break
                payload_data = full_payload[offset + 1 : offset + step_size]
                value = struct.unpack("<f", payload_data)[0]
                key = self._STATUS_KEYS[chunk_id_mod]
                
            elif chunk_id_mod in (10, 11):
                data_size = 1  # Boolean byte length
                step_size = 1 + data_size
                if offset + step_size > payload_len:
                    break
                payload_data = full_payload[offset + 1 : offset + step_size]
                
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
                payload_data = full_payload[offset + 1 : offset + step_size]
                value = struct.unpack("<I", payload_data)[0]
                key = "timestamp_ms"

            # 3. If there are valid targeted channels, distribute the decoded fields
            if channels:
                for uch in channels:
                    # Reset or initialize subdevice dictionary structure on sequence boundaries
                    if chunk_id_mod == 0 or uch not in target_status_list:
                        target_status_list[uch] = {"channel": "master" if uch == 0 else "slave"}
                    
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
                data_bytes = full_payload[offset + 1 : offset + 5]
                unmasked_channels = self.unmask_channel(mask_byte)

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
                asyncio.create_task(self.hub.logger.log("ERROR", "Periodic parse loop failure: {}".format(e)))
            else:
                print("Periodic parse loop failure:", e)
            return None

        except Exception as e:
            # Replaced fallback logger pointer safely using class hub hooks
            if hasattr(self, "hub") and self.hub.logger:
                asyncio.create_task(self.hub.logger.log("ERROR", "Error parsing getSensorDataSi_periodic: {}".format(e)))
            else:
                print("Error parsing getSensorDataSi_periodic:", e)
            return None


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

            full_can_id = (self.afe_id << 2)# | (payload.command & 0x0F)

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
                # print("Configuring", group, key, value)

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
        # await self.send_command_and_wait(
        #     AFECommand.setSensorDataSiAndTimestamp_periodic_average,
        #     0xFF,
        #     1000,
        #     **command_kwargs
        # )
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
        command = cmd_id
        full_payload = payload_bytes
        # print("$$$$ 0x{:02X}".format(command), full_payload)
        # --- Inside your full_payload command parsing logic ---
        if command == AFECommand.getSerialNumber:
            print("0x00")
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
            self._apply_channel_config(full_payload[0], full_payload[1:], "avg_mode", "averaging_mode")

        elif command == AFECommand.setAveragingAlpha_byMask:
            self._apply_channel_config(full_payload[0], full_payload[1:], "float", "alpha")

        elif command == AFECommand.setChannel_dt_ms_byMask:
            self._apply_channel_config(full_payload[0], full_payload[1:], "u32", "time_interval_ms")

        elif command == AFECommand.setChannel_a_byMask:
            self._apply_channel_config(full_payload[0], full_payload[1:], "float", "a")

        elif command == AFECommand.setChannel_b_byMask:
            self._apply_channel_config(full_payload[0], full_payload[1:], "float", "b")
                
        elif command == AFECommand.setRegulator_T_opt_byMask:
            self._apply_channel_config(full_payload[0], full_payload[1:], "float", "T_opt")
                
        elif command == AFECommand.setRegulator_dT_byMask:
            self._apply_channel_config(full_payload[0], full_payload[1:], "float", "dT")

        elif command == AFECommand.setRegulator_a_dac_byMask:
            self._apply_channel_config(full_payload[0], full_payload[1:], "float", "a")

        elif command == AFECommand.setRegulator_b_dac_byMask:
            self._apply_channel_config(full_payload[0], full_payload[1:], "float", "b")

        elif command == AFECommand.setRegulator_dV_dT_byMask:
            self._apply_channel_config(full_payload[0], full_payload[1:], "float", "dV_dT")

        elif command == AFECommand.setRegulator_V_opt_byMask:
            self._apply_channel_config(full_payload[0], full_payload[1:], "float", "V_opt")

        elif command == AFECommand.setRegulator_V_offset_byMask:
            self._apply_channel_config(full_payload[0], full_payload[1:], "float", "V_offset")

        elif command == AFECommand.setChannel_period_ms_byMask:
            self._apply_channel_config(full_payload[0], full_payload[1:], "u32", "period_ms")
        elif command == AFECommand.setAveraging_max_dt_ms_byMask:
            self._apply_channel_config(full_payload[0], full_payload[1:], "u32", "averaging_max_dt_ms")
        elif command == AFECommand.setTemperatureLoop_loop_every_ms:
            print("setTemperatureLoop_loop_every_ms", full_payload)
            self._apply_channel_config(full_payload[0], full_payload[1:], "u32", "temperatreloop_every_ms")
        elif command == AFECommand.startADC:
            self.adc_run = True
        # elif command == AFECommand.getSubdeviceStatus:
        #     print("getSubdeviceStatus")
        #     # Initialize or fetch your status list tracking structure
        #     if not hasattr(self, "subdevice_status"):
        #         self.subdevice_status = {}
            
        #     # Parse the complete stitched payload map cleanly in a single pass
        #     self._handle_full_subdevice_status(self.subdevice_status, full_payload)
        #     print("^^^", self.subdevice_status)
        elif command == AFECommand.getSensorDataSi_periodic:
            parsed_data = self._handle_full_periodic_sensor_data(full_payload)
            print(parsed_data)
        else:
            print("Not handled function 0x{:02X}: {}".format(command,full_payload))

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


    async def process_loop(self):
        while True:
            # print("process loop", millis())
            if self.request_configuration:
                self.request_configuration = False
                asyncio.create_task(self._safe_configure())
                await asyncio.sleep_ms(10)
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
                print("Malformed...")
                if chunk_id in buffer_info["chunks"]:
                    del buffer_info["chunks"][chunk_id]
                continue

            if chunk_id == max_chunks:
                try:
                    # 1. Grab and sort the dictionary keys sequentially (0, 1, 2...)
                    sorted_keys = sorted(buffer_info["chunks"].keys())
                    
                    # 2. Extract and join the byte payloads in the correct order
                    full_payload = b"".join(buffer_info["chunks"][idx] for idx in sorted_keys)
                    
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
        self.discover_current_id_min = 41
        self.discover_current_id_max = 41
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

            # print("HUB puting", afe_id, ":",data)
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
        entry = "[{}] [{}] {}".format(timestamp, level, message)
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
                if not header or header == b"":
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
                    response = b"HTTP/1.1 400 Bad Request Connection: close"
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
                        b"HTTP/1.1 200 OK"
                        b"Content-Type: application/json"
                        b"Connection: close"
                        b'{"status":"ok"}'
                    )

            elif request_line.startswith("GET /"):
                html = (
                    "<html><body><h1>HUB Controller</h1>"
                    "<p>Status: Running</p></body></html>"
                )
                body = html.encode("utf-8")
                response = (
                    "HTTP/1.1 200 OK"
                    "Content-Type: text/html"
                    "Content-Length: {}"
                    "Connection: close".format(len(body))
                ).encode("utf-8") + body
            else:
                response = b"HTTP/1.1 404 Not Found Connection: close"

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