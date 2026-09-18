import uasyncio as asyncio
import time
import os

# ============================================================================
# 1. CAN CONTROLLER INTERFACE
# ============================================================================
class CANController:
    """Wrapper around hardware CAN controller using asyncio queues."""
    def __init__(self, can_id=0, baudrate=500000):
        # Initialize hardware CAN driver here
        # e.g., self.can = CAN(can_id, CAN.NORMAL, baudrate=baudrate)
        self.tx_queue = asyncio.Queue()
        self.rx_listeners = []  # Callback routines or queues registered to listen

    def register_listener(self, queue):
        """Register a queue to receive incoming raw CAN frames."""
        self.rx_listeners.append(queue)

    async def send_frame(self, msg_id: int, data: bytes):
        """Public interface to enqueue outgoing messages."""
        await self.tx_queue.put((msg_id, data))

    async def tx_loop(self):
        """Background task: Pulls from TX queue and sends to hardware."""
        while True:
            msg_id, data = await self.tx_queue.get()
            # self.can.send(data, msg_id)  # Hardware driver call
            await asyncio.sleep_ms(2)      # Yield control

    async def rx_loop(self):
        """Background task: Reads hardware CAN and distributes to queues."""
        while True:
            # Poll hardware or wait on hardware interrupt
            # if self.can.any():
            #     msg_id, rtr, fmi, data = self.can.recv()
            
            # Simulated incoming frame:
            await asyncio.sleep_ms(100) 
            msg_id, data = 0x101, b'\x00\x01\x02\x03' # Simulated packet

            # Dispatch frame to all active listener queues
            for queue in self.rx_listeners:
                await queue.put((msg_id, data))


# ============================================================================
# 2. SUBDEVICE (AFEDevice)
# ============================================================================
class AFEDevice:
    """Represents an individual Analog Front-End device on the CAN bus."""
    def __init__(self, afe_id: int, can_controller: CANController, hub):
        self.afe_id = afe_id
        self.can = can_controller
        self.hub = hub
        self.rx_queue = asyncio.Queue()

    async def send_command(self, cmd_id: int, payload: bytes):
        """Send message using this subdevice's unique CAN ID frame layout."""
        full_can_id = (self.afe_id << 4) | (cmd_id & 0x0F)
        await self.can.send_frame(full_can_id, payload)

    async def process_loop(self):
        """Task processing incoming CAN messages designated for this AFE."""
        while True:
            msg_id, data = await self.rx_queue.get()
            
            # Extract AFE ID from message (Assuming ID format matches)
            if (msg_id >> 4) == self.afe_id:
                # Process data payload
                log_entry = f"AFE-{self.afe_id} received cmd 0x{msg_id & 0x0F:X}: {data.hex()}"
                
                # Pass data up to SD Logger via Hub
                await self.hub.logger.log("AFE", log_entry)


# ============================================================================
# 3. MAIN HUB DEVICE
# ============================================================================
class HUBDevice:
    """Main System Manager: coordinates CAN routing, AFEs, and Logging."""
    def __init__(self, can: CANController, logger):
        self.can = can
        self.logger = logger
        self.rx_queue = asyncio.Queue()
        self.can.register_listener(self.rx_queue)
        
        # Instantiate subdevices (AFEs) with distinct CAN IDs
        self.afes = {
            1: AFEDevice(afe_id=1, can_controller=self.can, hub=self),
            2: AFEDevice(afe_id=2, can_controller=self.can, hub=self)
        }

    async def router_loop(self):
        """Demux incoming raw CAN messages to the correct AFEDevice queue."""
        while True:
            msg_id, data = await self.rx_queue.get()
            
            # Route to matching subdevice based on CAN ID pattern
            afe_id = msg_id >> 4
            if afe_id in self.afes:
                await self.afes[afe_id].rx_queue.put((msg_id, data))

    async def run(self):
        """Spin up Hub routing loop and subdevice worker loops."""
        tasks = [asyncio.create_task(self.router_loop())]
        for afe in self.afes.values():
            tasks.append(asyncio.create_task(afe.process_loop()))
        await asyncio.gather(*tasks)


# ============================================================================
# 4. SD CARD LOGGER
# ============================================================================
class SDLogger:
    """Non-blocking file logger using an internal queue."""
    def __init__(self, mount_point="/sd", filename="system.log"):
        self.filepath = f"{mount_point}/{filename}"
        self.queue = asyncio.Queue()
        # SD Card Hardware Mount (uncomment for actual usage):
        # import machine, sdcard
        # sd = sdcard.SDCard(machine.SPI(1), machine.Pin(15))
        # os.mount(sd, mount_point)

    async def log(self, level: str, message: str):
        """Enqueue a log string with timestamp."""
        timestamp = time.time()
        entry = f"[{timestamp}] [{level}] {message}\n"
        await self.queue.put(entry)

    async def writer_loop(self):
        """Background task writing logs in batches to minimize SD wear/blocking."""
        while True:
            entry = await self.queue.get()
            try:
                # Open, write, close pattern to safeguard SD card integrity
                with open(self.filepath, "a") as f:
                    f.write(entry)
                    # Flush remaining queue items in same open handle
                    while not self.queue.empty():
                        f.write(self.queue.get_nowait())
            except Exception as e:
                print(f"SD Write Error: {e}")
            await asyncio.sleep_ms(100)


# ============================================================================
# 5. ASYNC WEB SERVER & TIME SYNC
# ============================================================================
class WebServer:
    """Lightweight HTTP server providing web interface and time-sync API."""
    def __init__(self, hub_device: HUBDevice, host="0.0.0.0", port=80):
        self.hub = hub_device
        self.host = host
        self.port = port

    async def start(self):
        """Start async socket server listener."""
        await asyncio.start_server(self.handle_client, self.host, self.port)

    async def handle_client(self, reader, writer):
        """Handle incoming HTTP requests asynchronously."""
        request_line = await reader.readline()
        req_str = request_line.decode('utf-8')
        
        # Read remaining headers
        while True:
            header = await reader.readline()
            if header == b"\r\n" or not header:
                break

        # Basic Route Parser
        if "POST /api/time" in req_str:
            # Endpoint to sync PC RTC time to MicroPython
            # Expected format: POST /api/time?epoch=1710000000
            try:
                epoch_str = req_str.split("epoch=")[1].split(" ")[0]
                new_time = int(epoch_str)
                # Apply to System RTC:
                # import machine
                # machine.RTC().datetime(new_time_tuple)
                await self.hub.logger.log("SYS", f"Time synced: {new_time}")
                response = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\":\"ok\"}"
            except Exception:
                response = "HTTP/1.1 400 Bad Request\r\n\r\n"

        elif "GET /" in req_str:
            # HTML Dashboard
            html = "<html><body><h1>HUB Controller</h1><p>Status: Running</p></body></html>"
            response = f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(html)}\r\n\r\n{html}"
        else:
            response = "HTTP/1.1 404 Not Found\r\n\r\n"

        await writer.awrite(response.encode('utf-8'))
        await writer.aclose()


# ============================================================================
# 6. MAIN APPLICATION ENTRY POINT
# ============================================================================
async def main():
    # Hardware instances
    can_bus = CANController(can_id=0)
    logger = SDLogger()
    hub = HUBDevice(can=can_bus, logger=logger)
    web_server = WebServer(hub_device=hub)

    # Launch background task event loop
    await asyncio.gather(
        can_bus.tx_loop(),
        can_bus.rx_loop(),
        logger.writer_loop(),
        hub.run(),
        web_server.start()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("System stopped.")