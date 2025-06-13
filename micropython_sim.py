import asyncio
import time
import datetime
import sys
import threading
import struct # For packing UID in CAN response

# --- Mock micropython module ---
class MicropythonModule:
    def __init__(self):
        self._scheduled_tasks = [] # Simple queue for scheduled tasks

    def alloc_emergency_exception_buf(self, size):
        print(f"SIM: micropython.alloc_emergency_exception_buf({size}) called")

    def schedule(self, func, arg):
        print(f"SIM: micropython.schedule({getattr(func, '__name__', 'unknown_func')}, {arg})")
        # In a real sim, this should integrate with the asyncio loop if func is not an ISR
        # For simplicity here, we'll try to call it soon using asyncio's loop
        # This assumes 'uasyncio' mock is already set up and its loop is accessible
        try:
            loop = uasyncio.get_event_loop()._loop # Accessing the underlying asyncio loop
            if loop:
                loop.call_soon(func, arg)
            else:
                # Fallback: execute immediately (might not be accurate for ISR context)
                print("SIM: micropython.schedule - asyncio loop not found, calling directly.")
                func(arg)
        except Exception as e:
            print(f"SIM: micropython.schedule - Error during scheduling/call: {e}")
            # Fallback if loop access fails
            # func(arg)

micropython = MicropythonModule()

# --- Minimal AFECommand for the simulator ---
class AFECommandSim:
    getSerialNumber = 0x0
    # Add other commands if the simulator needs to react to them specifically

