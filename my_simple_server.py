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

import network
import uasyncio as asyncio
import time

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
    
    def print_webpage_address(self):
        p.print(self.get_webpage_address())

    async def connect_ethernet(self):
        # The next line was already present
        self.lan.active(True)        
        if not self.dhcp and self.static_ip_config:            
            ip, subnet, gateway, dns = self.static_ip_config
            self.lan.ifconfig((ip, subnet, gateway, dns))

        p.print("Waiting for Ethernet connection...", end="")
        timeout = 10
        while not self.lan.isconnected() and timeout > 0:
            p.print(".", end="")
            await uasyncio.sleep(1) # Use asynchronous sleep
            timeout -= 1
        p.print("") # Newline after dots

        if not self.lan.isconnected():
            raise RuntimeError("Ethernet connection failed")

        p.print("Ethernet connected. IP:", self.lan.ifconfig()[0])
        self.lan_connected = True
        
    def hub_cb(self,msg:dict=None):
        device_id = msg.get("device_id", None)
        if not device_id:
            p.print("hub_cb: device_id missing in callback message.")
            return

        # Prepare the result to be sent back to the web client
        my_dict = msg.copy()
        if 'frame' in my_dict: # Assuming 'frame' is internal and not for web client
            my_dict.pop('frame')
        if 'callback' in my_dict: # The callback itself shouldn't be in the response
            my_dict.pop('callback')
        
        # Limit the size of procedure_results
        if device_id not in self.procedure_results and len(self.procedure_results) >= self.max_procedures_keep_len:
            p.print(f"AsyncWebServer: procedure_results cache full. Evicting an arbitrary item before adding for AFE {device_id}.")
            try:
                self.procedure_results.popitem() # Removes an arbitrary (key, value) pair
            except KeyError: # pragma: no cover
                pass # Dictionary was empty, should not happen if len >= max_len
        try:
            # Store the processed result for handle_procedure to pick up
            self.procedure_results[device_id] = ujson.dumps(my_dict).encode()
        except Exception as e:
            p.print("hub_cb: Error serializing result for AFE {}: {}".format(device_id, e))
            self.procedure_results[device_id] = ujson.dumps({"status": "ERROR", "info": "Failed to serialize AFE response"}).encode()

        # Signal the waiting handle_procedure task
        event = self.procedure_events.get(device_id)
        if event:
            event.set()
        else:
            p.print("hub_cb: No event found for AFE {}. Result stored but not awaited.".format(device_id))

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
                p.print(f"AsyncWebServer: Max concurrent procedures reached for procedure_events. Rejecting new procedure for AFE {afe_id}.")
                return ujson.dumps({"status": "ERROR", "info": "Server busy, max concurrent procedures reached"}).encode()

            event = uasyncio.Event()
            self.procedure_events[afe_id] = event
            self.procedure_results.pop(afe_id, None) # Clear previous result

            self.hub.default_get_measurement_last(afe_id,callback=self.hub_cb)
            
            try:
                await uasyncio.wait_for(event.wait(), timeout=20.0) # 20 second timeout
                result_data = self.procedure_results.pop(afe_id, None)
                # Event is removed from dict by hub_cb or here on timeout/error
                if self.procedure_events.get(afe_id) == event: # Check if event is still ours
                    self.procedure_events.pop(afe_id, None)
                return result_data
            except uasyncio.TimeoutError:
                p.print("Timeout waiting for procedure result for AFE {}".format(afe_id))
                self.procedure_events.pop(afe_id, None) 
                self.procedure_results.pop(afe_id, None)
                return ujson.dumps({"status": "ERROR", "info": "Timeout waiting for AFE response"}).encode()
            except Exception as e:
                p.print("Error waiting for event for AFE {}: {}".format(afe_id, e))
                self.procedure_events.pop(afe_id, None)
                self.procedure_results.pop(afe_id, None)
                return ujson.dumps({"status": "ERROR", "info": "Server error during procedure"}).encode()
            
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
                p.print("Client {} disconnected before sending request.".format(peername))
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
                p.print("Connection reset by peer {}.".format(peername))
            else:
                p.print("OSError in handle_client for {}: {}".format(peername,e))
        except uasyncio.TimeoutError:
            p.print("Timeout in handle_client for {}.".format(peername))
        except Exception as e:
            p.print("Error handling client {}: {}".format(peername,e))
        finally:
            try:
                await writer.aclose()
            except:
                pass
        

    async def sync_rtc_with_ntp(self):
        global rtc_synced, p, rtc
        """Syncs the RTC with an NTP server."""
        if not self.lan_connected:
            p.print("NTP sync: LAN not connected.")
            return False

        p.print("Attempting to sync NTP...")
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
                p.print("NTP sync successful. Time set to: {}".format(time.gmtime()))
                rtc_synced = True # Update global flag
                if not self.hub.logger.rtc_synced:
                    self.hub.logger.rtc_synced = True
                    self.hub.logger.request_rename_file()
                return True
        except Exception as e:
            p.print("NTP sync failed: {}".format(e))
            return False
        finally:
            if s:
                s.close()
                await uasyncio.sleep_ms(0) # Yield after close


    async def start(self):
        await self.connect_ethernet() # Call with await
        
        # Start NTP sync loop
        asyncio.create_task(self.sync_ntp_loop())
        
        while True:
            try:
                if not self.lan_connected:
                    p.print("Attempting to reconnect Ethernet...")
                    try:
                        await self.connect_ethernet() # Call with await
                    except RuntimeError:
                        p.print("Ethernet reconnection failed. Retrying in 10s.")
                        await asyncio.sleep(100)
                        continue

                if self.server is None:
                    p.print("Attempting to start server...")
                    try:
                        self.server = await asyncio.start_server(self.handle_client, "0.0.0.0", self.port)
                        p.print("Server running at http://{}:{}".format(self.lan.ifconfig()[0],self.port))
                    except Exception as e:
                        p.print("Failed to start server: {}. Retrying in 10s.".format(e))
                        self.server = None # Ensure server is None if start failed
                        await asyncio.sleep(100)
                        continue

                # Periodically check LAN connection status
                if is_timeout(self.last_lan_check_ms, 5000):
                    if not self.lan.isconnected():
                        p.print("Ethernet disconnected.")
                        self.lan_connected = False
                        if self.server:
                            self.server.close()
                            await self.server.wait_closed()
                            self.server = None
                    self.last_lan_check_ms = millis()
            except Exception as e:
                p.print("HUB main_loop:",e)
            await asyncio.sleep_ms(self.main_loop_yield_wait_ms) # Yield control
    def run(self):
        try:
            asyncio.run(self.start())
        except KeyboardInterrupt:
            print("Server stopped")


