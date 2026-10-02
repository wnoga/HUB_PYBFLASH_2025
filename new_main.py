import pyb
import uasyncio as asyncio
import time
import os
import network
import socket
import struct
from my_utilities import (AFECommand, millis, wdt, AFECommandAverage,)
# ============================================================================
# 0. LIGHTWEIGHT ASYNC QUEUE FOR UASYNCIO
# ============================================================================
import uasyncio as asyncio

class Queue:
    """A high-performance, memory-safe FIFO queue using a circular ring buffer."""
    def __init__(self, maxsize=32):
        self.maxsize = maxsize
        # Pre-allocate the list slots to avoid dynamic memory allocation at runtime
        self._queue = [None] * maxsize
        self._head = 0
        self._tail = 0
        self._count = 0
        self._ev_put = asyncio.Event()   # Notifies consumers that data is available
        self._ev_get = asyncio.Event()   # Notifies producers that space is available

    async def put(self, val):
        """Puts an item into the queue. Blocks if the queue is full."""
        while self._count >= self.maxsize:
            self._ev_get.clear()
            await self._ev_get.wait()
            
        self._queue[self._tail] = val
        self._tail = (self._tail + 1) % self.maxsize
        self._count += 1
        self._ev_put.set()

    async def get(self):
        """Gets an item from the queue. Blocks if the queue is empty."""
        while self._count == 0:
            self._ev_put.clear()
            await self._ev_put.wait()
            
        val = self._queue[self._head]
        self._queue[self._head] = None  # Free reference for the Garbage Collector
        self._head = (self._head + 1) % self.maxsize
        self._count -= 1
        self._ev_get.set()
        return val

    def empty(self):
        return self._count == 0

    def full(self):
        return self._count >= self.maxsize

    def get_nowait(self):
        if self._count == 0:
            raise IndexError("queue is empty")
        val = self._queue[self._head]
        self._queue[self._head] = None
        self._head = (self._head + 1) % self.maxsize
        self._count -= 1
        self._ev_get.set()
        return val

# ============================================================================
# 1. CAN CONTROLLER INTERFACE
# ============================================================================
class CANController:
    """Wrapper around hardware CAN controller using asyncio queues."""
    def __init__(self, can_id=1):
        self.can = pyb.CAN(can_id)
        # 250kbps config for typical STM32 clocks
        self.can.init(pyb.CAN.NORMAL, extframe=False, prescaler=54,
                 sjw=1, bs1=7, bs2=2, auto_restart=True)
        self.can.setfilter(0, self.can.MASK16, 0, (0, 0, 0, 0))
        self.tx_queue = Queue()
        self.rx_listeners = []

    def register_listener(self, queue):
        self.rx_listeners.append(queue)

    async def send_frame(self, msg_id: int, data: bytes):
        await self.tx_queue.put((msg_id, data))

    async def tx_loop(self):
        while True:
            msg_id, data = await self.tx_queue.get()
            try:
                # Kept timeout minimal (2ms) to prevent freezing the event loop on bus errors
                self.can.send(data, msg_id, timeout=2)
            except Exception:
                pass  
            await asyncio.sleep_ms(2)

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


# ============================================================================
# 2. SUBDEVICE (AFEDevice)
# ============================================================================

class CommandStatus:
    NONE = 0
    SUCCESS = 1
    ERROR = -1

class AFECommandPayload:
    """A memory-efficient container for AFE command configurations."""
    __slots__ = (
        "command", "frame", "device_id", "can_address", "timeout_ms",
        "timestamp_ms", "timestamp_ms_enqueued", "can_timeout_ms", "status",
        "preserve", "timeout_start_on_send_ms", "retval", "callback", "callback_error"
    )

    def __init__(self, command, frame, device_id, can_address, timeout_ms, 
                 timestamp_ms, can_timeout_ms, preserve, callback, callback_error):
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

class CommandRequest:
    __slots__ = ("cmd_id", "payload", "timeout_ms", "event", "result", "exception")
    
    def __init__(self, cmd_id: int, payload: bytes = b'', timeout_ms: int = 2000):
        self.cmd_id = cmd_id
        self.payload = payload
        self.timeout_ms = timeout_ms
        self.event = asyncio.Event()
        self.result = None
        self.exception = None  