# --- Mock pyb module ---
class PybModule:
    class Pin:
        OUT_PP = "OUT_PP_SIM"
        PULL_NONE = "PULL_NONE_SIM"
        # Add other pin modes/constants if used

        def __init__(self, id, mode=None, pull=None, value=None):
            self.id = id
            self._value = value
            print(f"SIM: pyb.Pin({id}, mode={mode}, pull={pull}, value={value}) initialized.")

        def init(self, mode=None, pull=None, value=None):
            if value is not None:
                self._value = value
            print(f"SIM: pyb.Pin({self.id}).init(mode={mode}, pull={pull}, value={value}). Current value: {self._value}")

        def value(self, x=None):
            if x is None:
                print(f"SIM: pyb.Pin({self.id}).value() read -> {self._value}")
                return self._value
            else:
                self._value = x
                print(f"SIM: pyb.Pin({self.id}).value({x}) set.")

        # Mock common pin names if your code uses pyb.Pin.cpu.E12 etc.
        # This requires knowing which pins are used. For now, a generic Pin class.
        # A more advanced mock could have a dictionary of pin objects.
        # For simplicity, if code does `pyb.Pin.cpu.E12`, it would need `pyb.Pin.cpu` to be an object
        # that then has an `E12` attribute of type `Pin`.
        # Let's add a simple way to get named pins for now.
        _pins = {}
        @classmethod
        def get_pin(cls, name_str): # e.g. "E12"
            if name_str not in cls._pins:
                cls._pins[name_str] = cls(name_str) # Auto-create pin on first access
            return cls._pins[name_str]

    # To support pyb.Pin.cpu.E12 style access:
    class PinCPU:
        def __getattr__(self, name):
            # This allows pyb.Pin.cpu.ANY_PIN_NAME
            print(f"SIM: pyb.Pin.cpu.{name} accessed, returning mock Pin instance.")
            return PybModule.Pin.get_pin(name)

    cpu = PinCPU() # This makes pyb.Pin.cpu work

    class CAN:
        # Constants
        NORMAL = 0
        LOOPBACK = 1
        SILENT = 2
        SILENT_LOOPBACK = 3
        STOPPED = 1 # Example value, check real MicroPython for pyb.CAN.STOPPED
        MASK16 = 1 # Example value
        # Add other states like WARNING, ERROR_PASSIVE, BUS_OFF if needed by logic

        def __init__(self, bus_id, **kwargs):
            print(f"SIM: pyb.CAN({bus_id}, {kwargs}) initialized")
            self.bus_id = bus_id
            self._rx_buffer = [] # Stores (id, is_extended, fmi, data_bytes)
            self._rx_callback_func = None
            self._rx_callback_arg = None # Typically the bus ID or a reason code
            self._state = PybModule.CAN.STOPPED # Initial state

        def init(self, mode, extframe=False, prescaler=0, sjw=0, bs1=0, bs2=0, auto_restart=False, **kwargs):
            print(f"SIM: pyb.CAN({self.bus_id}).init(mode={mode}, extframe={extframe}, prescaler={prescaler}, ..., auto_restart={auto_restart})")
            self._state = PybModule.CAN.NORMAL

        def setfilter(self, bank, mode, fifo, params, **kwargs):
            print(f"SIM: pyb.CAN({self.bus_id}).setfilter(bank={bank}, mode={mode}, fifo={fifo}, params={params})")

        def send(self, data, id, timeout=0, rtr=False):
            data_bytes = bytes(data)
            print(f"SIM: pyb.CAN({self.bus_id}).send(data={data_bytes}, id={id}, timeout={timeout}, rtr={rtr})")

            # Check if this is a getSerialNumber command
            if data_bytes and data_bytes[0] == AFECommandSim.getSerialNumber:
                print(f"SIM: Detected AFECommand.getSerialNumber for target CAN ID {id}")
                target_afe_id = (id >> 2) & 0xFF # Extract AFE ID from the request's CAN ID
                response_can_id = (target_afe_id << 2) | (1 << 10) # Slave bit set

                # Mock UID (3 parts, 32-bit each)
                mock_uid_parts = [0x12345678, 0xABCDEF01, 0x98765432]
                max_chunks = 3

                for i, uid_part in enumerate(mock_uid_parts):
                    chunk_id = i + 1 # Chunks are 1-indexed
                    chunk_info_byte = (max_chunks << 4) | chunk_id
                    payload_bytes = struct.pack('>I', uid_part) # Pack as big-endian for consistency with some devices
                                                              # AFE.py uses bytes_to_u32 which is little-endian ('<I')
                                                              # Let's use little-endian to match AFE.py's parsing
                    payload_bytes = struct.pack('<I', uid_part)

                    # The AFE.py expects chunk_payload[0] to be the channel for some commands,
                    # but for getSerialNumber, the payload starts directly with UID data.
                    # The response command byte is AFECommand.getSerialNumber
                    response_data_bytes = bytes([AFECommandSim.getSerialNumber, chunk_info_byte]) + payload_bytes
                    
                    # Ensure response_data_bytes is not longer than 8 (CAN payload limit)
                    # Command (1) + ChunkInfo (1) + UID part (4) = 6 bytes. This is fine.

                    response_msg_tuple = (response_can_id, False, 0, response_data_bytes)
                    self._rx_buffer.append(response_msg_tuple)
                    print(f"SIM: Queued getSerialNumber response chunk {chunk_id}/{max_chunks}: {response_msg_tuple}")
            else:
                # Default behavior: loop back the sent message (or simulate external device receiving it)
                # For most tests, we might not want to automatically loop back every message.
                # The current behavior adds it to its own RX buffer, which is useful if RxDeviceCAN polls this.
                msg_tuple = (id, False, 0, data_bytes) # Using original ID
                self._rx_buffer.append(msg_tuple)
                print(f"SIM: Message {msg_tuple} added to internal RX buffer (default send behavior). RX buffer size: {len(self._rx_buffer)}")

            if self._rx_callback_func:
                print(f"SIM: RX callback is registered. Scheduling it via micropython.schedule.")
                # Pass self (the CAN bus instance) and reason 1 (RX_MSG_PENDING)
                micropython.schedule(self._rx_callback_func, (self, 1))
            
            return len(data)

        def recv(self, fifo, list_or_buf=None, timeout=5000):
            print(f"SIM: pyb.CAN({self.bus_id}).recv(fifo={fifo}, timeout={timeout}) called.")
            if not self._rx_buffer:
                print("SIM: RX buffer empty.")
                # Behavior on timeout: MicroPython's recv might raise an OSError on timeout
                # or return with no message. For sim, let's return a 0-id message or raise.
                # For now, let's assume it would block or return indicating no message.
                # If list_or_buf is provided, it's for filling.
                # Returning a 4-tuple (id, is_extended, fmi, data_bytes_array) if list_or_buf is None
                return (0, False, 0, b'') # No message

            msg_id, is_ext, fmi, data_bytes = self._rx_buffer.pop(0)
            print(f"SIM: Popped message from RX buffer: {(msg_id, is_ext, fmi, data_bytes)}")

            if list_or_buf is not None and isinstance(list_or_buf, list) and len(list_or_buf) == 4:
                list_or_buf[0] = msg_id
                list_or_buf[1] = is_ext
                list_or_buf[2] = fmi
                # list_or_buf[3] is a memoryview(bytearray(8))
                # We need to copy data_bytes into it.
                data_mv = list_or_buf[3]
                num_bytes_to_copy = min(len(data_bytes), len(data_mv))
                for i in range(num_bytes_to_copy):
                    data_mv[i] = data_bytes[i]
                # If data_bytes is shorter than memoryview, rest of memoryview is untouched (or should be zeroed by user)
                # If data_bytes is longer, it's truncated.
                # The actual CAN hardware might only provide up to 8 bytes.
                print(f"SIM: Filled provided list: {list_or_buf}")
                return None # In-place modification
            else:
                # If no buffer provided or wrong type, return the tuple (original MicroPython behavior)
                return (msg_id, is_ext, fmi, data_bytes)

        def any(self, fifo):
            is_any = len(self._rx_buffer) > 0
            print(f"SIM: pyb.CAN({self.bus_id}).any(fifo={fifo}) -> {is_any}. RX buffer size: {len(self._rx_buffer)}")
            return is_any

        def rxcallback(self, fifo, func):
            print(f"SIM: pyb.CAN({self.bus_id}).rxcallback(fifo={fifo}, func={getattr(func, '__name__', 'unknown_func')}) registered.")
            self._rx_callback_func = func

        def state(self):
            print(f"SIM: pyb.CAN({self.bus_id}).state() -> {self._state}")
            return self._state

        def restart(self):
            print(f"SIM: pyb.CAN({self.bus_id}).restart()")
            self._state = PybModule.CAN.NORMAL # Simulate successful restart
            self._rx_buffer.clear() # Clear buffer on restart

    def millis(self):
        # print("SIM: pyb.millis() called") # Can be too verbose
        return int(time.monotonic() * 1000)