# === Example usage ===
    async def sync_ntp_loop(self):
        """Periodically syncs RTC with NTP."""
        while True:
            isSynced = await self.sync_rtc_with_ntp()
            if isSynced:
                await asyncio.sleep(self.sync_ntp_every_s)
            else:
                await asyncio.sleep(self.sync_ntp_loop_yield_wait_s)


# DHCP (default):
# server = AsyncWebServer()

# Static IP:
# server = AsyncWebServer(
#     dhcp=False,
#     static_ip_config=("192.168.1.150", "255.255.255.0", "192.168.1.1", "8.8.8.8")
# )

# Run the server
# server.run()

if False:
    class MySimpleServer():
        def __init__(self, hub: HUBDevice, static_ip=None):
            self.lan = network.LAN()
            self.port = 5555
            self.ntp_synced = False
            self.running = False  # Add running flag to control server loop
            self.host_ip = None
            # self.can, self.hub = initialize_can_hub()
            self.hub = hub
            # self.hub.afe_devices_max = 1
            self.server_instance = None # For uasyncio server

            # self.hub.discovery_active = True
            # self.hub.rx_process_active = True
            # self.hub.use_tx_delay = True

            # self.hub.afe_manage_active = True
            self.static_ip = static_ip
            self.server_socket:socket.socket = None
            self.timestamp_ms = 0
            self.wait_ms = 100
            
            self.setup_lan_state = 0
            self._lan_conn_start_ms = 0 # Helper for LAN connection timeout
            self._lan_setup_in_progress = False

            self.ntp_sync_state = 0
            self.ntp_sync_timestamp_ms = 0
            self.ntp_synce_every_ms = 1*60*60*1000 # sync every 1 hour

        async def sync_ntp_machine(self):
            if not self.lan.isconnected():
                p.print("NTP sync: LAN not connected.")
                self.ntp_sync_state = 0
                await uasyncio.sleep_ms(1000) # Use async sleep
                return False

            if (millis() - self.ntp_sync_timestamp_ms) > self.ntp_synce_every_ms:
                self.ntp_sync_state = 0

            if self.ntp_sync_state == 0:
                p.print("Attempting to sync NTP...")
                try:
                    # Create a UDP socket
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.settimeout(5) # 5 second timeout

                    # Get server IP
                    addr = socket.getaddrinfo(NTP_HOST, 123)[0][-1]

                    # NTP request packet (minimal: version 3, client mode)
                    # LI = 0 (no warning), VN = 3 (version 3), Mode = 3 (client)
                    # (0 << 6) | (3 << 3) | 3 = 0 | 24 | 3 = 27 (0x1b)
                    msg = bytearray(48)
                    msg[0] = 0x1B # LI, Version, Mode

                    s.sendto(msg, addr)
                    await uasyncio.sleep_ms(0) # Yield
                    data, server_addr = s.recvfrom(48)
                    s.close()
                    await uasyncio.sleep_ms(0) # Yield

                    if data:
                        timestamp_ms = millis()
                        # Extract the transmit timestamp (bytes 40-43)
                        # This is the number of seconds since Jan 1, 1900
                        secs = struct.unpack("!I", data[40:44])[0]
                        
                        # Convert to Unix epoch (seconds since Jan 1, 1970)
                        unix_secs = secs - NTP_DELTA

                        # Get the time tuple (year, month, mday, hour, minute, second, weekday, yearday)
                        # MicroPython's time.gmtime() expects seconds since 2000-01-01.
                        # So, we need to adjust unix_secs.
                        # Seconds from 1970-01-01 to 2000-01-01 is 946684800
                        secs_since_2000 = unix_secs - 946684800
                        
                        tm = time.gmtime(secs_since_2000)

                        # Set RTC: (year, month, day, weekday, hours, minutes, seconds, subseconds)
                        # weekday: Mon=1, Sun=7. time.gmtime returns Mon=0, Sun=6.
                        rtc.datetime((tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))
                        self.ntp_sync_state = 1
                        # p.print("NTP sync successful. Time set to: {}".format(time.gmtime()))
                        current_time_tuple_for_log = time.gmtime() # Get current time after setting RTC
                        p.print("NTP sync successful. Time set to: {}".format(current_time_tuple_for_log))
                        rtc_synced = True
                        self.ntp_synced = True
                        self.ntp_sync_timestamp_ms = millis()
                        self.hub.logger.log(VerbosityLevel["CRITICAL"], {"device_id": 0, "timestamp_ms": timestamp_ms, "info": "NTP time synced", "time": current_time_tuple_for_log, "unix_timestamp": unix_secs})
                        # Rename logger file with the new timestamp
                        try:
                            year, month, day, hour, minute, second = current_time_tuple_for_log[0:6]
                            new_log_filename = "log_synced_{:04d}{:02d}{:02d}_{:02d}{:02d}{:02d}.json".format(
                                year, month, day, hour, minute, second
                            )
                            if not "log_synced_" in self.hub.logger.filename:
                                self.hub.logger.rename_current_file(new_log_filename)
                            # p.print("Logger file active name changed to: {}".format(self.hub.logger.filename))
                        except Exception as e_rename:
                            p.print("Failed to rename logger file: {}".format(e_rename))
                except Exception as e:
                    p.print("NTP sync failed: {}".format(e))
                    self.ntp_sync_state = 0
                    await uasyncio.sleep_ms(0) # Yield
                    return False
            elif self.ntp_sync_state == 1:
                # Check if time is reasonable (after Unix epoch + some buffer, e.g., year 2023)
                # time.time() in MicroPython is seconds since 2000-01-01
                # if time.time() > (23 * 365 * 24 * 60 * 60): # Check if time is after Jan 1, 2023
                #     self.ntp_synced = True
                #     self.ntp_sync_timestamp_ms = millis()
                #     return True
                await uasyncio.sleep_ms(1000) # Use async sleep
                return True
            return False

        async def setup_lan_machine(self):
            if self._lan_setup_in_progress and self.setup_lan_state != 1:
                # If setup was in progress but state changed (e.g. externally), reset flag
                self._lan_setup_in_progress = False

            try:
                if self.setup_lan_state == 0:
                    p.print("Configuring LAN...")
                    self.lan = network.LAN()
                    self.lan.active(True)
                    if self.static_ip:
                        self.lan.ifconfig((self.static_ip, '255.255.255.0', '192.168.1.1', '8.8.8.8'))
                    else:
                        self.lan.ifconfig('dhcp')
                    self._lan_conn_start_ms = millis()
                    self.setup_lan_state = 1
                    self._lan_setup_in_progress = True
                    p.print("LAN configuration initiated, attempting to connect...")
                    return False # Indicate setup is in progress

                elif self.setup_lan_state == 1:
                    if self.lan.isconnected():
                        self.setup_lan_state = 2
                        p.print("Connected to LAN: {}".format(self.lan.ifconfig()))
                        self._lan_setup_in_progress = False
                        return True
                    else:
                        if (millis() - self._lan_conn_start_ms) > 15000: # 15 seconds timeout
                            p.print("LAN connection attempt timed out.")
                            self.setup_lan_state = 0
                            self._lan_setup_in_progress = False
                            return False # Failed to connect in time
                        # Still trying, not yet an error, just not connected
                        # p.print("Waiting for LAN connection...") # Optional: can be verbose
                        await uasyncio.sleep_ms(500) # Yield and check later
                        return False # Indicate still trying

                elif self.setup_lan_state == 2:
                    if not self.lan.isconnected():
                        p.print("LAN disconnected, attempting to reconnect...")
                        self.setup_lan_state = 0 # Reconnect
                        self._lan_setup_in_progress = False
                        return False
                    return True # Still connected

                else: # Should not happen
                    p.print("Unknown LAN setup state: {}, resetting.".format(self.setup_lan_state))
                    self.setup_lan_state = 0
                    self._lan_setup_in_progress = False
                    return False

            except Exception as e:
                p.print("Error in LAN setup: {}".format(e))
                self.setup_lan_state = 0
                self._lan_setup_in_progress = False
            return False

        async def handle_client(self, reader, writer):
            address = writer.get_extra_info('peername')
            p.print("New connection from {}".format(address))
            # connection.settimeout(10.0) # Not directly applicable with uasyncio reader/writer
            try:
                data_bytes = await reader.read(1024)
                if not data_bytes:
                    writer.close()
                    await writer.wait_closed()
                    p.print("Connection closed by peer {address} (no data).".format(address))
                    return
                
                data = data_bytes.decode()
                timestamp_ms = millis()
                p.print("Received from {}: {}".format(address, data))
                toSend = [] # This variable seems unused for sending back to client in original logic
                response_sent = False
                try:
                    j = ujson.loads(data)
                    if "procedure" in j:
                        p.print("Procedure: {}".format(j["procedure"]))
                        afe_id = j.get("afe_id", None)
                        procedure = j["procedure"]

                        if procedure == "default_full":
                            if afe_id is None:
                                writer.close()
                                await writer.wait_closed()
                                return
                            afe = self.hub.get_afe_by_id(afe_id)
                            if afe is None:
                                writer.close()
                                await writer.wait_closed()
                                return
                            self.hub.default_full(afe_id)
                            j["status"] = "OK" # Prepare response
                            writer.write(ujson.dumps(j).encode())
                            await writer.drain()
                            response_sent = True

                        elif procedure == "default_get_measurement_last":
                            async def cb(msg=None):
                                p.print("EXECUTED CALLBACK ON SERVER: {}".format(msg))
                                my_dict = msg.copy()
                                if 'frame' in my_dict:
                                    my_dict.pop('frame')
                                if 'callback' in my_dict:
                                    my_dict.pop('callback')
                                try:
                                    writer.write(ujson.dumps(my_dict).encode())
                                    await writer.drain()
                                except Exception as e_cb_send:
                                    p.print("Error sending callback data: {}".format(e_cb_send))
                                finally:
                                    writer.close()
                                    await writer.wait_closed()
                            # This callback structure with uasyncio needs care.
                            # The hub method should ideally be async or the callback needs to be uasyncio-aware.
                            # For now, assuming hub.default_get_measurement_last handles its callback execution.
                            # The socket closure is now part of the callback.
                            self.hub.default_get_measurement_last(afe_id, callback=cb)
                            response_sent = True # Callback will handle sending and closing
                            return # Callback handles closure

                        elif procedure == "get_all_afe_configuration":
                            my_dict = {}
                            for afe_device in self.hub.afe_devices: # Renamed afe to afe_device to avoid conflict
                                my_dict[afe_device.device_id] = afe_device.configuration
                            p.print(ujson.dumps(my_dict))
                            writer.write(ujson.dumps(my_dict).encode())
                            await writer.drain()
                            response_sent = True
                        
                        else: # Fallback for unimplemented procedures
                            error_response = {"status": "ERROR", "status_info": "Procedure not implemented or NTP not synced for relevant command"}
                            if procedure == "get_unix_timestamp" and not self.ntp_synced:
                                error_response["status_info"] = "NTP not synced"
                            
                            writer.write(ujson.dumps(error_response).encode())
                            await writer.drain()
                            response_sent = True
                            # Original code sent two error messages, simplifying to one.
                            # j["status"] = "ERROR"
                            # j["status_info"] = "Not implemented"
                            # writer.write(ujson.dumps(j).encode())
                            # await writer.drain()

                    elif "command" in j: # Handling 'command' based requests
                        afe_id = j.get("afe_id") # Assuming afe_id is present for commands
                        if afe_id is None and j["command"] != "some_command_not_needing_afe_id": # Example
                            error_response = {"status": "ERROR", "status_info": "afe_id missing for command"}
                            writer.write(ujson.dumps(error_response).encode())
                            await writer.drain()
                            response_sent = True
                        else:
                            # Simplified: Acknowledge command, actual execution is via hub methods
                            # Specific hub method calls...
                            if j["command"] == "get_data":
                                self.hub.send_back_data(afe_id)
                            elif j["command"] == "default_procedure":                            
                                self.hub.default_procedure(afe_id)
                            # ... (other commands) ...
                            elif j["command"] == "test1":
                                self.hub.test1(afe_id)
                            
                            if not response_sent: # Send a generic OK if no specific response was crafted
                                ok_response = {"status": "OK", "command_received": j["command"]}
                                writer.write(ujson.dumps(ok_response).encode())
                                await writer.drain()
                                response_sent = True
                    
                    else: # No procedure or command
                        if not response_sent:
                            error_response = {"status": "ERROR", "status_info": "No procedure or command specified"}
                            writer.write(ujson.dumps(error_response).encode())
                            await writer.drain()
                            response_sent = True

                except ujson.JSONDecodeError as e_json:
                    p.print("JSON decode error: {}".format(e_json))
                    if not response_sent: # Ensure a response is sent even on JSON error
                        error_response = {"status": "ERROR", "status_info": "Invalid JSON: {}".format(e_json)}
                        writer.write(ujson.dumps(error_response).encode())
                        await writer.drain()
                except Exception as e_proc:
                    p.print("Error processing client request: {}".format(e_proc))
                    if not response_sent: # Ensure a response is sent on other errors
                        error_response = {"status": "ERROR", "status_info": "Server error: {}".format(e_proc)}
                        writer.write(ujson.dumps(error_response).encode())
                        await writer.drain()

            # except OSError as e: # OSError might be caught by uasyncio stream
            #     if e.args[0] == 110:  # ETIMEDOUT - less likely with await reader.read()
            #         p.print("Connection timed out from {}".format(address))
            #     else:
            #         p.print("OSError handling client from {}: {}".format(address, e))
            except Exception as e:
                p.print("Generic error handling client from {}: {}".format(address, e))
            finally:
                if not writer.is_closing():
                    writer.close()
                    await writer.wait_closed()
                p.print("Connection closed from {}".format(address))

        async def main_loop(self):
            p.print("Server main_loop starting...")
            lan_ready = False
            while self.running and not lan_ready:
                lan_ready = await self.setup_lan_machine()
                if not lan_ready:
                    p.print("LAN not ready, retrying in 5s...")
                    await uasyncio.sleep_ms(5000)
                elif self._lan_setup_in_progress: # Still in setup_lan_state == 1 but not timed out
                    lan_ready = False # Force loop to continue until explicitly True or hard False
                    await uasyncio.sleep_ms(500) # Short sleep while setup_lan_machine progresses

            if not lan_ready:
                p.print("Failed to initialize LAN. Server cannot start.")
                self.running = False
                return

            p.print("Attempting to start server on {}:{}".format(self.lan.ifconfig()[0],self.port))
            try: # Start the uasyncio server
                self.server_instance = await uasyncio.start_server(
                    self.handle_client, self.lan.ifconfig()[0], int(self.port)
                )
                p.print("Server started successfully.")
            except Exception as e:
                p.print("Failed to start server: {}".format(e))
                self.running = False
                return

            while self.running:
                # Server runs in the background via uasyncio.start_server
                # This loop can do other periodic tasks or just sleep
                if not self.lan.isconnected(): # Check for LAN disconnection
                    p.print("LAN disconnected. Attempting to re-establish...")
                    self.setup_lan_state = 0 # Trigger re-setup logic
                    lan_ready = False
                    while self.running and not lan_ready:
                        lan_ready = await self.setup_lan_machine()
                        if not lan_ready:
                            await uasyncio.sleep_ms(5000)
                        elif self._lan_setup_in_progress:
                            lan_ready = False
                            await uasyncio.sleep_ms(500)
                    
                    if not lan_ready:
                        p.print("Could not re-establish LAN. Stopping server tasks.")
                        self.running = False # Stop if LAN cannot be re-established
                        break 
                    else:
                        p.print("LAN re-established.")
                
                await uasyncio.sleep_ms(1000) # Sleep to yield control and prevent tight loop

            if self.server_instance:
                self.server_instance.close()
                await self.server_instance.wait_closed()
            p.print("Server main_loop ended.")

        async def sync_ntp_loop(self,_=None): # Parameter _ is unused
            p.print("NTP sync_loop starting...") # Start NTP sync loop
            while self.running:
                await self.sync_ntp_machine()
                await uasyncio.sleep_ms(1000) # Check/sync periodically, e.g., every second for state changes, actual sync less often
            p.print("NTP sync_loop ended.")

        # def start_server(self): # This method is not used in the uasyncio main.py structure
        #     # self.lan = self.setup_lan() # setup_lan is not async, setup_lan_machine is
        #     self.running = True
        #     # self.run() # Original run method
        #     # self.run_thread = _thread.start_new_thread(self.run, ()) # Replaced by uasyncio tasks
        #     pass

        # def x(self): # Unused method
        #     while True: # Unused method
        #         pyb.delay(100)
        #         # p.print("TEST")

    # # Remove old threading-based methods if they are fully replaced
    # # For example, setup_socket and the original main_machine are implicitly handled by uasyncio.start_server
    #             elif self.setup_lan_state == 2:
    #                 if not self.lan.isconnected():
    #                     self.setup_lan_state = 0 # Recconect
    #                     return False
    #                 return True
    #             else:
    #                 self.setup_lan_state = 0
    #         except:
    #             self.setup_lan_state = 0
    #         return False

        def handle_client(self, connection: socket.socket, address: tuple):
            p.print("New connection from {}".format(address))
            connection.settimeout(10.0)
            try:
                data = connection.recv(1024).decode()
                if not data:
                    return
                timestamp_ms = millis()
                p.print("Received: {}".format(data))
                toSend = []
                try:
                    j = ujson.loads(data)
                    # afe_id = j["afe_id"]
                    # afe_id = 35
                    if "procedure" in j:
                        p.print("Procedure: {}".format(j["procedure"]))
                        # afe_id = int(j["afe_id"])
                        # afe = self.hub.get_afe_by_id(afe_id)

                        afe_id = j.get("afe_id",None)
                        procedure = j["procedure"]
                        if procedure == "default_full":
                            if afe_id is None:
                                return
                            afe = self.hub.get_afe_by_id(afe_id)
                            if afe is None:
                                return
                            self.hub.default_full(afe_id)
                            # j["status"] = "OK"
                            toSend.append(j)
                            # connection.sendall(ujson.dumps(j).encode())
                        elif procedure == "default_get_measurement_last":
                            # print("Before X")
                            # x = True
                            def cb(msg=None):
                                print("EXECUTED CALLBACK ON SERVER: {}".format(msg))
                                my_dict = msg.copy()
                                if 'frame' in my_dict:
                                    my_dict.pop('frame')
                                if 'callback' in my_dict:
                                    my_dict.pop('callback')
                                try:
                                    connection.sendall(ujson.dumps(my_dict).encode())
                                    connection.close()
                                except:
                                    pass
                                # toSend.append(my_dict)
                                # print("Appended: ", my_dict)
                                # x = False
                                # return my_dict
                            self.hub.default_get_measurement_last(afe_id,callback=cb)
                            # while x:
                            #     if (millis()-timestamp_ms > 5000):
                            #         return None
                            # print("After X")
                            # toSend_dict = {}
                            # print(toSend)
                            # for d in toSend:
                            #     for k,v in d.items():
                            #         toSend_dict.update(k)
                            # print(ujson.dumps(toSend_dict))
                            # connection.sendall(toSe)
                            # j["status"] = "OK"
                        elif procedure == "get_all_afe_configuration":
                            my_dict = {}
                            for afe in self.hub.afe_devices:
                                my_dict[afe.device_id] = afe.configuration
                            print(ujson.dumps(my_dict))
                            connection.sendall(ujson.dumps(my_dict).encode())
                        # Check for 'get_unix_timestamp' command
                        #  elif procedure == "get_unix_timestamp":
                        #     if self.ntp_synced:
                        #         connection.sendall(ujson.dumps({"unix_timestamp": time.time()}).encode())
                        #     else:
                                
                        else:
                            connection.sendall(ujson.dumps({"status": "ERROR", "status_info": "NTP not synced"}).encode())
                            j["status"] = "ERROR"
                            j["status_info"] = "Not implemented"
                            connection.sendall(ujson.dumps(j).encode())
                            return
                            
                    if "command" in j:
                        if j["command"] == "get_data":
                            self.hub.send_back_data(afe_id)
                        elif j["command"] == "default_procedure":                            
                            self.hub.default_procedure(afe_id)
                        elif j["command"] == "default_set_dac":
                            self.hub.default_set_dac(afe_id)
                        elif j["command"] == "default_get_measurement":
                            self.hub.default_get_measurement(afe_id)
                        elif j["command"] == "default_hv_set":
                            self.hub.default_hv_set(afe_id,True)
                        elif j["command"] == "default_hv_off":
                            self.hub.default_hv_set(afe_id,False)
                        elif j["command"] == "test4":
                            self.hub.test4(afe_id)
                        elif j["command"] == "test3":
                            self.hub.test3(afe_id)
                        elif j["command"] == "test2":
                            self.hub.test2(afe_id)
                        elif j["command"] == "test1":
                            self.hub.test1(afe_id)
                except Exception as e:
                    p.print("Error: {}".format(e))
                # connection.sendall(ujson.dumps({"status":"OK"}).encode())
            except OSError as e:
                if e.args[0] == 110:  # ETIMEDOUT
                    p.print("Connection timed out from {}".format(address))
                else:
                    p.print("Error handling client from {}: {}".format(address, e))
            except Exception as e:
                p.print("Error handling client from {}: {}".format(address, e))
            # finally: # Avoid deadlock
            #     connection.close()
            #     p.print("Connection closed from {}".format(address))
        
        def setup_socket(self):
            try:
                try:
                    self.server_socket.close()
                except:
                    pass
                self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                # self.server_socket.settimeout(2.0)
                self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.server_socket.bind((self.lan.ifconfig()[0], int(self.port)))
                self.server_socket.listen(1)
            except Exception as e:
                self.server_socket = None
                p.print("Error setting up server socket: {}".format(e))
        # def sync_loop(self, _=None):
            
        def main_machine(self, _=None):
            # if (millis() - self.timestamp_ms) < self.wait_ms:
            #     return
            # self.timestamp_ms = millis()
            # print("MAIN MACHINE SERVER")
            # p.print(self.lan.ifconfig())
            # print(self.setup_lan_machine())
            # return
            if self.setup_lan_machine(): # If True, then lan is set
                # self.sync_ntp_machine()
                if self.server_socket is None:
                    self.setup_socket()
                # try:
                # self.server_socket.settimeout(0.01)
                connection, address = self.server_socket.accept()
                # _thread.start_new_thread(self.handle_client, (connection, address,))
                # with connection:
                self.handle_client(connection,address)
                # self.connecions.append({"connection":connection,"address":address})
                # except:
                #     pass
                # finally:
                #     try:
                #         connection.close()
                #     except:
                #         pass
        
        # def main_machine_scheduled(self,_):
        #     self.main_machine()      
            
        # def sync_ntp_loop(self,_=None):
        #     while self.running:
        #         self.sync_ntp_machine()
        #         time.sleep_ms(1)
        
        # def main_loop(self):
        #     while self.running:
        #         # micropython.schedule(self.main_machine_scheduled, 0)
        #         self.main_machine()
        #         # print("SERVER")
        #         # time.sleep_us(10)
        #         time.sleep_ms(1)
        #         # time.sleep_ms(100)
        #         # time.sleep(0.01)
        #         # wdt.feed()
                    
        # def start_server(self):
        #     self.lan = self.setup_lan()
        #     self.running = True
        #     # self.run()
        #     self.run_thread = _thread.start_new_thread(self.run, ())
            
        # def x(self):
        #     while True:
        #         pyb.delay(100)
        #         # p.print("TEST")