@micropython.native
def parse_can_frame(received_data, expected_device_id):
    """Validates and extracts header metadata and payload from raw received data."""
    if not received_data or len(received_data) < 4:
        return None

    msg_header = received_data[0]
    device_id = (msg_header >> 2) & 0xFF
    msg_from_slave = (msg_header >> 10) & 0x001

    if msg_from_slave != 1 or device_id != expected_device_id:
        return None

    data_bytes = list(bytes(received_data[3]))
    if len(data_bytes) < 2:
        return None  

    command = int(data_bytes[0])
    chunk_id = int(data_bytes[1] & 0x0F)
    max_chunks = int((data_bytes[1] >> 4) & 0x0F)
    chunk_payload = data_bytes[2:]

    return device_id, command, chunk_id, max_chunks, data_bytes, chunk_payload

from my_utilities import (AFECommandSubdevice, AFECommandChannel, AFECommandChannelMask)
@micropython.native
def _get_subdevice_ch_id(g):
    return AFECommandSubdevice.AFECommandSubdevice_master if g == 'M' else AFECommandSubdevice.AFECommandSubdevice_slave

@micropython.native
def _get_T_measured_ch_id(g):
    return AFECommandChannel.AFECommandChannel_7 if g == 'M' else AFECommandChannel.AFECommandChannel_6

@micropython.native
def _get_U_measured_ch_id(g):
    return AFECommandChannel.AFECommandChannel_2 if g == 'M' else AFECommandChannel.AFECommandChannel_3

@micropython.native
def _get_I_measured_ch_id(g):
    return AFECommandChannel.AFECommandChannel_4 if g == 'M' else AFECommandChannel.AFECommandChannel_5

@micropython.native
def _get_general_ch_id_mask(g):
    return AFECommandChannelMask.master if g == 'M' else AFECommandChannelMask.slave


