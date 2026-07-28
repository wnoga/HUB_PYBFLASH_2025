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
from my_utilities import dump_json_sorted

import uasyncio as asyncio

# NTP constants
NTP_DELTA = 2208988800  # Seconds between NTP epoch (1900) and Unix epoch (1970)
NTP_HOST = "pool.ntp.org"

class PendingProcedure:
    __slots__ = (
        "event",
        "result",
        "afe_id",
        "procedure",
        "created_ms",
    )
    
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
        
        self.pending_procedures = {}   # request_id -> PendingProcedure
        self.max_pending_procedures = 32
        self.next_request_id = 1
        
        self.RESPONSE_OK = b'{"status":"OK"}'

        self.RESPONSE_SERVER_BUSY = (
            b'{"status":"ERROR","info":"Server busy"}'
        )

        self.RESPONSE_INVALID_JSON = (
            b'{"status":"ERROR","info":"Invalid request format"}'
        )

        self.RESPONSE_UNKNOWN_PROCEDURE = (
            b'{"status":"ERROR","info":"Unknown procedure"}'
        )
        
        self.procedure_map = {
            "default_get_measurement_last":
                ("default_get_measurement_last", 20.0, None),

            "afe_configure":
                ("default_configure_afe", 60.0, None),

            "afe_reset":
                ("reset", 10.0, None),

            "afe_temperature_loop_start":
                ("start_afe_temperature_loop", 10.0,
                    {"afe_subdevice": int}),

            "afe_temperature_loop_stop":
                ("stop_afe_temperature_loop", 10.0,
                    {"afe_subdevice": int}),

            "afe_set_dac":
                ("default_set_dac", 10.0,
                    {"dac_master": int, "dac_slave": int}),

            "start_periodic_measurement_by_config":
                ("start_periodic_measurement_by_config", 10.0, None),

            "stop_periodic_measurement_download":
                ("stop_afe_periodic_measurement_download", 10.0, None),

            "afe_set_offset":
                ("set_afe_offset", 10.0,
                    {"offset_master": int, "offset_slave": int}),

            "afe_set_averaging_mode":
                ("set_afe_averaging_mode", 10.0,
                    {"channel_mask": int, "mode": int}),

            "afe_set_averaging_alpha":
                ("set_afe_averaging_alpha", 10.0,
                    {"channel_mask": int, "alpha": float}),

            "afe_get_subdevice_status":
                ("get_subdevice_status", 15.0,
                    {"subdevice_mask": int}),

            "afe_set_channel_dt_ms":
                ("set_afe_channel_dt_ms", 10.0,
                    {"channel_mask": int, "dt_ms": int}),

            "afe_set_sipm_voltage_si":
                ("afe_set_sipm_voltage_si", 10.0,
                    {"voltage": float, "afe_subdevice": int}),
        }
        
        
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

        await p.print("Waiting for Ethernet connection...")
        timeout = 10
        while not self.lan.isconnected() and timeout > 0:
            await uasyncio.sleep(1) # Use asynchronous sleep
            timeout -= 1

        if not self.lan.isconnected():
            raise RuntimeError("Ethernet connection failed")

        await p.print("Ethernet connected. IP:", self.lan.ifconfig()[0]) # Added await
        self.lan_connected = True
        
    async def hub_cb(self, msg, callback_data):
        request_id = callback_data

        pending = self.pending_procedures.get(request_id)
        if pending is None:
            return

        pending["result"] = ujson.dumps(msg).encode()
        pending["event"].set()

    async def _execute_afe_procedure(
        self,
        request_json,
        hub_method_name,
        timeout_duration,
        required_params_map=None
    ):
        afe_id = request_json.get("afe_id")
        if afe_id is None:
            return ujson.dumps({
                "status": "ERROR",
                "info": "afe_id missing"
            }).encode()

        # Reject if server is overloaded
        if len(self.pending_procedures) >= self.max_pending_procedures:
            return ujson.dumps({
                "status": "ERROR",
                "info": "Server busy"
            }).encode()

        # Parse parameters
        hub_call_kwargs = {}

        if required_params_map:
            for name, converter in required_params_map.items():
                value = request_json.get(name)

                if value is None:
                    return ujson.dumps({
                        "status": "ERROR",
                        "info": "Parameter '{}' missing".format(name)
                    }).encode()

                try:
                    hub_call_kwargs[name] = converter(value)
                except Exception:
                    return ujson.dumps({
                        "status": "ERROR",
                        "info": "Invalid value for '{}'".format(name)
                    }).encode()

        # Get hub method
        try:
            hub_method = getattr(self.hub, hub_method_name)
        except AttributeError:
            return ujson.dumps({
                "status": "ERROR",
                "info": "Unknown HUB method '{}'".format(hub_method_name)
            }).encode()

        # Allocate request id
        request_id = self.next_request_id
        self.next_request_id += 1

        if self.next_request_id > 0x7FFFFFFF:
            self.next_request_id = 1

        event = uasyncio.Event()

        self.pending_procedures[request_id] = {
            "event": event,
            "result": None,
            "afe_id": afe_id,
            "procedure": hub_method_name,
        }

        async def callback(msg):
            await self.hub_cb(request_id, msg)

        hub_call_kwargs["callback"] = callback

        try:

            await hub_method(
                afe_id=afe_id,
                **hub_call_kwargs
            )

            await uasyncio.wait_for(
                event.wait(),
                timeout_duration
            )

            result = self.pending_procedures[request_id]["result"]

            if result is None:
                return ujson.dumps({
                    "status": "OK",
                    "info": "{} completed".format(hub_method_name)
                }).encode()

            return result

        except uasyncio.TimeoutError:

            await p.print(
                "Timeout waiting for '{}' request {}".format(
                    hub_method_name,
                    request_id
                )
            )

            return ujson.dumps({
                "status": "ERROR",
                "info": "Timeout waiting for response"
            }).encode()

        except Exception as e:

            await p.print(
                "Procedure '{}' failed: {}".format(
                    hub_method_name,
                    e
                )
            )

            return ujson.dumps({
                "status": "ERROR",
                "info": str(e)
            }).encode()

        finally:
            self.pending_procedures.pop(request_id, None)
       
    async def handle_procedure(self, request_line):

        if len(self.pending_procedures) >= self.max_pending_procedures:
            return self.RESPONSE_SERVER_BUSY

        try:
            request_json = ujson.loads(request_line)
        except (ValueError, UnicodeError):
            return self.RESPONSE_INVALID_JSON

        procedure = request_json.get("procedure")

        if procedure is None:
            return self.RESPONSE_UNKNOWN_PROCEDURE

        #
        # Immediate (non-blocking) procedures
        #

        if procedure == "get_all_afe_configuration":

            cfg = {}

            for afe in self.hub.afe_devices:
                cfg[afe.device_id] = afe.configuration

            return ujson.dumps(cfg).encode()

        if procedure == "hub_close_all":

            await self.hub.close_all()

            return b'{"status":"OK"}'

        if procedure == "hub_powerOn":

            await self.hub.powerOn()

            return b'{"status":"OK"}'

        if procedure == "hub_powerOff":

            await self.hub.powerOff()

            return b'{"status":"OK"}'

        if procedure == "hub_clear_old_logs":

            await self.hub.clear_old_logs()

            return b'{"status":"OK"}'

        if procedure == "default_procedure":

            afe_id = request_json.get("afe_id")

            if afe_id is None:
                return ujson.dumps({
                    "status":"ERROR",
                    "info":"afe_id missing"
                }).encode()

            await self.hub.default_procedure(afe_id)

            return b'{"status":"OK"}'

        #
        # Async procedures
        #

        entry = self.procedure_map.get(procedure)

        if entry is None:

            await p.print("Unknown procedure:", procedure)

            return ujson.dumps({
                "status":"ERROR",
                "info":"Unknown procedure"
            }).encode()

        hub_method, timeout, params = entry

        return await self._execute_afe_procedure(
            request_json=request_json,
            hub_method_name=hub_method,
            timeout_duration=timeout,
            required_params_map=params
        )
        
    # async def handle_client(self, reader, writer):
    #     try:
    #         request_line = await uasyncio.wait_for(reader.readline(), 5)
    #         await p.print("Request: ", request_line)
    #         if not request_line:
    #             return

    #         # HTTP GET -> web page
    #         if request_line.startswith(b"GET"):
    #             while True:
    #                 header = await reader.readline()
    #                 if header == b"\r\n" or not header:
    #                     break

    #             await self.send_control_web_page(writer)
    #             return

    #         # HTTP POST -> ignore or implement later
    #         if request_line.startswith(b"POST"):
    #             ...
    #             return

    #         # Otherwise assume JSON procedure
    #         response = await self.handle_procedure(request_line)
    #         await writer.awrite(response)

    #     finally:
    #         await writer.aclose()
        
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
        # await self.handle_procedure("afe_get_subdevice_status") # Update status every page refresh
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
                    .collapsible-content { display: none; }
                </style>
                <script>
                    function toggleCollapse(elementId) {
                        var content = document.getElementById(elementId);
                        if (content.style.display === "block") {
                            content.style.display = "none";
                        } else {
                            content.style.display = "block";
                        }
                    }
                    function updatePage() { window.location.reload(); }
                </script>
            </head>""")
        await writer.awrite("""
            <body>
                <h1>AFE HUB Control</h1>
                <p>Current Time: <span id="current-time">{}</span> @ {}</p>
                <button onclick="updatePage()">Refresh</button>
            """.format(rtc_datetime_pretty(),millis()))
        
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

                await writer.awrite("""
                <h4>Status:</h4>
                <pre>Master: {}</pre>
                <pre>Slave: {}</pre>
                """.format(
                    dump_json_sorted(afe.debug_machine_control_msg_last[0]),
                    dump_json_sorted(afe.debug_machine_control_msg_last[1])
                ))
                await writer.awrite("""
                <button onclick="toggleCollapse('channels-afe-{}')">Toggle Channels</button>
                <div id="channels-afe-{}" class="afe-channels collapsible-content">
                """.format(afe.device_id, afe.device_id))
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
                    </div></div>
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
            await p.print("Request: ", request_line)
            response = await self.handle_procedure(request_line)
            if response:
                await p.print("Response...")
                await writer.awrite(response)
            else:
                await p.print("Should be web page...")
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

    async def sync_ntp_loop(self):
        """Periodically syncs RTC with NTP."""
        while True:
            isSynced = await self.sync_rtc_with_ntp()
            if isSynced:
                await asyncio.sleep(self.sync_ntp_every_s)
            else:
                await asyncio.sleep(self.sync_ntp_loop_yield_wait_s)
                
    async def _ensure_ethernet(self):
        if self.lan_connected:
            return True

        await p.print("Attempting to reconnect Ethernet...")

        try:
            await self.connect_ethernet()
            return True

        except Exception as e:
            await p.print("Ethernet reconnect failed:", e)
            return False

    async def _ensure_server(self):
        if self.server is not None:
            return True

        await p.print("Starting HTTP server...")

        try:
            self.server = await asyncio.start_server(
                self.handle_client,
                "0.0.0.0",
                self.port
            )

            await p.print(
                "Server running at http://{}:{}".format(
                    self.lan.ifconfig()[0],
                    self.port
                )
            )

            return True

        except Exception as e:
            await p.print("Server start failed:", e)
            self.server = None
            return False
        
    async def _check_connection(self):
        if not is_timeout(self.last_lan_check_ms, 5000):
            return

        self.last_lan_check_ms = millis()

        if self.lan.isconnected():
            return

        await p.print("Ethernet disconnected")

        self.lan_connected = False

        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
            
    async def start(self):

        while True:

            try:
                wdt.feed()

                if not await self._ensure_ethernet():
                    await asyncio.sleep(10)
                    continue

                if not await self._ensure_server():
                    await asyncio.sleep(10)
                    continue

                await self._check_connection()

            except MemoryError:
                gc.collect()
                await p.print(
                    "MemoryError:",
                    gc.mem_free(),
                    gc.mem_alloc()
                )

            except Exception as e:
                await p.print("AsyncWebServer:", e)

            await asyncio.sleep_ms(self.main_loop_yield_wait_ms)
    