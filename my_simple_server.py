import network
import socket
import uselect
import time
import uasyncio as asyncio
import ujson
import struct
import uos
import gc

from HUB import HUBDevice
from my_utilities import wdt, millis, is_timeout
from my_utilities import p, rtc, rtc_synced, rtc_datetime_pretty, dump_json_sorted

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
        self.server_sock = None
        self.lan = network.LAN()
        self.lan_connected = False
        self.last_lan_check_ms = 0
        self.ntp_synced = False
        
        self.main_loop_yield_wait_ms = 10
        self.sync_ntp_loop_yield_wait_s = 10
        self.sync_ntp_every_s = 60
        
        # Async procedure callback management
        self.procedure_results = {} # Stores results from hub callbacks
        self.procedure_events = {}  # Stores asyncio.Event for synchronization
        self.max_procedures_keep_len = 32
        
        # Raw Socket Polling & Zero-Allocation Configuration
        self.poller = uselect.poll()
        self.BUFFER_SIZE = 512  # Fixed buffer per connection
        self.client_sockets = {}  # {fileno: [socket, start_time, bytearray_buf, write_idx]}
        self.sock_map = {}        # { id(sock): sock }
        self.tcp_requests_max = 10
        self.CLIENT_TIMEOUT_S = 5

        self.RESPONSE_SERVER_BUSY = b'{"status":"ERROR","info":"Server busy"}\r\n'

    # =========================================================================
    # LOW-RAM RAW SOCKET HELPERS
    # =========================================================================

    def _send_raw(self, sock, data: bytes):
        """Helper to reliably send bytes over a non-blocking raw socket."""
        total_sent = 0
        while total_sent < len(data):
            try:
                sent = sock.send(data[total_sent:])
                if sent == 0 or sent is None:
                    raise OSError("Socket closed by peer")
                total_sent += sent
            except OSError as e:
                # EAGAIN / EWOULDBLOCK: wait briefly if send buffer is full
                if e.args[0] in (11, 110):  
                    time.sleep_ms(10)
                    continue
                raise e

    def _send_json_dict_chunked(self, sock, data: dict):
        """Serializes dictionary directly to socket without holding whole JSON string in RAM."""
        self._send_raw(sock, b"{")
        first = True
        for key, value in data.items():
            if not first:
                self._send_raw(sock, b",")
            first = False
            self._send_raw(sock, ujson.dumps(key).encode())
            self._send_raw(sock, b":")
            self._send_raw(sock, ujson.dumps(value).encode())
        self._send_raw(sock, b"}")

    def _close_client(self, client_sock):
        """Unregisters socket from poller and safely closes it."""
        fn = client_sock.fileno()
        if fn in self.client_sockets:
            del self.client_sockets[fn]
        try:
            self.poller.unregister(client_sock)
        except Exception:
            pass
        try:
            client_sock.close()
        except Exception:
            pass

    # =========================================================================
    # NETWORK & ETHERNET SETUP
    # =========================================================================

    def get_webpage_address(self):
        if self.lan_connected:
            return "http://{}:{}".format(self.lan.ifconfig()[0], self.port)
        return None
    
    async def print_webpage_address(self):
        await p.print(self.get_webpage_address())

    async def connect_ethernet(self):
        self.lan.active(True)        
        if not self.dhcp and self.static_ip_config:            
            ip, subnet, gateway, dns = self.static_ip_config
            self.lan.ifconfig((ip, subnet, gateway, dns))

        await p.print("Waiting for Ethernet connection...")
        timeout = 10
        while not self.lan.isconnected() and timeout > 0:
            await asyncio.sleep(1)
            timeout -= 1

        if not self.lan.isconnected():
            raise RuntimeError("Ethernet connection failed")

        await p.print("Ethernet connected. IP:", self.lan.ifconfig()[0])
        self.lan_connected = True

    # =========================================================================
    # HUB CALLBACK & ASYNC PROCEDURE HANDLERS
    # =========================================================================

    async def hub_cb(self, msg: dict = None):
        if not msg or "device_id" not in msg:
            await p.print("hub_cb: device_id missing in callback message.")
            return

        device_id = msg["device_id"]
        my_dict = {k: v for k, v in msg.items() if k not in ('frame', 'callback')}

        if device_id not in self.procedure_results and len(self.procedure_results) >= self.max_procedures_keep_len:
            await p.print("AsyncWebServer: procedure_results cache full. Evicting an item.")
            try:
                self.procedure_results.pop(next(iter(self.procedure_results)))
            except (StopIteration, KeyError):
                pass

        try:
            self.procedure_results[device_id] = ujson.dumps(my_dict).encode()
        except Exception as e:
            await p.print("hub_cb: Error serializing result for AFE {}: {}".format(device_id, e))
            self.procedure_results[device_id] = ujson.dumps({"status": "ERROR", "info": "Failed to serialize response"}).encode()

        event = self.procedure_events.get(device_id)
        if event:
            event.set()

    async def _execute_afe_procedure(self, request_json, hub_method_name, timeout_duration, required_params_map=None):
        afe_id = request_json.get("afe_id", None)
        if afe_id is None:
            return ujson.dumps({"status": "ERROR", "info": "afe_id missing"}).encode()

        hub_call_kwargs = {"callback": self.hub_cb}
        
        if required_params_map:
            for param_name, converter_func in required_params_map.items():
                param_value_str = request_json.get(param_name, None)
                if param_value_str is None:
                    return ujson.dumps({"status": "ERROR", "info": "Parameter '{}' missing".format(param_name)}).encode()
                try:
                    hub_call_kwargs[param_name] = converter_func(param_value_str)
                except ValueError:
                    return ujson.dumps({"status": "ERROR", "info": "Invalid value for parameter '{}'".format(param_name)}).encode()

        if afe_id not in self.procedure_events and len(self.procedure_events) >= self.max_procedures_keep_len:
            return ujson.dumps({"status": "ERROR", "info": "Server busy, max concurrent procedures reached"}).encode()
        
        event = asyncio.Event()
        self.procedure_events[afe_id] = event
        self.procedure_results.pop(afe_id, None)

        try:
            hub_method_ref = getattr(self.hub, hub_method_name)
        except AttributeError:
            return ujson.dumps({"status": "ERROR", "info": "Procedure handle missing"}).encode()

        async def _execute_and_wait():
            await hub_method_ref(afe_id=afe_id, **hub_call_kwargs)
            await event.wait()

        try:
            await asyncio.wait_for(_execute_and_wait(), timeout=timeout_duration)
            result_data = self.procedure_results.pop(afe_id, None)
            return result_data if result_data else ujson.dumps({"status": "OK"}).encode()
        except asyncio.TimeoutError:
            await p.print("Timeout executing '{}' for AFE {}".format(hub_method_name, afe_id))
            return ujson.dumps({"status": "ERROR", "info": "Timeout waiting for AFE response"}).encode()
        except Exception as e:
            await p.print("Error executing '{}' for AFE {}: {}".format(hub_method_name, afe_id, e))
            return ujson.dumps({"status": "ERROR", "info": "Server execution error"}).encode()
        finally:
            self.procedure_events.pop(afe_id, None)
            self.procedure_results.pop(afe_id, None)

    # =========================================================================
    # HTTP & PROCEDURE DISPATCHERS
    # =========================================================================

    async def handle_procedure_raw(self, request_bytes, sock):
        """Processes request payload line and writes JSON responses directly to raw socket."""
        try:
            request_json = ujson.loads(request_bytes)
        except (ValueError, UnicodeError):
            self._send_raw(sock, b'{"status":"ERROR","info":"Invalid JSON"}\r\n')
            return

        procedure = request_json.get("procedure")
        if not procedure:
            self._send_raw(sock, b'{"status":"ERROR","info":"Procedure missing"}\r\n')
            return

        if procedure == "get_all_afe_configuration":
            self._send_raw(sock, b"{")
            first = True
            for afe_device in self.hub.afe_devices:
                if not first:
                    self._send_raw(sock, b",")
                first = False
                self._send_raw(sock, ('"' + str(afe_device.device_id) + '":').encode())
                self._send_raw(sock, ujson.dumps(afe_device.configuration).encode())
            self._send_raw(sock, b"}\r\n")

        elif procedure == "get_all_latest_status":
            self._send_raw(sock, b"{")
            first = True
            for afe_device in self.hub.afe_devices:
                if not first:
                    self._send_raw(sock, b",")
                first = False
                self._send_raw(sock, ('"' + str(afe_device.device_id) + '":').encode())
                self._send_json_dict_chunked(sock, afe_device.latest_status)
            self._send_raw(sock, b"}\r\n")

        elif procedure == "get_all_afe_id":
            ids = [str(afe.device_id) for afe in self.hub.afe_devices]
            res = '{"available_afe":[' + ','.join(ids) + ']}\r\n'
            self._send_raw(sock, res.encode())

        elif procedure == "hub_close_all":
            await self.hub.close_all()
            self._send_raw(sock, b'{"status":"OK"}\r\n')

        elif procedure == "hub_clear_old_logs":
            await self.hub.clear_old_logs()
            self._send_raw(sock, b'{"status":"OK"}\r\n')

        elif procedure == "hub_powerOn":
            await self.hub.powerOn()
            self._send_raw(sock, b'{"status":"OK"}\r\n')

        elif procedure == "hub_powerOff":
            await self.hub.powerOff()
            self._send_raw(sock, b'{"status":"OK"}\r\n')

        # Helper-backed procedures
        elif procedure == "default_get_measurement_last":
            res = await self._execute_afe_procedure(request_json, "default_get_measurement_last", 20.0)
            self._send_raw(sock, res + b"\r\n")

        elif procedure == "afe_configure":
            res = await self._execute_afe_procedure(request_json, "default_configure_afe", 60.0)
            self._send_raw(sock, res + b"\r\n")

        elif procedure == "afe_reset":
            res = await self._execute_afe_procedure(request_json, "reset", 10.0)
            self._send_raw(sock, res + b"\r\n")

        elif procedure == "afe_set_dac":
            res = await self._execute_afe_procedure(request_json, "default_set_dac", 10.0,
                                                     required_params_map={"dac_master": int, "dac_slave": int})
            self._send_raw(sock, res + b"\r\n")

        else:
            self._send_raw(sock, b'{"status":"ERROR","info":"Unknown procedure"}\r\n')

    def send_control_web_page_raw(self, client_sock):
        """
        Renders an HTML dashboard listing all AFE modules with 
        their respective parameters and real-time readings.
        """
        try:
            # 1. Fetch AFE channel data dynamically
            # Accepts dictionary structure: {'AFE1': {'Gain': 10, 'Voltage': 3.3, 'Status': 'OK'}}
            # or object lists depending on self.hub implementation.
            afe_data = getattr(self.hub, "afe_channels", {})
            
            if not afe_data and hasattr(self.hub, "get_afe_status"):
                afe_data = self.hub.get_afe_status()

            # 2. Build dynamic dynamic table rows for AFEs
            afe_rows = ""
            if afe_data:
                for afe_name, params in afe_data.items():
                    if isinstance(params, dict):
                        for param_name, val in params.items():
                            afe_rows += (
                                "<tr>"
                                "<td>%s</td>"
                                "<td>%s</td>"
                                "<td><strong>%s</strong></td>"
                                "</tr>\n" % (str(afe_name), str(param_name), str(val))
                            )
                    else:
                        afe_rows += (
                            "<tr>"
                            "<td>%s</td>"
                            "<td>Value</td>"
                            "<td><strong>%s</strong></td>"
                            "</tr>\n" % (str(afe_name), str(params))
                        )
            else:
                afe_rows = "<tr><td colspan='3'>No Active AFE Devices Detected</td></tr>"

            # 3. System Metrics
            free_ram = gc.mem_free()
            uptime_s = int(time.time())
            active_clients = len(self.client_sockets)

            # 4. Construct Full Webpage (%-formatting only)
            html_body = (
                "<!DOCTYPE html>\n"
                "<html>\n"
                "<head>\n"
                '    <meta name="viewport" content="width=device-width, initial-scale=1">\n'
                "    <title>HUB - AFE Status Dashboard</title>\n"
                "    <style>\n"
                "        body { font-family: monospace, sans-serif; margin: 15px; background: #1e1e1e; color: #d4d4d4; }\n"
                "        .card { background: #252526; padding: 15px; border-radius: 6px; border: 1px solid #3c3c3c; margin-bottom: 15px; }\n"
                "        h2 { color: #569cd6; margin-top: 0; font-size: 1.2em; }\n"
                "        table { width: 100%%; border-collapse: collapse; margin-top: 10px; }\n"
                "        td, th { padding: 6px 10px; text-align: left; border-bottom: 1px solid #333; font-size: 0.9em; }\n"
                "        th { background-color: #2d2d2d; color: #9cdcfe; }\n"
                "        tr:nth-child(even) { background-color: #1a1a1a; }\n"
                "    </style>\n"
                '    <meta http-equiv="refresh" content="3">\n'
                "</head>\n"
                "<body>\n"
                '    <div class="card">\n'
                "        <h2>Analog Front-End (AFE) Parameters</h2>\n"
                "        <table>\n"
                "            <thead>\n"
                "                <tr><th>AFE Module</th><th>Parameter</th><th>Latest Value</th></tr>\n"
                "            </thead>\n"
                "            <tbody>\n"
                "                %s\n"
                "            </tbody>\n"
                "        </table>\n"
                "    </div>\n"
                '    <div class="card">\n'
                "        <h2>System Health</h2>\n"
                "        <table>\n"
                "            <tr><td>Free RAM</td><td>%d bytes</td></tr>\n"
                "            <tr><td>Active Sockets</td><td>%d / %d</td></tr>\n"
                "            <tr><td>Uptime</td><td>%d s</td></tr>\n"
                "        </table>\n"
                "    </div>\n"
                "</body>\n"
                "</html>" % (
                    afe_rows,
                    free_ram,
                    active_clients,
                    self.tcp_requests_max,
                    uptime_s
                )
            )

            # 5. Build HTTP Headers and Payload
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                "Content-Length: %d\r\n"
                "Connection: close\r\n"
                "\r\n"
                "%s" % (len(html_body), html_body)
            )

            # 6. Send Response
            client_sock.sendall(response.encode("utf-8"))

        except OSError:
            pass  # Handle non-blocking socket disconnects safely
        
    # =========================================================================
    # RAW SOCKET POLLING & ZERO-ALLOCATION LOOP
    # =========================================================================

    def _accept_client(self):
            try:
                client_sock, client_addr = self.server_sock.accept()
                if len(self.client_sockets) >= self.tcp_requests_max:
                    client_sock.sendall(self.RESPONSE_SERVER_BUSY)
                    client_sock.close()
                    return

                client_sock.setblocking(False)
                
                # Register socket object directly with poller
                self.poller.register(client_sock, uselect.POLLIN)
                
                # Use unique memory ID as hashable dictionary key
                sock_id = id(client_sock)
                buf = bytearray(self.BUFFER_SIZE)
                
                self.client_sockets[sock_id] = [client_sock, time.time(), buf, 0]
                self.sock_map[sock_id] = client_sock
            except OSError:
                pass

    def _close_client(self, client_sock):
        """Unregisters socket from poller and safely cleans up references."""
        sock_id = id(client_sock)
        
        if sock_id in self.client_sockets:
            del self.client_sockets[sock_id]
        if sock_id in self.sock_map:
            del self.sock_map[sock_id]

        try:
            self.poller.unregister(client_sock)
        except Exception:
            pass
            
        try:
            client_sock.close()
        except Exception:
            pass

    def _find_newline_and_length(self, buf, length):
        """
        Finds the end of a line in bytearray 'buf' up to 'length'.
        Handles both '\r\n' and plain '\n'.
        Returns the length of the line including newline bytes, or -1 if incomplete.
        """
        for i in range(length):
            if buf[i] == 10:  # '\n'
                return i + 1
        return -1
    
    async def _process_client_read(self, client_sock):
        sock_id = id(client_sock)
        state = self.client_sockets.get(sock_id)
        if not state:
            return

        sock, _, buf, buf_len = state

        # Zero-allocation write slice
        free_space = memoryview(buf)[buf_len:]
        if len(free_space) == 0:
            self._close_client(client_sock)
            return

        try:
            bytes_read = sock.readinto(free_space)
            if bytes_read is None or bytes_read == 0:
                self._close_client(client_sock)
                return

            new_len = buf_len + bytes_read
            state[3] = new_len

            newline_idx = self._find_newline_and_length(buf, new_len)
            if newline_idx != -1:
                line_view = memoryview(buf)[:newline_idx]

                if newline_idx >= 3 and line_view[:3] == b"GET":
                    self.send_control_web_page_raw(sock)
                else:
                    await self.handle_procedure_raw(line_view, sock)

                self._close_client(client_sock)

        except OSError as e:
            if e.args[0] not in (11, 110):  # Not EAGAIN / EWOULDBLOCK
                self._close_client(client_sock)

    async def socket_poll_task(self):
        """Main non-blocking event loop driven by uselect.poll()."""
        while self.lan_connected and self.server_sock:
            events = self.poller.poll(0)  # Non-blocking poll
            
            for obj, event in events:
                if obj == self.server_sock:
                    self._accept_client()
                else:
                    sock_id = id(obj)
                    if sock_id in self.client_sockets:
                        # Removed POLLPRI — MicroPython only uses POLLIN for incoming data
                        if event & uselect.POLLIN:
                            await self._process_client_read(obj)
                        elif event & (uselect.POLLHUP | uselect.POLLERR):
                            self._close_client(obj)

            # Clean up timed-out sockets
            now = time.time()
            for sock_id, (sock, start_time, _, _) in list(self.client_sockets.items()):
                if now - start_time > self.CLIENT_TIMEOUT_S:
                    self._close_client(sock)

            await asyncio.sleep_ms(self.main_loop_yield_wait_ms)

    # =========================================================================
    # NTP SYNC & BACKGROUND TASKS
    # =========================================================================

    async def sync_rtc_with_ntp(self):
        global rtc_synced, p, rtc
        if not self.lan_connected:
            return False

        await p.print("Attempting NTP sync...")
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(3)
            addr = socket.getaddrinfo(NTP_HOST, 123)[0][-1]

            msg = bytearray(48)
            msg[0] = 0x1B
            s.sendto(msg, addr)

            start_t = time.ticks_ms()
            data = None
            while time.ticks_diff(time.ticks_ms(), start_t) < 3000:
                try:
                    data, _ = s.recvfrom(48)
                    if data:
                        break
                except OSError:
                    await asyncio.sleep_ms(100)

            if data:
                secs = struct.unpack("!I", data[40:44])[0]
                unix_secs = secs - NTP_DELTA
                tm = time.gmtime(unix_secs - 946684800)
                rtc.datetime((tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))
                self.ntp_synced = True
                rtc_synced = True
                if not self.hub.logger.rtc_synced:
                    self.hub.logger.rtc_synced = True
                    self.hub.logger.request_rename_file()
                return True
        except Exception as e:
            await p.print("NTP sync failed:", e)
            return False
        finally:
            if s:
                s.close()

    async def sync_ntp_loop(self):
        """Periodically syncs RTC with NTP."""
        while True:
            isSynced = await self.sync_rtc_with_ntp()
            if isSynced:
                await asyncio.sleep(self.sync_ntp_every_s)
            else:
                await asyncio.sleep(self.sync_ntp_loop_yield_wait_s)

    # =========================================================================
    # LIFECYCLE CONTROLS
    # =========================================================================

    async def start(self):
        while True:
            try:
                wdt.feed()
                if not self.lan_connected:
                    await p.print("Attempting to reconnect Ethernet...")
                    try:
                        await self.connect_ethernet()
                    except RuntimeError:
                        await p.print("Ethernet connection failed. Retrying in 10s.")
                        await asyncio.sleep(10)
                        continue

                if self.server_sock is None:
                    await p.print("Attempting to start server...")
                    try:
                        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        self.server_sock.setblocking(False)
                        self.server_sock.bind(("0.0.0.0", self.port))
                        self.server_sock.listen(5)

                        self.poller.register(self.server_sock, uselect.POLLIN)  # Pass the socket directly
                        asyncio.create_task(self.socket_poll_task())
                        await p.print("Server running at http://{}:{}".format(self.lan.ifconfig()[0], self.port))
                    except Exception as e:
                        await p.print("Failed to start server: {}. Retrying in 10s.".format(e))
                        self.server_sock = None
                        await asyncio.sleep(10)
                        continue

                if is_timeout(self.last_lan_check_ms, 5000):
                    if not self.lan.isconnected():
                        await p.print("Ethernet disconnected.")
                        self.lan_connected = False
                        if self.server_sock:
                            self.poller.unregister(self.server_sock)
                            self.server_sock.close()
                            self.server_sock = None
                    self.last_lan_check_ms = millis()
            except Exception as e:
                await p.print("AsyncWebServer main loop error:", e)
            await asyncio.sleep_ms(self.main_loop_yield_wait_ms)

    def run(self):
        try:
            asyncio.run(self.start())
        except KeyboardInterrupt:
            print("Server stopped")