class AFEDevice:
    def __init__(self, afe_id: int, can_controller, hub, stale_buffer_ttl_ms: int = 3000, max_queue_size: int = 32):
        self.afe_id = afe_id
        self.can = can_controller
        self.hub = hub
        self.stale_buffer_ttl_ms = stale_buffer_ttl_ms
        
        self.can_address = afe_id << 2
        self.default_command_timeout_ms = 1000
        self.default_can_timeout_ms = 500
        
        self.rx_queue = Queue()  
        self.to_execute = Queue(maxsize=max_queue_size)  
        
        self._pending_requests = {}  
        self._active_payloads = {}   
        self._assembly_buffers = {}
        
        self.request_configuration = True
        self.is_configured = False
        self.configuration = None
        
        self.last_data = {}

    def prepare_command(
        self,
        command: int,
        data=None,
        chunk: int = 1,
        max_chunks: int = 1,
        timeout_ms: int = None,
        preserve: bool = False,
        can_timeout_ms: int = None,
        callback=None,
        callback_error=None,
        **kwargs,
    ):
        """Builds a highly efficient AFECommandPayload object using zero-slicing logic."""
        if data is None:
            data = ()
        elif isinstance(data, int):
            data = (data,)

        data_len = len(data)
        if data_len > 6:
            data_len = 6

        chunk_info = (max_chunks << 4) | chunk
        frame = bytearray(2 + data_len)
        frame[0] = command
        frame[1] = chunk_info
        
        for i in range(data_len):
            frame[2 + i] = int(data[i])

        now = millis()

        return AFECommandPayload(
            command=command,
            frame=frame,
            device_id=self.afe_id,
            can_address=self.can_address,
            timeout_ms=self.default_command_timeout_ms if timeout_ms is None else timeout_ms,
            timestamp_ms=now,
            can_timeout_ms=self.default_can_timeout_ms if can_timeout_ms is None else can_timeout_ms,
            preserve=preserve,
            callback=callback,
            callback_error=callback_error
        )

    async def enqueue_command(self, command: int, data=None, **kwargs):
        """Prepares a command payload and asynchronously pushes it to the TX Ring Buffer."""
        payload = self.prepare_command(command, data, **kwargs)
        await self.to_execute.put(payload)
        return payload

    async def execute_command(self, cmd_id: int, data=None, timeout_ms: int = 2000, **kwargs):
        """Prepares, enqueues, and waits for a response. Raises exceptions gracefully if failed."""
        req = CommandRequest(cmd_id, b'', timeout_ms)
        self._pending_requests[cmd_id] = req

        try:
            await self.enqueue_command(cmd_id, data, timeout_ms=timeout_ms, **kwargs)
        except Exception as queue_err:
            self._pending_requests.pop(cmd_id, None)
            raise RuntimeError("Failed to enqueue command 0x{:X}".format(cmd_id)) from queue_err

        try:
            await asyncio.wait_for(req.event.wait(), timeout_ms / 1000.0)
            
            if req.exception is not None:
                raise req.exception
                
            return req.result

        except asyncio.TimeoutError:
            msg = "AFE-{} cmd 0x{:X} timed out".format(self.afe_id, cmd_id)
            await self.hub.logger.log("WARN", msg)
            raise TimeoutError(msg) 
            
        finally:
            if self._pending_requests.get(cmd_id) is req:
                self._pending_requests.pop(cmd_id, None)

    async def command_queue_worker(self):
        """Processes the optimized ring buffer and tracks active payloads for callbacks."""
        while True:
            payload = await self.to_execute.get()
            
            full_can_id = (self.afe_id << 4) | (payload.command & 0x0F)
            payload.timeout_start_on_send_ms = millis()
            
            self._active_payloads[payload.command] = payload
            print("Sending frame:",payload)
            await self.can.send_frame(full_can_id, payload.frame)

    # --- Downstream Specialized Serialization Wrapper Methods ---

    async def enqueue_gpio_set(self, gpio, state, **kwargs):
        return await self.enqueue_command(AFECommand.writeGPIO, (gpio.port, gpio.pin, state), **kwargs)

    async def enqueue_float_for_channel(self, command, channel, value, **kwargs):
        return await self.enqueue_command(command, (channel,) + struct.unpack('4B', struct.pack('<f', value)), **kwargs)

    async def enqueue_u8_for_channel(self, command, channel, value, **kwargs):
        return await self.enqueue_command(command, (channel, value & 0xFF), **kwargs)

    async def enqueue_u16_for_channel(self, command, channel, value, **kwargs):
        return await self.enqueue_command(command, (channel, value & 0xFF, (value >> 8) & 0xFF), **kwargs)

    async def enqueue_u32_for_channel(self, command, channel, value, **kwargs):
        return await self.enqueue_command(command, (channel, value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF, (value >> 24) & 0xFF), **kwargs)

    # --- Ingress Loops & Housekeeping Loops ---
    def callback_afe_error(self, **x):
        print("Callbackerror:",x)
        
    async def configure(self):
        from my_utilities import get_configuration_from_files, extract_bracketed, convert_to_si
        self.configuration = await get_configuration_from_files(self.afe_id)
        self.request_configuration = False
        print(self.configuration)
        commandKwargs = {"timeout_ms": 10220,
                    "preserve": False,
                    "timeout_start_on_send_ms": 3000,
                    "callback_error": self.callback_afe_error}
        # if kwargs:
        #     commandKwargs.update(kwargs)
        for g in ["M", "S"]:
            ch_id = None
            avg_number = 256
            time_sample_ms = 1000
            for k, v in self.configuration[g].items():
                print("Configuring",g,k,v)
                ch_id = 0x00
                ks = k.split(" ")[0]
                unit = None
                if len(k.split(" ")) > 1:
                    unit = k.split(" ")[1]
                    unit = extract_bracketed(unit)
                    if len(unit):
                        unit = unit[0]
                    else:
                        unit = None
                if unit:
                    v = convert_to_si(v, unit)
                # print("Loading for AFE{}:{} => {} {} [{}]".format(afe_id,g,k,v,unit))
                if ks == "T_measured_a":
                    await self.enqueue_float_for_channel(AFECommand.setChannel_a_byMask, _get_T_measured_ch_id(g), v, **commandKwargs)
                elif ks == "T_measured_b":
                    await self.enqueue_float_for_channel(
                        AFECommand.setChannel_b_byMask, _get_T_measured_ch_id(g), v, **commandKwargs)
                elif ks == "offset":
                    await self.enqueue_u8_for_channel(
                        AFECommand.setAD8402Value_byte_byMask, _get_subdevice_ch_id(g), int(v), **commandKwargs)
                elif ks == "U_measured_a":
                    await self.enqueue_float_for_channel(
                        AFECommand.setChannel_a_byMask, _get_U_measured_ch_id(g), v, **commandKwargs)
                elif ks == "U_measured_b":
                    await self.enqueue_float_for_channel(
                        AFECommand.setChannel_b_byMask, _get_U_measured_ch_id(g), v, **commandKwargs)
                elif ks == "I_measured_a":
                    await self.enqueue_float_for_channel(
                        AFECommand.setChannel_a_byMask, _get_I_measured_ch_id(g), v, **commandKwargs)
                elif ks == "I_measured_b":
                    await self.enqueue_float_for_channel(
                        AFECommand.setChannel_b_byMask, _get_I_measured_ch_id(g), v, **commandKwargs)
                elif ks == "U_set_a":
                    await self.enqueue_float_for_channel(
                        AFECommand.setRegulator_a_dac_byMask, _get_subdevice_ch_id(g), v, **commandKwargs)
                elif ks == "U_set_b":
                    await self.enqueue_float_for_channel(
                        AFECommand.setRegulator_b_dac_byMask, _get_subdevice_ch_id(g), v, **commandKwargs)
                elif ks == "V_opt":
                    await self.enqueue_float_for_channel(
                        AFECommand.setRegulator_V_opt_byMask, _get_subdevice_ch_id(g), v, **commandKwargs)
                elif ks == "dV/dT":
                    await self.enqueue_float_for_channel(
                        AFECommand.setRegulator_dV_dT_byMask, _get_subdevice_ch_id(g), v, **commandKwargs)
                elif ks == "T_opt":
                    await self.enqueue_float_for_channel(
                        AFECommand.setRegulator_T_opt_byMask, _get_subdevice_ch_id(g), v, **commandKwargs)
                elif ks == "avg_number":  # Maximum nuber of samples used in averaging
                    avg_number = v
                    if v:
                        avg_number = v
                    else:
                        avg_number = 256
                    avg_number = int(round(avg_number))
                    continue
                elif ks == "avg_mode":
                    if not v:
                        v = "NONE"
                    avg_mode = AFECommandAverage[v]
                    await self.enqueue_command(AFECommand.setAveragingMode_byMask, [_get_subdevice_ch_id(g),
                                                                                   avg_mode
                                                                                   ], **commandKwargs)
                elif ks == "avg_alpha":  # Average parameter, usually weight
                    ch_id = _get_general_ch_id_mask(g)
                    if v:
                        await self.enqueue_float_for_channel(
                            AFECommand.setAveragingAlpha_byMask, ch_id, v, **commandKwargs)
                    else:
                        await self.enqueue_float_for_channel(
                            AFECommand.setAveragingAlpha_byMask, ch_id, 1.0/(10000*100.0), **commandKwargs)
                elif ks == "time_sample":  # time sample
                    if v:
                        time_sample_ms = v*1000 # to ms
                    else:
                        time_sample_ms = 1000
                    time_sample_ms = int(round(time_sample_ms))
                    await self.enqueue_u32_for_channel(
                        AFECommand.setChannel_dt_ms_byMask, _get_general_ch_id_mask(g), time_sample_ms, **commandKwargs)
                elif ks == "dT":
                    await self.enqueue_float_for_channel(
                        AFECommand.setRegulator_dT_byMask, _get_subdevice_ch_id(g), v, **commandKwargs)
                elif ks == "V_offset":
                    await self.enqueue_float_for_channel(
                        AFECommand.setRegulator_V_offset_byMask, _get_subdevice_ch_id(g), v, **commandKwargs)
                else:
                    continue
                # for uch in afe.unmask_channel(ch_id):
                #     # await self.logger.log(VerbosityLevel["DEBUG"], {
                #     await p.print({
                #         "device_id": afe.device_id,
                #         "timestamp_ms": millis(),
                #         "debug": "AFE {} {} Loading {} (CH{} ? {}) value {}".format(
                #             afe_id, g, k, uch, e_ADC_CHANNEL[uch], v)
                #     })

            await self.enqueue_u32_for_channel(
                AFECommand.setAveraging_max_dt_ms_byMask, _get_general_ch_id_mask(g), int(round(time_sample_ms * avg_number)), **commandKwargs)
            await self.enqueue_u32_for_channel( # Limit temperature loop frequency
                AFECommand.setTemperatureLoop_loop_every_ms, _get_general_ch_id_mask(g), int(100), **commandKwargs)
        await self.enqueue_u32_for_channel(AFECommand.startADC,
            0xFF, int(250), **commandKwargs) # for all channels (0xFF) (not implemented yet), every 500 ms

    async def buffer_cleanup_loop(self):
        while True:
            await asyncio.sleep_ms(1000)
            now = millis()
            
            stale_buffers = [cmd_id for cmd_id, buf in self._assembly_buffers.items() 
                             if time.ticks_diff(now, buf['last_updated']) > self.stale_buffer_ttl_ms]
            for cmd_id in stale_buffers:
                del self._assembly_buffers[cmd_id]
                await self.hub.logger.log("WARN", "AFE-{} purged stale buffer for cmd 0x{:X}".format(self.afe_id, cmd_id))

            stale_payloads = [cmd_id for cmd_id, p in self._active_payloads.items()
                              if p.timeout_start_on_send_ms and time.ticks_diff(now, p.timeout_start_on_send_ms) > p.timeout_ms]
            
            for cmd_id in stale_payloads:
                err = TimeoutError("AFE-{} command 0x{:X} timed out waiting for response bus.".format(self.afe_id, cmd_id))
                await self._handle_complete_message(cmd_id, payload_bytes=None, exception=err)

    async def process_loop(self):
        """Main ingress loop: Demux incoming frames & reassemble payloads using native parsing."""
        while True:
            if self.request_configuration:
                await self.configure()
            msg_id, data = await self.rx_queue.get()
            print("AFE Ingress:", self.afe_id, msg_id, data)
            
            parsed = parse_can_frame((msg_id, None, None, data), self.afe_id)
            if parsed is None:
                continue
                
            device_id, command, chunk_id, max_chunks, data_bytes, chunk_payload = parsed
            total_msgs = max_chunks + 1
            now = millis()
            buffer_key = (command, total_msgs)

            if buffer_key not in self._assembly_buffers:
                self._assembly_buffers[buffer_key] = {
                    'total': total_msgs,
                    'chunks': {},
                    'last_updated': now
                }

            buf = self._assembly_buffers[buffer_key]
            buf['chunks'][chunk_id] = bytes(chunk_payload)
            buf['last_updated'] = now

            if len(buf['chunks']) == buf['total']:
                full_payload = b"".join(buf['chunks'][i] for i in range(buf['total']))
                del self._assembly_buffers[buffer_key]
                await self._handle_complete_message(command, full_payload)

    async def _handle_complete_message(self, cmd_id: int, payload_bytes: bytes = None, exception: Exception = None):
        """Delivers data or exceptions to both blocking calls and callback streams."""
        if cmd_id in self._pending_requests:
            req = self._pending_requests[cmd_id]
            req.result = payload_bytes
            req.exception = exception  
            req.event.set()            

        if cmd_id in self._active_payloads:
            command_payload = self._active_payloads.pop(cmd_id)
            command_payload.retval = payload_bytes
            
            if exception:
                command_payload.status = CommandStatus.ERROR
                if command_payload.callback_error:
                    try:
                        if asyncio.iscoroutinefunction(command_payload.callback_error):
                            await command_payload.callback_error(command_payload, exception)
                        else:
                            command_payload.callback_error(command_payload, exception)
                    except Exception as cb_err:
                        await self.hub.logger.log("ERROR", "Error-callback crashed: {}".format(cb_err))
            else:
                command_payload.status = CommandStatus.SUCCESS
                if command_payload.callback:
                    try:
                        if asyncio.iscoroutinefunction(command_payload.callback):
                            await command_payload.callback(command_payload)
                        else:
                            command_payload.callback(command_payload)
                    except Exception as cb_err:
                        await self.hub.logger.log("ERROR", "Callback crashed: {}".format(cb_err))
                        if command_payload.callback_error:
                            try:
                                command_payload.callback_error(command_payload, cb_err)
                            except Exception: pass

        if cmd_id not in self._pending_requests and cmd_id not in self._active_payloads and not exception:
            msg = "AFE-{} spontaneous data for cmd 0x{:X}".format(self.afe_id, cmd_id)
            await self.hub.logger.log("INFO", msg)
        
        if cmd_id == AFECommand.getSerialNumber:
            unique_id_str = "".join(
                "{:02X}".format(b) for b in payload_bytes)
            self.last_data["serial"] = unique_id_str
            # print("Get Serial Number {}".format(payload_bytes), "->", unique_id_str)
        print(self.last_data)

    async def run(self):
        await asyncio.gather(
            self.process_loop(),
            self.command_queue_worker(),
            self.buffer_cleanup_loop()
        )