pyb = PybModule()


# --- Mock uasyncio module (using Python's asyncio) ---
class UasyncioShim:
    def __init__(self):
        self._loop = None

    def get_event_loop(self):
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
        return self # In MicroPython, get_event_loop() often returns the loop itself which has create_task etc.

    def create_task(self, coro):
        if self._loop is None: # Ensure loop exists if get_event_loop wasn't called explicitly before create_task
            self.get_event_loop()
        return self._loop.create_task(coro)

    async def sleep(self, t_s):
        await asyncio.sleep(t_s)

    async def sleep_ms(self, t_ms):
        await asyncio.sleep(t_ms / 1000.0)

    def run_forever(self): # This method is on the loop object
        if self._loop:
            try:
                print("SIM: Starting asyncio event loop run_forever().")
                self._loop.run_forever()
            except KeyboardInterrupt:
                print("SIM: asyncio loop interrupted.")
            finally:
                if self._loop.is_running():
                    self._loop.stop()
                # self._loop.close() # Closing should be handled carefully, esp. if run in a thread
                print("SIM: asyncio loop finished run_forever().")
        else:
            print("SIM: uasyncio.run_forever() called but no loop was properly retrieved/set.")

    def run(self, coro): # For uasyncio.run(main())
        return asyncio.run(coro)

uasyncio = UasyncioShim()

# --- Mock _thread module ---
class ThreadModule:
    _thread_id_counter = 0
    _active_threads = []

    def start_new_thread(self, func, args_tuple):
        ThreadModule._thread_id_counter += 1
        thread_id = ThreadModule._thread_id_counter
        print(f"SIM: _thread.start_new_thread for {getattr(func, '__name__', 'unknown_func')} with args {args_tuple}. Assigning ID {thread_id}")

        # Special handling for loop.run_forever
        # func will be UasyncioShim.run_forever
        if hasattr(func, '__self__') and isinstance(func.__self__, UasyncioShim) and func.__name__ == 'run_forever':
            target_loop = func.__self__._loop # Get the actual asyncio loop
            if target_loop:
                print(f"SIM: Detected loop.run_forever. Starting asyncio loop in a new daemon thread.")
                thread = threading.Thread(target=target_loop.run_forever, args=(), daemon=True)
                thread.start()
                ThreadModule._active_threads.append(thread)
                return thread_id
            else:
                print("SIM: ERROR - loop.run_forever called but target_loop not found in UasyncioShim.")
                return -1 # Indicate error
        else:
            # For other functions, run them in a new thread.
            # This is a simple simulation; real MicroPython _thread might have different GIL behavior.
            print(f"SIM: Running function {getattr(func, '__name__', 'unknown_func')} in a new daemon thread.")
            thread = threading.Thread(target=func, args=args_tuple, daemon=True)
            thread.start()
            ThreadModule._active_threads.append(thread)
            return thread_id

