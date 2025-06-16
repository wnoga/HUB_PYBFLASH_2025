import network
import socket as socket
import time
import uasyncio
import ujson
from HUB import HUBDevice
import pyb
import struct
import machine
import uos # For file operations

from my_utilities import wdt
from my_utilities import millis, is_timeout, is_delay
from my_utilities import p, VerbosityLevel
from my_utilities import rtc, rtc_synced, rtc_datetime_pretty, rtc_unix_timestamp

import uasyncio as asyncio

# NTP constants
NTP_DELTA = 2208988800  # Seconds between NTP epoch (1900) and Unix epoch (1970)
NTP_HOST = "pool.ntp.org"

class AsyncWebServer:
    def __init__(self, hub: HUBDevice, dhcp=True, static_ip_config=None, port=5555):
        """
        dhcp: True to use DHCP, False to use static IP
        static_ip_config: tuple (ip, subnet, gateway, dns)
        port: Port to bind the server on
        """
        self.hub = hub
        self.dhcp = dhcp
        self.static_ip_config = static_ip_config
        self.port = port
        self.server = None
        self.lan = network.LAN()
        self.lan_connected = False
        self.last_lan_check_ms = 0
        self.ntp_synced = False
        
        self.main_loop_yield_wait_ms = 10
        self.sync_ntp_loop_yield_wait_s = 10
        self.sync_ntp_every_s = 10*60
        
        self.procedure_results = {} # Stores results from hub callbacks
        self.procedure_events = {}  # Stores uasyncio.Event for synchronization
        self.max_procedures_keep_len = 32
        
    def get_webpage_address(self):
        """
        Returns the current IP address of the server.
        Returns None if the LAN is not connected.
        """
        if self.lan_connected:
            return "http://{}:{}".format(self.lan.ifconfig()[0],self.port)
        return None
    
    async def print_webpage_address(self): # Changed to async def
        await p.print(self.get_webpage_address()) # Added await

    async def connect_ethernet(self):
        # The next line was already present
        self.lan.active(True)        
        if not self.dhcp and self.static_ip_config:            
            ip, subnet, gateway, dns = self.static_ip_config
            self.lan.ifconfig((ip, subnet, gateway, dns))

        await p.print("Waiting for Ethernet connection...", end="") # Added await
        timeout = 10
        while not self.lan.isconnected() and timeout > 0:
            await p.print(".", end="") # Added await
            await uasyncio.sleep(1) # Use asynchronous sleep
            timeout -= 1
        await p.print("") # Newline after dots, Added await

        if not self.lan.isconnected():
            raise RuntimeError("Ethernet connection failed")

        await p.print("Ethernet connected. IP:", self.lan.ifconfig()[0]) # Added await
        self.lan_connected = True
        
    async def hub_cb(self,msg:dict=None): # Changed to async def
        device_id = msg.get("device_id", None)
        if not device_id:
            await p.print("hub_cb: device_id missing in callback message.") # Added await
            return

        # Prepare the result to be sent back to the web client
        my_dict = msg.copy()
        if 'frame' in my_dict: # Assuming 'frame' is internal and not for web client
            my_dict.pop('frame')
        if 'callback' in my_dict: # The callback itself shouldn't be in the response
            my_dict.pop('callback')
        
        # Limit the size of procedure_results
        if device_id not in self.procedure_results and len(self.procedure_results) >= self.max_procedures_keep_len:
            await p.print("AsyncWebServer: procedure_results cache full. Evicting an arbitrary item before adding for AFE {device_id}.".format(device_id)) # Added await
            try:
                self.procedure_results.popitem() # Removes an arbitrary (key, value) pair
            except KeyError: # pragma: no cover
                pass # Dictionary was empty, should not happen if len >= max_len
        try:
            # Store the processed result for handle_procedure to pick up
            self.procedure_results[device_id] = ujson.dumps(my_dict).encode()
        except Exception as e:
            await p.print("hub_cb: Error serializing result for AFE {}: {}".format(device_id, e)) # Added await
            self.procedure_results[device_id] = ujson.dumps({"status": "ERROR", "info": "Failed to serialize AFE response"}).encode()

        # Signal the waiting handle_procedure task
        event = self.procedure_events.get(device_id)
        if event:
            event.set()
        else:
            await p.print("hub_cb: No event found for AFE {}. Result stored but not awaited.".format(device_id)) # Added await

    async def handle_procedure(self, request_line):
        request = None
        try:
            request = ujson.loads(request_line.decode())
        except:
            return None
        procedure = request.get("procedure", None)
        if not procedure:
            return None
        elif procedure == "get_all_afe_configuration":
            my_dict = {}
            for afe_device in self.hub.afe_devices: # Renamed afe to afe_device to avoid conflict
                my_dict[afe_device.device_id] = afe_device.configuration
            return ujson.dumps(my_dict).encode()
        elif procedure == "default_get_measurement_last":
            afe_id = request.get("afe_id",None)
            if afe_id is None:
                return ujson.dumps({"status": "ERROR", "info": "afe_id missing"}).encode()
            
            # Limit the number of concurrent procedures
            if afe_id not in self.procedure_events and len(self.procedure_events) >= self.max_procedures_keep_len:
                await p.print("AsyncWebServer: Max concurrent procedures reached for procedure_events. Rejecting new procedure for AFE {}.".format(afe_id)) # Added await
                return ujson.dumps({"status": "ERROR", "info": "Server busy, max concurrent procedures reached"}).encode()

            event = uasyncio.Event()
            self.procedure_events[afe_id] = event
            self.procedure_results.pop(afe_id, None) # Clear previous result

            await self.hub.default_get_measurement_last(afe_id,callback=self.hub_cb) # Added await
            
            try:
                await uasyncio.wait_for(event.wait(), timeout=20.0) # 20 second timeout
                result_data = self.procedure_results.pop(afe_id, None)
                # Event is removed from dict by hub_cb or here on timeout/error
                if self.procedure_events.get(afe_id) == event: # Check if event is still ours
                    self.procedure_events.pop(afe_id, None)
                return result_data
            except uasyncio.TimeoutError:
                await p.print("Timeout waiting for procedure result for AFE {}".format(afe_id)) # Added await
                self.procedure_events.pop(afe_id, None) 
                self.procedure_results.pop(afe_id, None)
                return ujson.dumps({"status": "ERROR", "info": "Timeout waiting for AFE response"}).encode()
            except Exception as e:
                await p.print("Error waiting for event for AFE {}: {}".format(afe_id, e)) # Added await
                self.procedure_events.pop(afe_id, None)
                self.procedure_results.pop(afe_id, None)
                return ujson.dumps({"status": "ERROR", "info": "Server error during procedure"}).encode()
        elif procedure == "hub_close_all":
            await self.hub.close_all()
            return ujson.dumps({"status": "OK"}).encode()
        
        elif procedure == "default_procedure":
            afe_id = request.get("afe_id",None)
            if not afe_id:
                return ujson.dumps({"status": "ERROR"}).encode()
            await self.hub.default_procedure(afe_id)
            return ujson.dumps({"status": "OK"}).encode()
        
        return None

    async def send_control_web_page(self, writer):
        """
        Generates the HTML content for the control web page.

        This method creates a simple HTML page that displays information about
        the connected AFE devices and provides controls for interacting with them.
        It includes device status, configuration, and buttons for triggering
        measurements and other procedures.

        Returns:
            str: The complete HTML content of the control page.
        """
        await writer.awrite("HTTP/1.0 200 OK\r\nContent-type: text/html\r\n\r\n")
        await writer.awrite("""
            <!DOCTYPE html>
            <html>
            <head>
                <title>AFE HUB Control</title>
                <style>
                    body { }
                    .afe-device { border: 1px solid #ccc; margin: 10px; padding: 10px; }
                    .afe-device h3 { margin-top: 0; }
                    button { margin: 5px; padding: 8px; cursor: pointer; }
                    pre { background-color: #eee; padding: 10px; overflow-x: auto; }
                </style>
            </head>""")
        await writer.awrite("""
            <body>
                <h1>AFE HUB Control</h1>
                <p>Current Time: <span id="current-time">{}</span></p>
                <button onclick="updatePage()">Refresh Data</button>
            """.format(rtc_datetime_pretty()))
        
        await writer.awrite(b"<div><h4>Log Files in /sd/logs/:</h4><ul>")
        try:
            log_files = uos.listdir("/sd/logs")
            for log_file in log_files:
                await writer.awrite("<li>{}</li>".format(log_file))
        except OSError:
            await writer.awrite(b"<li>Could not list log files.</li>")
        await writer.awrite(b"</ul></div>")

        if not self.hub.afe_devices:
            await writer.awrite("<p>No AFE devices found.</p>")
        else:
            for afe in self.hub.afe_devices:
                await writer.awrite("""
                <div id="afe-devices-container">
                <div class="afe-device" id="afe-{}">
                    <h3>AFE Device ID: {}</h3>
                    <p>UID: {}</p>
                    <h4>Configuration:</h4>
                    <pre>{}</pre> 
                    """.format(
                        afe.device_id,
                        afe.device_id,
                        afe.unique_id_str or 'N/A',
                        ujson.dumps(afe.configuration))) # Consider if afe.configuration can be very large

                for ch in afe.channels:
                    # Formatting complex objects like ch.last_recieved_data directly
                    # might still be an issue if they are very large.
                    # For now, let's assume ujson.dumps is efficient enough for them.
                    channel_data_str = ujson.dumps(ch.last_recieved_data)
                    await writer.awrite("""
                    <div class="afe-channel">
                        <h5>Channel {}:</h5>
                        <p>Data: {}</p>
                    </div>
                    """.format(ch.name,channel_data_str))
                await writer.awrite("""
                    </div>
                """)
                # Example buttons (ensure functions are defined in your JS)
                # await writer.awrite(f"""
                # <button onclick="getMeasurementLast({afe.device_id})">Get Last Measurement</button>
                # </div>
                # """.encode())
        
        await writer.awrite(b"</div>") # Close afe-devices-container

        await writer.awrite(b"""
                </div>
            </body>
            </html>
        """)


    async def handle_client(self, reader, writer):
        peername = writer.get_extra_info('peername')
        try:
            request_line = await uasyncio.wait_for(reader.readline(), 5)
            if not request_line: # Client closed connection before sending anything
                await p.print("Client {} disconnected before sending request.".format(peername)) # Added await
                # writer.close() # Ensure writer is closed in finally block
                # await writer.wait_closed()
                return

            response = await self.handle_procedure(request_line)
            if response:
                await writer.awrite(response)
            else:
                # If it's not a procedure call, assume it's a GET for the main page.
                # We need to consume the rest of the HTTP headers.
                while True:
                    header_line = await uasyncio.wait_for(reader.readline(), 1) # Timeout for each header line
                    if header_line == b"\r\n" or not header_line: # Empty line signifies end of headers or client closed
                        break
                await self.send_control_web_page(writer)
        except OSError as e:
            if e.args[0] == 104:  # ECONNRESET
                await p.print("Connection reset by peer {}.".format(peername)) # Added await
            else:
                await p.print("OSError in handle_client for {}: {}".format(peername,e)) # Added await
        except uasyncio.TimeoutError:
            await p.print("Timeout in handle_client for {}.".format(peername)) # Added await
        except Exception as e:
            await p.print("Error handling client {}: {}".format(peername,e)) # Added await
        finally:
            try:
                await writer.aclose()
            except:
                pass
        

    async def sync_rtc_with_ntp(self):
        global rtc_synced, p, rtc
        """Syncs the RTC with an NTP server."""
        if not self.lan_connected:
            await p.print("NTP sync: LAN not connected.") # Added await
            return False

        await p.print("Attempting to sync NTP...") # Added await
        s = None # Initialize s
        try:
            # Create a UDP socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(5) # 5 second timeout

            addr = socket.getaddrinfo(NTP_HOST, 123)[0][-1]

            # NTP request packet (minimal: version 3, client mode)
            msg = bytearray(48)
            msg[0] = 0x1B # LI, Version, Mode

            s.sendto(msg, addr)
            await uasyncio.sleep_ms(0) # Yield
            data, _ = s.recvfrom(48) # server_addr is not used

            if data:
                # Extract the transmit timestamp (bytes 40-43)
                secs = struct.unpack("!I", data[40:44])[0]
                unix_secs = secs - NTP_DELTA
                secs_since_2000 = unix_secs - 946684800
                tm = time.gmtime(secs_since_2000)
                rtc.datetime((tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))
                self.ntp_synced = True
                await p.print("NTP sync successful. Time set to: {}".format(time.gmtime())) # Added await
                rtc_synced = True # Update global flag
                if not self.hub.logger.rtc_synced:
                    self.hub.logger.rtc_synced = True
                    self.hub.logger.request_rename_file()
                return True
        except Exception as e:
            await p.print("NTP sync failed: {}".format(e)) # Added await
            return False
        finally:
            if s:
                s.close()
                await uasyncio.sleep_ms(0) # Yield after close


    async def start(self):
        while True:
            try:
                if not self.lan_connected:
                    await p.print("Attempting to reconnect Ethernet...") # Added await
                    try:
                        await self.connect_ethernet() # Call with await
                    except RuntimeError:
                        await p.print("Ethernet reconnection failed. Retrying in 10s.") # Added await
                        await asyncio.sleep(100)
                        continue

                if self.server is None:
                    await p.print("Attempting to start server...") # Added await
                    try:
                        self.server = await asyncio.start_server(self.handle_client, "0.0.0.0", self.port)
                        await p.print("Server running at http://{}:{}".format(self.lan.ifconfig()[0],self.port)) # Added await
                    except Exception as e:
                        await p.print("Failed to start server: {}. Retrying in 10s.".format(e)) # Added await
                        self.server = None # Ensure server is None if start failed
                        await asyncio.sleep(100)
                        continue

                # Periodically check LAN connection status
                if is_timeout(self.last_lan_check_ms, 5000):
                    if not self.lan.isconnected():
                        await p.print("Ethernet disconnected.") # Added await
                        self.lan_connected = False
                        if self.server:
                            self.server.close()
                            await self.server.wait_closed()
                            self.server = None
                    self.last_lan_check_ms = millis()
            except Exception as e:
                await p.print("AsyncWebServer main_loop (start method):",e) # Added await, clarified source
            await asyncio.sleep_ms(self.main_loop_yield_wait_ms) # Yield control
    
    def run(self):
        try:
            asyncio.run(self.start())
        except KeyboardInterrupt:
            print("Server stopped")

    async def sync_ntp_loop(self):
        """Periodically syncs RTC with NTP."""
        while True:
            isSynced = await self.sync_rtc_with_ntp()
            if isSynced:
                await asyncio.sleep(self.sync_ntp_every_s)
            else:
                await asyncio.sleep(self.sync_ntp_loop_yield_wait_s)
    