# ============================================================================
# 3. MAIN HUB DEVICE
# ============================================================================
class HUBDevice:
    """Main System Manager: coordinates CAN routing, AFEs, and Logging."""
    def __init__(self, can: CANController, logger):
        self.can = can
        self.logger = logger
        self.rx_queue = Queue()
        self.can.register_listener(self.rx_queue)
        
        self.afes = {
            # 1: AFEDevice(afe_id=1, can_controller=self.can, hub=self),
            # 2: AFEDevice(afe_id=2, can_controller=self.can, hub=self)
        }
        self.discover_active = 1
        self.discover_current_id_min = 40
        self.discover_current_id_max = 45
        self.discover_current_id = self.discover_current_id_min
        
    async def discover(self):
        if self.afes.get(self.discover_current_id,None) == None:    
            try:
                # self.can.can.send(b"\x00\x11", self.discover_current_id << 1, timeout=2)
                await self.can.send_frame(self.discover_current_id << 2, b"\x00\x11")
            except Exception:
                pass
        self.discover_current_id += 1
        if self.discover_current_id > self.discover_current_id_max:
            self.discover_current_id = self.discover_current_id_min

    async def discovery_loop(self):
        """FIX: Moved ping operations out of processing router task into a dedicated loop."""
        while True:
            if self.discover_active:
                await self.discover()
            await asyncio.sleep_ms(250) # Ping 4 times a second safely

    async def router_loop(self):
        """Demux incoming raw CAN messages to the correct AFEDevice queue."""
        while True:
            wdt.feed()
            msg_id, data = await self.rx_queue.get()
            afe_id = (msg_id >> 2) & 0xFF
            
            # DYNAMIC REGISTRATION: If it's a new AFE ID, build and start it on the fly
            if afe_id not in self.afes:
                await self.logger.log("SYS", "Discovered new hardware AFE-{%i}. Initializing..." % (afe_id))
                new_afe = AFEDevice(afe_id=afe_id, can_controller=self.can, hub=self) 
                self.afes[afe_id] = new_afe 
                asyncio.create_task(new_afe.run())
            
            # Forward the frame to the target subdevice queue
            await self.afes[afe_id].rx_queue.put((msg_id, data))


    async def run(self):
        await asyncio.gather(
            self.router_loop(),
            self.discovery_loop(),
            *[afe.run() for afe in self.afes.values()]
        )