_thread = ThreadModule()


# --- Stubs for custom modules ---

# my_utilities
class MockP:
    async def print(self, *args, **kwargs):
        print("SIM_P:", *args, **kwargs)

    async def machine(self):
        # print("SIM_P: machine called")
        await uasyncio.sleep_ms(1)

class MockWDT:
    def feed(self):
        # print("SIM_WDT: feed() called")
        pass

class MockJSONLogger:
    def __init__(self, keep_file_open=False, filename="log.json", max_entries=1000):
        print(f"SIM: JSONLogger initialized (keep_file_open={keep_file_open}, filename={filename})")
        self.log_queue = []

    def log(self, level, message, *args):
        # In real code, level might be an enum or int. Assuming string for sim.
        formatted_message = message.format(*args) if args else message
        print(f"SIM_LOGGER: [{level}] {formatted_message}")

    async def machine(self):
        await uasyncio.sleep_ms(1)

    async def writer_main_loop(self):
        print("SIM_LOGGER: writer_main_loop started")
        while True:
            await uasyncio.sleep_ms(100)

def rtc_unix_timestamp():
    return int(time.time())

class MockRTC:
    def datetime(self):
        dt = datetime.datetime.now()
        return (dt.year, dt.month, dt.day, dt.weekday(), dt.hour, dt.minute, dt.second, 0)

rtc = MockRTC()

def rtc_datetime_pretty():
    dt_tuple = rtc.datetime()
    return f"{dt_tuple[0]}-{dt_tuple[1]:02d}-{dt_tuple[2]:02d} {dt_tuple[4]:02d}:{dt_tuple[5]:02d}:{dt_tuple[6]:02d}"

# Simulator's versions of millis, is_timeout, and is_delay for the my_utilities mock
def sim_millis():
    """Uses the mocked pyb.millis()"""
    return pyb.millis()

def sim_ticks_diff(new, old):
    """Basic ticks_diff for monotonic time, assumes no wraparound in sim context."""
    return new - old

def sim_is_timeout(timestamp_ms, timeout_ms):
    return timeout_ms != 0 and sim_ticks_diff(sim_millis(), timestamp_ms) > timeout_ms

def sim_is_delay(timestamp_ms, delay_ms):
    return delay_ms != 0 and sim_ticks_diff(sim_millis(), timestamp_ms) < delay_ms

# my_RxDeviceCAN
# This mock is kept if you want to stub out your RxDeviceCAN.
# If you want to run your *actual* my_RxDeviceCAN.py, you'd remove this
# and ensure my_RxDeviceCAN.py is importable and relies on the mocked pyb/micropython.
class MockRxDeviceCAN: # Kept as per original simulator structure
    def __init__(self, can_bus, use_rxcallback=True, logger=None, node_id=0x7F, name="RxDeviceCAN"): # Added logger to match HUB.initialize_can_hub
        print(f"SIM: MockRxDeviceCAN initialized (use_rxcallback={use_rxcallback}, node_id={node_id}, name={name})")
    async def main_loop(self): # This is what main.py calls
        print("SIM: RxDeviceCAN.main_loop started")
        while True:
            await uasyncio.sleep_ms(100)

# HUB
class MockHUBDevice:
    def __init__(self, can_bus, logger, use_rxcallback=False, use_automatic_restart=False):
        print(f"SIM: HUBDevice initialized (use_rxcallback={use_rxcallback}, use_automatic_restart={use_automatic_restart})")
        self.logger = logger
        self.afe_devices_max = 0 # Ensure all attributes exist
        self.discovery_active = False
        self.rx_process_active = False
        self.use_tx_delay = False
        self.afe_manage_active = False
        self.tx_delay_ms = 0
        self.afe_id_min = 0
        self.afe_id_max = 0

    async def main_loop(self):
        print("SIM: HUBDevice.main_loop started")
        while True:
            await uasyncio.sleep_ms(100)

