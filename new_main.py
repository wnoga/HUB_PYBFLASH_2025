import pyb
import uasyncio as asyncio
import time
import os
import network
import socket
import struct

# ============================================================================
# 0. LIGHTWEIGHT ASYNC QUEUE FOR UASYNCIO
# ============================================================================
class Queue:
    """A simple event-driven queue compatible with MicroPython's uasyncio."""
    def __init__(self):
        self._queue = []
        self._ev = asyncio.Event()

    async def put(self, val):
        self._queue.append(val)
        self._ev.set()

    async def get(self):
        while not self._queue:
            self._ev.clear()
            await self._ev.wait()
        return self._queue.pop(0)

    def empty(self):
        return len(self._queue) == 0

    def get_nowait(self):
        if not self._queue:
            raise IndexError("queue is empty")
        return self._queue.pop(0)

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
        print("CANController: send_frame:", msg_id, data)
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
class CommandRequest:
    def __init__(self, cmd_id: int, payload: bytes, timeout_ms: int):
        self.cmd_id = cmd_id
        self.payload = payload
        self.timeout_ms = timeout_ms
        self.event = asyncio.Event()
        self.result = None
        self.created_at = time.ticks_ms()

class AFEDevice:
    def __init__(self, afe_id: int, can_controller, hub, stale_buffer_ttl_ms: int = 3000):
        self.afe_id = afe_id
        self.can = can_controller
        self.hub = hub
        self.stale_buffer_ttl_ms = stale_buffer_ttl_ms
        
        self.rx_queue = Queue()
        self.cmd_queue = Queue()
        
        self._pending_requests = {}
        self._assembly_buffers = {}

    async def execute_command(self, cmd_id: int, payload: bytes = b'', timeout_ms: int = 2000):
        req = CommandRequest(cmd_id, payload, timeout_ms)
        self._pending_requests[cmd_id] = req
        await self.cmd_queue.put(req)

        try:
            # FIX: Convert milliseconds to floating point seconds for modern uasyncio
            await asyncio.wait_for(req.event.wait(), timeout_ms / 1000.0)
            return req.result
        except asyncio.TimeoutError:
            msg = "AFE-{} cmd 0x{:X} timed out".format(self.afe_id, cmd_id)
            await self.hub.logger.log("WARN", msg)
            return None
        finally:
            if self._pending_requests.get(cmd_id) is req:
                self._pending_requests.pop(cmd_id, None)

    async def command_queue_worker(self):
        while True:
            req = await self.cmd_queue.get()
            await self.send_command(req.cmd_id, req.payload)
            # FIX: Do not double-wait the event here; let execute_command handle the suspension.

    async def buffer_cleanup_loop(self):
        while True:
            await asyncio.sleep_ms(1000)
            now = time.ticks_ms()
            stale_keys = []

            for cmd_id, buf in self._assembly_buffers.items():
                if time.ticks_diff(now, buf['last_updated']) > self.stale_buffer_ttl_ms:
                    stale_keys.append(cmd_id)

            for cmd_id in stale_keys:
                del self._assembly_buffers[cmd_id]
                msg = "AFE-{} purged stale buffer for cmd 0x{:X}".format(self.afe_id, cmd_id)
                await self.hub.logger.log("WARN", msg)

    async def process_loop(self):
        while True:
            msg_id, data = await self.rx_queue.get()
            
            incoming_afe_id = msg_id >> 4
            # FIX: Extracted cmd_id directly from the CAN ID mask to match send_command format
            cmd_id = msg_id & 0x0F
            
            if incoming_afe_id != self.afe_id or len(data) < 1:
                continue

            # Protocol Reassembly Logic mapping payload indices assuming data is sequence byte
            seq_byte = data[0]
            total_msgs = ((seq_byte >> 4) & 0x0F) + 1
            msg_idx = seq_byte & 0x0F
            chunk_payload = data[1:]
            now = time.ticks_ms()

            if cmd_id not in self._assembly_buffers:
                self._assembly_buffers[cmd_id] = {
                    'total': total_msgs,
                    'chunks': {},
                    'last_updated': now
                }

            buf = self._assembly_buffers[cmd_id]
            buf['chunks'][msg_idx] = chunk_payload
            buf['last_updated'] = now

            if len(buf['chunks']) == buf['total']:
                full_payload = b"".join(buf['chunks'][i] for i in range(buf['total']))
                del self._assembly_buffers[cmd_id]
                await self._handle_complete_message(cmd_id, full_payload)

    async def send_command(self, cmd_id: int, payload: bytes):
        full_can_id = (self.afe_id << 4) | (cmd_id & 0x0F)
        await self.can.send_frame(full_can_id, payload)

    async def _handle_complete_message(self, cmd_id: int, payload: bytes):
        if cmd_id in self._pending_requests:
            req = self._pending_requests[cmd_id]
            req.result = payload
            req.event.set()
        else:
            msg = "AFE-{} spontaneous data for cmd 0x{:X}".format(self.afe_id, cmd_id)
            await self.hub.logger.log("INFO", msg)

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
            msg_id, data = await self.rx_queue.get()
            print(msg_id, data)
            afe_id = msg_id >> 4
            if afe_id in self.afes:
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