# ============================================================================
# 4. SD CARD LOGGER
# ============================================================================
class SDLogger:
    def __init__(self, mount_point="/sd", filename="system.log"):
        self.filepath = "{}/{}".format(mount_point, filename)
        self.queue = Queue()

    async def log(self, level: str, message: str):
        timestamp = time.time()
        entry = "[{}] [{}] {}\n".format(timestamp, level, message)
        print(entry, end="")
        await self.queue.put(entry)

    async def writer_loop(self):
        while True:
            entry = await self.queue.get()
            try:
                with open(self.filepath, "a") as f:
                    f.write(entry)
                    while not self.queue.empty():
                        f.write(self.queue.get_nowait())
            except Exception as e:
                print("SD Write Error: {}".format(e))
            await asyncio.sleep_ms(200)


# ============================================================================
# 5. ASYNC WEB SERVER & TIME SYNC
# ============================================================================
# ============================================================================
# 5. ASYNC WEB SERVER & TIME SYNC
# ============================================================================
class WebServer:
    def __init__(self, hub_device: HUBDevice, host="0.0.0.0", port=80):
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
            req_str = request_line.decode('utf-8')
            
            # Simple content length parser loop
            content_length = 0
            while True:
                header = await reader.readline()
                if header == b"\r\n" or not header:
                    break
                header_str = header.decode('utf-8').lower()
                if "content-length:" in header_str:
                    # FIX: Strip whitespace securely and convert to integer
                    content_length = int(header_str.split(":")[1].strip())

            if "POST /api/time" in req_str and content_length > 0:
                body = await reader.read(content_length)
                body_str = body.decode('utf-8')
                try:
                    # FIX: Correctly extract query parameters from body string (layout: epoch=123456789)
                    new_time = None
                    pairs = body_str.split("&")
                    for pair in pairs:
                        if "=" in pair:
                            k, v = pair.split("=", 1)
                            if k.strip() == "epoch":
                                new_time = int(v.strip())
                                break
                    
                    if new_time is not None:
                        tm = time.localtime(new_time)
                        # FIX: Format for Pyboard/STM32 RTC: (year, month, day, weekday, hours, minutes, seconds, subseconds)
                        # tm format: (year, month, mday, hour, minute, second, weekday, yearday)
                        pyb.RTC().datetime((tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))
                        
                        await self.hub.logger.log("SYS", "Manual RTC adjust: {}".format(new_time))
                        response = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\":\"ok\"}"
                    else:
                        response = b"HTTP/1.1 400 Bad Request\r\n\r\n"
                except Exception as ex:
                    await self.hub.logger.log("WARN", "Time parse error: {}".format(ex))
                    response = b"HTTP/1.1 400 Bad Request\r\n\r\n"
            elif "GET /" in req_str:
                html = "<html><body><h1>HUB Controller</h1><p>Status: Running</p></body></html>"
                response = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\n\r\n{}".format(len(html), html).encode()
            else:
                response = b"HTTP/1.1 404 Not Found\r\n\r\n"

            writer.write(response)
            await writer.drain()
        except Exception as e:
            print("Web server transaction error: {}".format(e))
        finally:
            await writer.close()