async def mock_initialize_can_hub(can_bus, logger, use_rxcallback=False, use_automatic_restart=False):
    print("SIM: initialize_can_hub called")
    hub = MockHUBDevice(can_bus, logger, use_rxcallback, use_automatic_restart)
    # If using the actual RxDeviceCAN, you would import and instantiate it here:
    # from my_RxDeviceCAN import RxDeviceCAN
    # rx_device_can = RxDeviceCAN(can_bus, use_rxcallback=use_rxcallback) # logger might be needed if RxDeviceCAN uses it
    # For now, using the MockRxDeviceCAN as per the existing simulator structure:
    rx_device_can = MockRxDeviceCAN(can_bus, use_rxcallback=use_rxcallback, logger=logger)
    return can_bus, hub, rx_device_can

# my_simple_server
class MockMySimpleServer:
    def __init__(self, hub_instance):
        print("SIM: MySimpleServer initialized")
        self.running = False

    async def main_loop(self):
        print("SIM: MySimpleServer.main_loop started")
        while self.running: # Will be set by main.py
            await uasyncio.sleep_ms(200)
        print("SIM: MySimpleServer.main_loop presumably stopped")

    async def sync_ntp_loop(self):
        print("SIM: MySimpleServer.sync_ntp_loop started")
        while self.running: # Will be set by main.py
            await uasyncio.sleep(1) # Simulate NTP sync

class MockAsyncWebServer:
    def __init__(self, hub_instance):
        print("SIM: AsyncWebServer initialized")

    async def start(self):
        print("SIM: AsyncWebServer.start() called - server mock running")
        while True:
            await uasyncio.sleep(1)


# --- Helper to inject mocks into sys.modules ---
def _setup_mocks():
    # Create a module-like object for my_utilities
    my_utilities_module = type(sys)('my_utilities')
    my_utilities_module.p = MockP()
    my_utilities_module.wdt = MockWDT()
    my_utilities_module.JSONLogger = MockJSONLogger
    my_utilities_module.rtc_unix_timestamp = rtc_unix_timestamp
    my_utilities_module.rtc = rtc
    my_utilities_module.rtc_datetime_pretty = rtc_datetime_pretty
    my_utilities_module.millis = sim_millis
    my_utilities_module.is_timeout = sim_is_timeout
    my_utilities_module.is_delay = sim_is_delay
    sys.modules['my_utilities'] = my_utilities_module

    # If you want to use the *actual* my_RxDeviceCAN.py, comment out or remove the next two lines.
    # Ensure my_RxDeviceCAN.py is in the same directory or python path.
    # The mocked pyb and micropython should allow it to run.
    # my_RxDeviceCAN_module = type(sys)('my_RxDeviceCAN') # Kept for consistency with original sim
    # my_RxDeviceCAN_module.RxDeviceCAN = MockRxDeviceCAN # Kept
    # sys.modules['my_RxDeviceCAN'] = my_RxDeviceCAN_module # Kept

    HUB_module = type(sys)('HUB')
    HUB_module.initialize_can_hub = mock_initialize_can_hub
    sys.modules['HUB'] = HUB_module

    my_simple_server_module = type(sys)('my_simple_server')
    my_simple_server_module.MySimpleServer = MockMySimpleServer
    my_simple_server_module.AsyncWebServer = MockAsyncWebServer
    sys.modules['my_simple_server'] = my_simple_server_module

    sys.modules['pyb'] = pyb
    sys.modules['uasyncio'] = uasyncio
    sys.modules['micropython'] = micropython
    sys.modules['_thread'] = _thread

_setup_mocks()

print("SIM: MicroPython mocks initialized and injected into sys.modules.")
print("SIM: You can now import 'main.py' or run its content.")
print("SIM: For example, create a run_sim.py with 'import micropython_sim; import main'")

if __name__ == "__main__":
    print("SIM: micropython_sim.py executed directly.")
    print("SIM: This script primarily sets up mocks. To run your main.py, create a separate runner script.")
    print("SIM: Example runner (e.g., run_my_app.py):")
    print("SIM: import micropython_sim  # This sets up mocks")
    print("SIM: import main            # This executes your main.py script")
    print("SIM: import time")
    print("SIM: try:")
    print("SIM:     while True: time.sleep(1) # Keep main thread alive for async tasks")
    print("SIM: except KeyboardInterrupt:")
    print("SIM:     print('Simulation stopped by user.')")
    print("SIM: finally:")
    print("SIM:     if micropython_sim.uasyncio._loop and micropython_sim.uasyncio._loop.is_running():")
    print("SIM:         micropython_sim.uasyncio._loop.call_soon_threadsafe(micropython_sim.uasyncio._loop.stop)")
