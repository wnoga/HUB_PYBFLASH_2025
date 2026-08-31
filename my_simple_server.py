import gc
import network
import socket
import struct
import time
import uasyncio as asyncio
import ujson
import uos
import uselect
import utime

json = ujson

from HUB import HUBDevice
from my_utilities import (
    dump_json_sorted,
    is_timeout,
    millis,
    p,
    rtc,
    rtc_datetime_pretty,
    rtc_synced,
    wdt,
)

# NTP constants
NTP_DELTA = 2208988800  # Seconds between NTP epoch (1900) and Unix epoch (1970)
NTP_HOST = "pool.ntp.org"


class AsyncWebServer:

    def __init__(
        self, hub: HUBDevice, dhcp=True, static_ip_config=None, port=5555
    ):
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
        self.procedure_results = {}
        self.procedure_events = {}
        self.max_procedures_keep_len = 32

        # Raw Socket Polling & Configuration
        self.poller = uselect.poll()
        self.BUFFER_SIZE = 512
        self.client_sockets = {}
        self.sock_map = {}
        self.tcp_requests_max = 10
        self.CLIENT_TIMEOUT_S = 5

        self.poll_task = None
        self.ntp_task = None

        self.RESPONSE_SERVER_BUSY = (
            b'{"status":"ERROR","info":"Server busy"}\r\n'
        )

    # =========================================================================
    # LOW-RAM RAW SOCKET HELPERS (ASYNC NON-BLOCKING)
    # =========================================================================

    async def _send_raw(self, sock, data: bytes):
        """Async helper to send bytes over non-blocking raw socket without freezing loop."""
        total_sent = 0
        while total_sent < len(data):
            try:
                sent = sock.send(data[total_sent:])
                if sent == 0 or sent is None:
                    raise OSError("Socket closed by peer")
                total_sent += sent
            except OSError as e:
                if e.args[0] in (11, 110):  # EAGAIN / EWOULDBLOCK
                    await asyncio.sleep_ms(10)
                    continue
                raise e

    async def _send_json_dict_chunked(self, sock, data: dict):
        """Serializes dictionary directly to socket in small chunks."""
        await self._send_raw(sock, b"{")
        first = True
        for key, value in data.items():
            if not first:
                await self._send_raw(sock, b",")
            first = False
            await self._send_raw(sock, ujson.dumps(key).encode())
            await self._send_raw(sock, b":")
            await self._send_raw(sock, ujson.dumps(value).encode())
        await self._send_raw(sock, b"}")

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
        my_dict = {
            k: v for k, v in msg.items() if k not in ("frame", "callback")
        }

        if (
            device_id not in self.procedure_results
            and len(self.procedure_results) >= self.max_procedures_keep_len
        ):
            await p.print(
                "AsyncWebServer: procedure_results cache full. Evicting item."
            )
            try:
                self.procedure_results.pop(next(iter(self.procedure_results)))
            except (StopIteration, KeyError):
                pass

        try:
            self.procedure_results[device_id] = ujson.dumps(my_dict).encode()
        except Exception as e:
            await p.print(
                "hub_cb: Error serializing result for AFE {}: {}".format(
                    device_id, e
                )
            )
            self.procedure_results[device_id] = ujson.dumps(
                {"status": "ERROR", "info": "Failed to serialize response"}
            ).encode()

        event = self.procedure_events.get(device_id)
        if event:
            event.set()

    async def _execute_afe_procedure(
        self,
        request_json,
        hub_method_name,
        timeout_duration,
        required_params_map=None,
    ):
        afe_id = request_json.get("afe_id", None)
        if afe_id is None:
            return ujson.dumps(
                {"status": "ERROR", "info": "afe_id missing"}
            ).encode()

        hub_call_kwargs = {"callback": self.hub_cb}

        if required_params_map:
            for param_name, converter_func in required_params_map.items():
                param_value_str = request_json.get(param_name, None)
                if param_value_str is None:
                    return ujson.dumps(
                        {
                            "status": "ERROR",
                            "info": "Parameter '{}' missing".format(param_name),
                        }
                    ).encode()
                try:
                    hub_call_kwargs[param_name] = converter_func(
                        param_value_str
                    )
                except ValueError:
                    return ujson.dumps(
                        {
                            "status": "ERROR",
                            "info": "Invalid value for parameter '{}'".format(
                                param_name
                            ),
                        }
                    ).encode()

        if (
            afe_id not in self.procedure_events
            and len(self.procedure_events) >= self.max_procedures_keep_len
        ):
            return ujson.dumps(
                {
                    "status": "ERROR",
                    "info": "Server busy, max concurrent procedures reached",
                }
            ).encode()

        event = asyncio.Event()
        self.procedure_events[afe_id] = event
        self.procedure_results.pop(afe_id, None)

        try:
            hub_method_ref = getattr(self.hub, hub_method_name)
        except AttributeError:
            return ujson.dumps(
                {"status": "ERROR", "info": "Procedure handle missing"}
            ).encode()

        async def _execute_and_wait():
            await hub_method_ref(afe_id=afe_id, **hub_call_kwargs)
            await event.wait()

        try:
            await asyncio.wait_for(
                _execute_and_wait(), timeout=timeout_duration
            )
            result_data = self.procedure_results.pop(afe_id, None)
            return (
                result_data
                if result_data
                else ujson.dumps({"status": "OK"}).encode()
            )
        except asyncio.TimeoutError:
            await p.print(
                "Timeout executing '{}' for AFE {}".format(
                    hub_method_name, afe_id
                )
            )
            return ujson.dumps(
                {
                    "status": "ERROR",
                    "info": "Timeout waiting for AFE response",
                }
            ).encode()
        except Exception as e:
            await p.print(
                "Error executing '{}' for AFE {}: {}".format(
                    hub_method_name, afe_id, e
                )
            )
            return ujson.dumps(
                {"status": "ERROR", "info": "Server execution error"}
            ).encode()
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
            await self._send_raw(
                sock, b'{"status":"ERROR","info":"Invalid JSON"}\r\n'
            )
            return

        procedure = request_json.get("procedure")
        if not procedure:
            await self._send_raw(
                sock, b'{"status":"ERROR","info":"Procedure missing"}\r\n'
            )
            return

        if procedure == "get_all_afe_configuration":
            await self._send_raw(sock, b"{")
            first = True
            for afe_device in self.hub.afe_devices:
                if not first:
                    await self._send_raw(sock, b",")
                first = False
                await self._send_raw(
                    sock, ('"' + str(afe_device.device_id) + '":').encode()
                )
                await self._send_raw(
                    sock, ujson.dumps(afe_device.configuration).encode()
                )
            await self._send_raw(sock, b"}\r\n")

        elif procedure == "get_all_latest_status":
            await self._send_raw(sock, b"{")
            first = True
            for afe_device in self.hub.afe_devices:
                if not first:
                    await self._send_raw(sock, b",")
                first = False
                await self._send_raw(
                    sock, ('"' + str(afe_device.device_id) + '":').encode()
                )
                await self._send_json_dict_chunked(sock, afe_device.latest_status)
            await self._send_raw(sock, b"}\r\n")

        elif procedure == "get_all_afe_id":
            ids = [str(afe.device_id) for afe in self.hub.afe_devices]
            res = '{"available_afe":[' + ",".join(ids) + "]}\r\n"
            await self._send_raw(sock, res.encode())

        elif procedure == "hub_close_all":
            await self.hub.close_all()
            await self._send_raw(sock, b'{"status":"OK"}\r\n')

        elif procedure == "hub_clear_old_logs":
            await self.hub.clear_old_logs()
            await self._send_raw(sock, b'{"status":"OK"}\r\n')

        elif procedure == "hub_powerOn":
            await self.hub.powerOn()
            await self._send_raw(sock, b'{"status":"OK"}\r\n')

        elif procedure == "hub_powerOff":
            await self.hub.powerOff()
            await self._send_raw(sock, b'{"status":"OK"}\r\n')

        elif procedure == "default_get_measurement_last":
            res = await self._execute_afe_procedure(
                request_json, "default_get_measurement_last", 20.0
            )
            await self._send_raw(sock, res + b"\r\n")

        elif procedure == "afe_configure":
            res = await self._execute_afe_procedure(
                request_json, "default_configure_afe", 60.0
            )
            await self._send_raw(sock, res + b"\r\n")

        elif procedure == "afe_reset":
            res = await self._execute_afe_procedure(
                request_json, "reset", 10.0
            )
            await self._send_raw(sock, res + b"\r\n")

        elif procedure == "afe_set_dac":
            res = await self._execute_afe_procedure(
                request_json,
                "default_set_dac",
                10.0,
                required_params_map={"dac_master": int, "dac_slave": int},
            )
            await self._send_raw(sock, res + b"\r\n")

        else:
            await self._send_raw(
                sock, b'{"status":"ERROR","info":"Unknown procedure"}\r\n'
            )

    def format_afe_status_html(self, status_data):
            """Generates a compact, dark-themed HTML card for SiPM telemetry."""
            if not isinstance(status_data, dict) or "last_data" not in status_data:
                return "<pre>%s</pre>" % str(status_data)

            last = status_data.get("last_data", {})

            # Extract values
            u0 = last.get("U_SIPM_MEAS0", {}).get("value", 0.0)
            u1 = last.get("U_SIPM_MEAS1", {}).get("value", 0.0)

            # Convert A to nA for better readability
            i0_na = last.get("I_SIPM_MEAS0", {}).get("value", 0.0) * 1e9
            i1_na = last.get("I_SIPM_MEAS1", {}).get("value", 0.0) * 1e9

            dc0 = last.get("DC_LEVEL_MEAS0", {}).get("value", 0)
            dc1 = last.get("DC_LEVEL_MEAS1", {}).get("value", 0)

            temp_ext = last.get("TEMP_EXT", {}).get("value", 0.0)
            temp_loc = last.get("TEMP_LOCAL", {}).get("value", 0.0)
            uid = status_data.get("unique_id_str", {}).get("value", "N/A")

            return (
                '<div style="margin-top: 5px;">'
                '<div class="sys-row">'
                "<span><strong>Ext Temp:</strong> %.1f °C</span>"
                "<span><strong>Loc Temp:</strong> %.1f °C</span>"
                "<span><strong>UID:</strong> %s</span>"
                "</div>"
                '<div class="sipm-grid">'
                '<div class="sipm-chan">'
                "<h3>Channel 0</h3>"
                '<div class="metric"><span>Bias (U):</span><span class="metric-val">%.2f V</span></div>'
                '<div class="metric"><span>Current (I):</span><span class="metric-val">%.1f nA</span></div>'
                '<div class="metric"><span>DC Offset:</span><span class="metric-val">%d</span></div>'
                "</div>"
                '<div class="sipm-chan">'
                "<h3>Channel 1</h3>"
                '<div class="metric"><span>Bias (U):</span><span class="metric-val">%.2f V</span></div>'
                '<div class="metric"><span>Current (I):</span><span class="metric-val">%.1f nA</span></div>'
                '<div class="metric"><span>DC Offset:</span><span class="metric-val">%d</span></div>'
                "</div>"
                "</div>"
                "</div>"
                % (temp_ext, temp_loc, uid, u0, i0_na, int(dc0), u1, i1_na, int(dc1))
            )

    async def send_control_web_page_raw(self, reader, writer):
        """Streams an HTML dashboard in chunks using non-blocking uasyncio Streams."""
        try:
            gc.collect()

            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                "Transfer-Encoding: chunked\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(header.encode("utf-8"))
            await writer.drain()

            async def send_chunk(text):
                if not text:
                    return
                data = text.encode("utf-8")
                writer.write(("%X\r\n" % len(data)).encode("utf-8"))
                writer.write(data)
                writer.write(b"\r\n")
                await writer.drain()

            await send_chunk(
                "<!DOCTYPE html><html><head>"
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                "<title>HUB - Status Dashboard</title><style>"
                "body{font-family:monospace,sans-serif;margin:15px;background:#1e1e1e;color:#d4d4d4;}"
                ".card{background:#252526;padding:15px;border-radius:6px;border:1px solid #3c3c3c;margin-bottom:15px;}"
                "h2{color:#569cd6;margin-top:0;font-size:1.1em;border-bottom:1px solid #3c3c3c;padding-bottom:5px;}"
                "table{width:100%;border-collapse:collapse;margin-top:10px;}"
                "td,th{padding:6px 10px;text-align:left;border-bottom:1px solid #333;font-size:0.85em;vertical-align:top;}"
                "th{color:#9cdcfe;width:30%;background-color:#2d2d2d;}"
                ".badge-on{background:#28a745;color:#fff;padding:2px 6px;border-radius:3px;font-weight:bold;}"
                ".badge-off{background:#dc3545;color:#fff;padding:2px 6px;border-radius:3px;font-weight:bold;}"
                "pre{margin:0;white-space:pre-wrap;word-wrap:break-word;color:#ce9178;font-size:0.85em;}"
                ".sipm-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px;}"
                ".sipm-chan{background:#1e1e1e;padding:8px;border-radius:4px;border:1px solid #333;}"
                ".sipm-chan h3{margin:0 0 6px 0;font-size:0.9em;color:#4ec9b0;border-bottom:1px solid #333;padding-bottom:3px;}"
                ".metric{display:flex;justify-content:space-between;font-size:0.8em;margin-bottom:3px;}"
                ".metric-val{font-weight:bold;color:#b5cea8;}"
                ".sys-row{display:flex;gap:15px;background:#1e1e1e;padding:6px 10px;border-radius:4px;font-size:0.8em;margin-top:4px;}"
                '</style><meta http-equiv="refresh" content="5"></head><body>'
            )

            try:
                current_ms = getattr(self, "millis", utime.ticks_ms)()
                start_time = getattr(self.hub, "discovery_start_time", 0)
                timeout_ms = getattr(self.hub, "discovery_timeout_ms", 0)
                discovery_timed_out = (
                    utime.ticks_diff(current_ms, start_time) >= timeout_ms
                )
            except Exception:
                discovery_timed_out = "N/A"

            afe_manage_active = getattr(self.hub, "afe_manage_active", False)
            rx_process_active = getattr(self.hub, "rx_process_active", False)

            await send_chunk(
                '<div class="card"><h2>Current HUB Status</h2><table>'
                "<tr><th>Discovery Timed Out</th><td>%s</td></tr>"
                "<tr><th>AFE Manage Active</th><td>%s</td></tr>"
                "<tr><th>RX Process Active</th><td>%s</td></tr>"
                "</table></div>"
                % (
                    (
                        '<span class="badge-off">TRUE</span>'
                        if discovery_timed_out is True
                        else (
                            '<span class="badge-on">FALSE</span>'
                            if discovery_timed_out is False
                            else "N/A"
                        )
                    ),
                    (
                        '<span class="badge-on">ACTIVE</span>'
                        if afe_manage_active
                        else '<span class="badge-off">INACTIVE</span>'
                    ),
                    (
                        '<span class="badge-on">ACTIVE</span>'
                        if rx_process_active
                        else '<span class="badge-off">INACTIVE</span>'
                    ),
                )
            )

            afe_devices = getattr(self.hub, "afe_devices", [])
            if afe_devices:
                for afe in afe_devices:
                    dev_id = getattr(afe, "device_id", "Unknown")
                    config = getattr(afe, "configuration", {})

                    is_online = getattr(afe, "is_online", False)
                    firmware_version = getattr(afe, "firmware_version", "N/A")
                    version_checked = getattr(afe, "version_checked", False)
                    is_configured = getattr(afe, "is_configured", False)
                    is_config_started = getattr(
                        afe, "is_configuration_started", False
                    )

                    if isinstance(config, dict):
                        config_str = " | ".join(
                            "%s: %s" % (k, v) for k, v in config.items()
                        )
                    else:
                        config_str = str(config)

                    await send_chunk(
                        '<div class="card"><h2>AFE Device: %s</h2><table>'
                        "<tr><th>Online Status</th><td>%s</td></tr>"
                        "<tr><th>Firmware Version</th><td><strong>%s</strong></td></tr>"
                        "<tr><th>Version Checked</th><td>%s</td></tr>"
                        "<tr><th>Configured</th><td>%s</td></tr>"
                        "<tr><th>Configuration Started</th><td>%s</td></tr>"
                        "<tr><th>Configuration</th><td>%s</td></tr>"
                        % (
                            str(dev_id),
                            (
                                '<span class="badge-on">ONLINE</span>'
                                if is_online
                                else '<span class="badge-off">OFFLINE</span>'
                            ),
                            str(firmware_version),
                            (
                                '<span class="badge-on">YES</span>'
                                if version_checked
                                else '<span class="badge-off">NO</span>'
                            ),
                            (
                                '<span class="badge-on">YES</span>'
                                if is_configured
                                else '<span class="badge-off">NO</span>'
                            ),
                            (
                                '<span class="badge-on">YES</span>'
                                if is_config_started
                                else '<span class="badge-off">NO</span>'
                            ),
                            config_str if config_str else "N/A",
                        )
                    )

                    # Render Visual Telemetry Card instead of raw JSON
                    raw_status = getattr(afe, "latest_status", {})
                    status_html = self.format_afe_status_html(raw_status)

                    await send_chunk(
                        "<tr><th>Telemetry</th><td>%s</td></tr>"
                        "</table></div>" % status_html
                    )
                    gc.collect()
            else:
                await send_chunk(
                    '<div class="card"><h2>AFE Devices</h2><p>No AFE devices detected.</p></div>'
                )

            free_ram = gc.mem_free()
            uptime_s = int(utime.time())
            active_clients = len(self.client_sockets)

            await send_chunk(
                '<div class="card"><h2>Server Metrics</h2><table>'
                "<tr><th>Free RAM</th><td>%d bytes</td></tr>"
                "<tr><th>Active Sockets</th><td>%d / %d</td></tr>"
                "<tr><th>Uptime</th><td>%d s</td></tr>"
                "</table></div></body></html>"
                % (
                    free_ram,
                    active_clients,
                    self.tcp_requests_max,
                    uptime_s,
                )
            )

            writer.write(b"0\r\n\r\n")
            await writer.drain()

        except OSError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

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
            self.poller.register(client_sock, uselect.POLLIN)

            sock_id = id(client_sock)
            buf = bytearray(self.BUFFER_SIZE)

            self.client_sockets[sock_id] = [client_sock, time.time(), buf, 0]
            self.sock_map[sock_id] = client_sock
        except OSError:
            pass

    def _find_newline_and_length(self, buf, length):
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
                    # Adapt raw socket to StreamWriter/StreamReader interface
                    reader = asyncio.StreamReader(client_sock)
                    writer = asyncio.StreamWriter(client_sock, {})
                    await self.send_control_web_page_raw(reader, writer)
                else:
                    await self.handle_procedure_raw(line_view, client_sock)

                self._close_client(client_sock)

        except OSError as e:
            if e.args[0] not in (11, 110):  # Not EAGAIN / EWOULDBLOCK
                self._close_client(client_sock)

    async def socket_poll_task(self):
        """Main non-blocking event loop driven by uselect.poll()."""
        while self.lan_connected and self.server_sock:
            events = self.poller.poll(0)

            for obj, event in events:
                if obj == self.server_sock:
                    self._accept_client()
                else:
                    sock_id = id(obj)
                    if sock_id in self.client_sockets:
                        if event & uselect.POLLIN:
                            await self._process_client_read(obj)
                        elif event & (uselect.POLLHUP | uselect.POLLERR):
                            self._close_client(obj)

            now = time.time()
            for sock_id, (sock, start_time, _, _) in list(
                self.client_sockets.items()
            ):
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
                rtc.datetime(
                    (tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0)
                )
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
                        await p.print(
                            "Ethernet connection failed. Retrying in 10s."
                        )
                        await asyncio.sleep(10)
                        continue

                if self.server_sock is None:
                    await p.print("Attempting to start server...")
                    try:
                        self.server_sock = socket.socket(
                            socket.AF_INET, socket.SOCK_STREAM
                        )
                        self.server_sock.setsockopt(
                            socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
                        )
                        self.server_sock.setblocking(False)
                        self.server_sock.bind(("0.0.0.0", self.port))
                        self.server_sock.listen(5)

                        self.poller.register(self.server_sock, uselect.POLLIN)

                        # Cleanly handle task creation
                        if self.poll_task:
                            self.poll_task.cancel()
                        self.poll_task = asyncio.create_task(
                            self.socket_poll_task()
                        )

                        if not self.ntp_task:
                            self.ntp_task = asyncio.create_task(
                                self.sync_ntp_loop()
                            )

                        await p.print(
                            "Server running at http://{}:{}".format(
                                self.lan.ifconfig()[0], self.port
                            )
                        )
                    except Exception as e:
                        await p.print(
                            "Failed to start server: {}. Retrying in 10s.".format(
                                e
                            )
                        )
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