# ============================================================================
# 6. ETHERNET AND NETWORK TIME MANAGERS
# ============================================================================
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
                    await self.logger.log("NET", "Link down, resetting interface...")
                
                self.nic.active(False)
                await asyncio.sleep_ms(200)
                self.nic.active(True)
                
                for _ in range(10):
                    if self.nic.isconnected():
                        self.is_connected = True
                        config = self.nic.ifconfig()
                        await self.logger.log("NET", "IP Bound: {}".format(config))
                        break
                    await asyncio.sleep_ms(1000)
            else:
                self.is_connected = True
            await asyncio.sleep_ms(5000)


class TimeSyncManager:
    def __init__(self, network_manager, logger, server="pool.ntp.org", sync_interval_sec=3600):
        self.net = network_manager
        self.logger = logger
        self.server = server
        self.sync_interval_sec = sync_interval_sec

    async def sync_loop(self):
        NTP_QUERY = bytearray(48)
        NTP_QUERY[0] = 0x1B
        
        while True:
            if self.net.is_connected:
                try:
                    addr = socket.getaddrinfo(self.server, 123)[0][-1]
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.settimeout(2)
                    
                    s.sendto(NTP_QUERY, addr)
                    msg, address = s.recvfrom(48)
                    s.close()
                    
                    val = struct.unpack("!I", msg[40:44])[0]
                    unix_time = val - 2208988800
                    
                    tm = time.localtime(unix_time)
                    pyb.RTC().datetime((tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))
                    
                    await self.logger.log("SYS", "NTP Synced: {}".format(unix_time))
                    await asyncio.sleep_ms(self.sync_interval_sec * 1000)
                    continue
                except Exception as e:
                    await self.logger.log("WARN", "NTP Err: {}".format(e))
            
            await asyncio.sleep_ms(15000)


# ============================================================================
# 7. MAIN APPLICATION ENTRY POINT
# ============================================================================
async def main():
    can_bus = CANController(can_id=1)
    logger = SDLogger()
    hub = HUBDevice(can=can_bus, logger=logger)
    web_server = WebServer(hub_device=hub)
    net_manager = NetworkManager(logger=logger)
    time_sync = TimeSyncManager(network_manager=net_manager, logger=logger)

    await asyncio.gather(
        can_bus.tx_loop(),
        can_bus.rx_loop(),
        logger.writer_loop(),
        net_manager.connection_loop(),
        time_sync.sync_loop(),
        hub.run(),
        web_server.start()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("System stopped.")