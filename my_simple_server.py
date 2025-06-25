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
from my_utilities import AFECommand, AFECommandSubdevice

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
        self.sync_ntp_every_s = 60
        
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

    async def _execute_afe_procedure(self, request_json, hub_method_name, timeout_duration, required_params_map=None):
        """
        Helper to execute AFE-specific procedures that require an afe_id 
        and wait for a callback via an event.
        """
        afe_id = request_json.get("afe_id", None)
        if afe_id is None:
            return ujson.dumps({"status": "ERROR", "info": "afe_id missing"}).encode()

        hub_call_kwargs = {}  # For named arguments to the hub method, excluding afe_id and callback
        
        if required_params_map:
            for param_name, converter_func in required_params_map.items():
                param_value_str = request_json.get(param_name, None)
                if param_value_str is None:
                    return ujson.dumps({"status": "ERROR", "info": "Parameter '{}' missing".format(param_name)}).encode()
                try:
                    hub_call_kwargs[param_name] = converter_func(param_value_str)
                except ValueError:
                    return ujson.dumps({"status": "ERROR", "info": "Invalid value for parameter '{}'".format(param_name)}).encode()
        if not "callback" in hub_call_kwargs:
            hub_call_kwargs["callback"] = self.hub_cb

        # Use afe_id as the key for managing events and results
        if afe_id not in self.procedure_events and len(self.procedure_events) >= self.max_procedures_keep_len:
            await p.print("AsyncWebServer: Max concurrent procedures reached. Rejecting new '{}' for AFE {}.".format(hub_method_name, afe_id))
            return ujson.dumps({"status": "ERROR", "info": "Server busy, max concurrent procedures reached"}).encode()
        
        event = uasyncio.Event()
        self.procedure_events[afe_id] = event
        self.procedure_results.pop(afe_id, None)  # Clear previous result

        try:
            hub_method_ref = getattr(self.hub, hub_method_name)
        except AttributeError:
            await p.print("Error: HUB method {} not found.".format(hub_method_name))
            self.procedure_events.pop(afe_id, None) # Clean up event
            return ujson.dumps({"status": "ERROR", "info": "Internal server error: procedure {} not found".format(hub_method_name)}).encode()

        async def _execute_and_wait_for_event():
            # This coroutine will be managed by uasyncio.wait_for
            await hub_method_ref(afe_id=afe_id, **hub_call_kwargs) # Call the hub method
            await event.wait() # Wait for the event set by the callback

        try:
            await uasyncio.wait_for(_execute_and_wait_for_event(), timeout=timeout_duration)
            # If we reach here, the event was set within the timeout
            result_data = self.procedure_results.pop(afe_id, None)
            if self.procedure_events.get(afe_id) == event: # Clean up event
                self.procedure_events.pop(afe_id, None)
            
            if result_data is None: # Event set, but no data (e.g., simple ack or error handled by callback setting no data)
                return ujson.dumps({"status": "OK", "info": "Procedure '{}' completed.".format(hub_method_name)}).encode()
            return result_data # This is already encoded by hub_cb
        except uasyncio.TimeoutError:
            await p.print("Timeout during execution of or waiting for result from '{}' for AFE {}.".format(hub_method_name, afe_id))
            self.procedure_events.pop(afe_id, None) # Clean up event
            self.procedure_results.pop(afe_id, None) # Clean up any partial result
            return ujson.dumps({"status": "ERROR", "info": "Timeout waiting for AFE response for '{}'".format(hub_method_name)}).encode()
        except Exception as e: # Catches errors from hub_method_ref or event.wait()
            await p.print("Error during execution of '{}' or waiting for event for AFE {}: {}".format(hub_method_name, afe_id, e))
            self.procedure_events.pop(afe_id, None) # Clean up event
            self.procedure_results.pop(afe_id, None) # Clean up any partial result
            return ujson.dumps({"status": "ERROR", "info": "Server error during '{}' procedure execution".format(hub_method_name)}).encode()

    async def handle_procedure(self, request_line):
        request_json = None
        if request_line[:3] == b'GET':
            return None # It's HTTP GET
        if request_line[:4] == b'POST':
            return None # It's HTTP POST
        try:
            decoded_line = request_line.decode()
            request_json = ujson.loads(decoded_line)
        except (ValueError, UnicodeError) as e:
            await p.print("Failed to decode or parse request line as JSON: {} - Line: {}".format(e, request_line))
            return ujson.dumps({"status": "ERROR", "info": "Invalid request format"}).encode()
        
        if not request_json:
             return ujson.dumps({"status": "ERROR", "info": "Empty request"}).encode()

        procedure = request_json.get("procedure", None)
        if not procedure:
            return ujson.dumps({"status": "ERROR", "info": "Procedure not specified"}).encode()

        # Procedures not using the common helper
        if procedure == "get_all_afe_configuration":
            my_dict = {}
            for afe_device in self.hub.afe_devices: # Renamed afe to afe_device to avoid conflict
                my_dict[afe_device.device_id] = afe_device.configuration
            return ujson.dumps(my_dict).encode()
        elif procedure == "hub_close_all":
            await self.hub.close_all()
            return ujson.dumps({"status": "OK"}).encode()
        
        elif procedure == "default_procedure":
            afe_id = request_json.get("afe_id",None)
            if not afe_id: return ujson.dumps({"status": "ERROR", "info": "afe_id missing for default_procedure"}).encode()
            await self.hub.default_procedure(afe_id)
            return ujson.dumps({"status": "OK", "info": "default_procedure initiated"}).encode()
        
        elif procedure == "hub_powerOn":
            await self.hub.powerOn()
            return ujson.dumps({"status": "OK"}).encode()

        elif procedure == "hub_powerOff":
            await self.hub.powerOff()
            return ujson.dumps({"status": "OK"}).encode()

        # Procedures using the _execute_afe_procedure helper
        elif procedure == "default_get_measurement_last":
            return await self._execute_afe_procedure(request_json, "default_get_measurement_last", 20.0)

        elif procedure == "afe_configure":
            # Assumes HUB.py's default_configure_afe is adapted or a similar method 
            # exists that uses the callback for completion signaling.
            return await self._execute_afe_procedure(request_json, "default_configure_afe", 60.0)

        elif procedure == "afe_reset":
            # Assumes HUB.py's `reset` method is adapted to accept and use a callback.
            return await self._execute_afe_procedure(request_json, "reset", 10.0)

        elif procedure == "afe_temperature_loop_start":
            return await self._execute_afe_procedure(request_json, "start_afe_temperature_loop", 10.0,
                                                     required_params_map={"afe_subdevice": int})

        elif procedure == "afe_temperature_loop_stop":
            return await self._execute_afe_procedure(request_json, "stop_afe_temperature_loop", 10.0,
                                                     required_params_map={"afe_subdevice": int})

        elif procedure == "afe_set_dac":            
            return await self._execute_afe_procedure(request_json, "default_set_dac", 10.0,
                                                     required_params_map={"dac_master": int, "dac_slave": int})
        elif procedure == "start_periodic_measurement_by_config":
            return await self._execute_afe_procedure(request_json, "start_periodic_measurement_by_config", 10.0)
            
        elif procedure == "stop_periodic_measurement_download":
            return await self._execute_afe_procedure(request_json, "stop_afe_periodic_measurement_download", 10.0)

        elif procedure == "afe_set_offset":
            return await self._execute_afe_procedure(request_json, "set_afe_offset", 10.0,
                                                     required_params_map={"offset_master": int, "offset_slave": int})

        elif procedure == "afe_set_averaging_mode":
            return await self._execute_afe_procedure(request_json, "set_afe_averaging_mode", 10.0,
                                                     required_params_map={"channel_mask": int, "mode": int})

        elif procedure == "afe_set_averaging_alpha":
            return await self._execute_afe_procedure(request_json, "set_afe_averaging_alpha", 10.0,
                                                     required_params_map={"channel_mask": int, "alpha": float})

        elif procedure == "afe_set_channel_dt_ms":
            return await self._execute_afe_procedure(request_json, "set_afe_channel_dt_ms", 10.0,
                                                     required_params_map={"channel_mask": int, "dt_ms": int})

        else:
            await p.print("Unknown procedure: {}".format(procedure))
            return ujson.dumps({"status": "ERROR", "info": "Unknown procedure: {}".format(procedure)}).encode()

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
        
        await writer.awrite(b"<div><h4>Log Files in /sd/logs/:</h4><ul style=\"column-width: 25ex;\">")
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
                wdt.feed()
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
    