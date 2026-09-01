import gc
import errno
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

DASHBOARD_CSS = (
    b"body{font-family:monospace,sans-serif;margin:15px;background:#1e1e1e;color:#d4d4d4;}"
    b".card{background:#252526;padding:15px;border-radius:6px;border:1px solid #3c3c3c;margin-bottom:15px;}"
    b"h2{color:#569cd6;margin-top:0;font-size:1.1em;border-bottom:1px solid #3c3c3c;padding-bottom:5px;}"
    b"table{width:100%;border-collapse:collapse;margin-top:10px;}"
    b"td,th{padding:6px 10px;text-align:left;border-bottom:1px solid #333;font-size:0.85em;vertical-align:top;}"
    b"th{color:#9cdcfe;width:30%;background-color:#2d2d2d;}"
    b".badge-on{background:#28a745;color:#fff;padding:2px 6px;border-radius:3px;font-weight:bold;}"
    b".badge-off{background:#dc3545;color:#fff;padding:2px 6px;border-radius:3px;font-weight:bold;}"
    b"pre{margin:0;white-space:pre-wrap;word-wrap:break-word;color:#ce9178;font-size:0.85em;}"
    b".sipm-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px;}"
    b".sipm-chan{background:#1e1e1e;padding:8px;border-radius:4px;border:1px solid #333;}"
    b".sipm-chan h3{margin:0 0 6px 0;font-size:0.9em;color:#4ec9b0;border-bottom:1px solid #333;padding-bottom:3px;}"
    b".metric{display:flex;justify-content:space-between;font-size:0.8em;margin-bottom:3px;}"
    b".metric-val{font-weight:bold;color:#b5cea8;}"
    b".sys-row{display:flex;gap:15px;background:#1e1e1e;padding:6px 10px;border-radius:4px;font-size:0.8em;margin-top:4px;}"
    b".scroll-box{max-height:140px;overflow-y:auto;background:#1e1e1e;padding:8px;border-radius:4px;border:1px solid #3c3c3c;}"
    b".scroll-box::-webkit-scrollbar{width:6px;}"
    b".scroll-box::-webkit-scrollbar-track{background:#1e1e1e;}"
    b".scroll-box::-webkit-scrollbar-thumb{background:#3c3c3c;border-radius:3px;}"
    b".btn-dl{background:#0e639c;color:#fff;padding:3px 8px;text-decoration:none;border-radius:3px;font-size:0.85em;display:inline-block;}"
    b".btn-dl:hover{background:#1177bb;}"
    b"tr:nth-child(even){background:#333;}"
    b"tr:hover {background:#515;}"
)


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
        total_sent = 0
        view = memoryview(data)
        while total_sent < len(view):
            try:
                sent = sock.send(view[total_sent:])
                if sent == 0 or sent is None:
                    raise OSError("Socket closed by peer")
                total_sent += sent
            except OSError as e:
                err = e.errno if hasattr(e, "errno") else (e.args[0] if e.args else None)
                if err in (errno.EAGAIN, errno.ETIMEDOUT, 11, 110):
                    await asyncio.sleep_ms(10)
                    continue
                raise e


    async def _send_chunk_raw(self, writer, data: bytes, max_chunk: int = 512):
        """Streams byte data in chunks up to max_chunk bytes to prevent large allocations."""
        if not data:
            return

        view = memoryview(data)
        total_len = len(view)
        offset = 0

        while offset < total_len:
            chunk_len = min(max_chunk, total_len - offset)
            sub_chunk = view[offset : offset + chunk_len]

            # HTTP Chunk header (<hex_size>\r\n)
            header = ("%X\r\n" % chunk_len).encode("ascii")
            writer.write(header)
            writer.write(sub_chunk)
            writer.write(b"\r\n")
            await writer.drain()

            offset += chunk_len


    async def _send_chunk_str(self, writer, text: str, max_chunk: int = 512):
        """Encodes and streams string data in controlled 512-byte allocations."""
        if not text:
            return

        for i in range(0, len(text), max_chunk):
            slice_str = text[i : i + max_chunk]
            data = slice_str.encode("utf-8")
            await self._send_chunk_raw(writer, data, max_chunk=max_chunk)


    async def _stream_json_key_by_key(self, writer_or_sock, obj, is_async_writer=True):
        """
        Recursively serializes and sends JSON key-by-key / element-by-element
        to keep allocations well below 512 bytes.
        """
        async def _write_bytes(raw_b):
            if is_async_writer:
                await self._send_chunk_raw(writer_or_sock, raw_b, max_chunk=512)
            else:
                await self._send_raw(writer_or_sock, raw_b)

        if obj is None:
            await _write_bytes(b"null")
        elif isinstance(obj, bool):
            await _write_bytes(b"true" if obj else b"false")
        elif isinstance(obj, (int, float)):
            await _write_bytes(str(obj).encode("ascii"))
        elif isinstance(obj, str):
            await _write_bytes(ujson.dumps(obj).encode("utf-8"))
        elif isinstance(obj, dict):
            await _write_bytes(b"{")
            first = True
            for k, v in obj.items():
                if not first:
                    await _write_bytes(b",")
                first = False

                await _write_bytes(ujson.dumps(str(k)).encode("utf-8"))
                await _write_bytes(b":")

                await self._stream_json_key_by_key(writer_or_sock, v, is_async_writer)
                gc.collect()

            await _write_bytes(b"}")
        elif isinstance(obj, (list, tuple)):
            await _write_bytes(b"[")
            first = True
            for item in obj:
                if not first:
                    await _write_bytes(b",")
                first = False

                await self._stream_json_key_by_key(writer_or_sock, item, is_async_writer)
                gc.collect()

            await _write_bytes(b"]")


    def sort_log_files(self, file_list):
        """
        Sorts log files from latest to oldest.
        Handles:
        - Timestamped: log_YYYYMMDD_HHMMSS.json
        - Unsynced indexed: log_1.json, log_2.json
        - Unsynced base: log.json
        """
        def file_sort_key(filename):
            if filename == "log.json":
                return (1, 0)

            if filename.startswith("log_") and filename.endswith(".json"):
                core = filename[4:-5]
                if core.isdigit():
                    return (2, int(core))
                return (3, core)

            return (0, filename)

        return sorted(file_list, key=file_sort_key, reverse=True)


    async def stream_afe_status_html(self, writer, status_data):
        """Streams the AFE status HTML directly to the writer in sub-512 byte chunks."""
        if not isinstance(status_data, dict) or "last_data" not in status_data:
            return

        last = status_data.get("last_data", {})

        u0 = last.get("U_SIPM_MEAS0", {}).get("value", 0.0)
        u1 = last.get("U_SIPM_MEAS1", {}).get("value", 0.0)
        i0_na = last.get("I_SIPM_MEAS0", {}).get("value", 0.0) * 1e9
        i1_na = last.get("I_SIPM_MEAS1", {}).get("value", 0.0) * 1e9
        dc0 = last.get("DC_LEVEL_MEAS0", {}).get("value", 0)
        dc1 = last.get("DC_LEVEL_MEAS1", {}).get("value", 0)
        temp_ext = last.get("TEMP_EXT", {}).get("value", 0.0)
        temp_loc = last.get("TEMP_LOCAL", {}).get("value", 0.0)
        uid = status_data.get("unique_id_str", {}).get("value", "N/A")

        await self._send_chunk_str(
            writer,
            '<div class="sys-row">'
            "<span><strong>Ext Temp:</strong> %.1f °C</span>"
            "<span><strong>Loc Temp:</strong> %.1f °C</span>"
            "<span><strong>UID:</strong> %s</span>"
            "</div>" % (temp_ext, temp_loc, str(uid)),
            max_chunk=512,
        )

        await self._send_chunk_str(
            writer,
            '<div class="sipm-grid">'
            '<div class="sipm-chan"><h3>Channel 0</h3>'
            '<div class="metric"><span>Bias (U):</span><span class="metric-val">%.2f V</span></div>'
            '<div class="metric"><span>Current (I):</span><span class="metric-val">%.1f nA</span></div>'
            '<div class="metric"><span>DC Offset:</span><span class="metric-val">%d</span></div>'
            "</div>" % (u0, i0_na, int(dc0)),
            max_chunk=512,
        )

        await self._send_chunk_str(
            writer,
            '<div class="sipm-chan"><h3>Channel 1</h3>'
            '<div class="metric"><span>Bias (U):</span><span class="metric-val">%.2f V</span></div>'
            '<div class="metric"><span>Current (I):</span><span class="metric-val">%.1f nA</span></div>'
            '<div class="metric"><span>DC Offset:</span><span class="metric-val">%d</span></div>'
            "</div></div>" % (u1, i1_na, int(dc1)),
            max_chunk=512,
        )


    async def send_control_web_page_raw(self, reader, writer):
        """Streams HTML dashboard keeping allocations strictly under 512 bytes and JSON key-by-key."""
        try:
            gc.collect()

            # 1. HTTP Headers
            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                "Transfer-Encoding: chunked\r\n"
                "Connection: close\r\n\r\n"
            )
            await self._send_chunk_raw(writer, header.encode("ascii"), max_chunk=512)

            # 2. Document Shell & Static CSS
            await self._send_chunk_raw(
                writer,
                b'<!DOCTYPE html><html><head>'
                b'<meta name="viewport" content="width=device-width, initial-scale=1">'
                b'<title>HUB - Status Dashboard</title><style>',
                max_chunk=512,
            )

            await self._send_chunk_raw(writer, DASHBOARD_CSS, max_chunk=512)

            await self._send_chunk_raw(
                writer,
                b'</style><meta http-equiv="refresh" content="5"></head><body>',
                max_chunk=512,
            )

            # 3. System Status
            gc.collect()
            current_ms = getattr(self, "millis", utime.ticks_ms)()
            start_time = getattr(self.hub, "discovery_start_time", 0)
            timeout_ms = getattr(self.hub, "discovery_timeout_ms", 0)

            try:
                discovery_timed_out = utime.ticks_diff(current_ms, start_time) >= timeout_ms
            except Exception:
                discovery_timed_out = "N/A"

            afe_manage_active = getattr(self.hub, "afe_manage_active", False)
            rx_process_active = getattr(self.hub, "rx_process_active", False)

            await self._send_chunk_str(
                writer,
                '<div class="card"><h2>Current HUB Status</h2><table>'
                "<tr><th>Discovery Timed Out</th><td>%s</td></tr>"
                "<tr><th>AFE Manage Active</th><td>%s</td></tr>"
                "<tr><th>RX Process Active</th><td>%s</td></tr>"
                "</table></div>"
                % (
                    '<span class="badge-off">TRUE</span>' if discovery_timed_out is True
                    else ('<span class="badge-on">FALSE</span>' if discovery_timed_out is False else "N/A"),
                    '<span class="badge-on">ACTIVE</span>' if afe_manage_active else '<span class="badge-off">INACTIVE</span>',
                    '<span class="badge-on">ACTIVE</span>' if rx_process_active else '<span class="badge-off">INACTIVE</span>',
                ),
                max_chunk=512,
            )

            # 4. AFE Devices
            afe_devices = getattr(self.hub, "afe_devices", [])
            if afe_devices:
                for afe in afe_devices:
                    gc.collect()
                    dev_id = getattr(afe, "device_id", "Unknown")
                    config = getattr(afe, "configuration", {})

                    is_online = getattr(afe, "is_online", False)
                    firmware_version = getattr(afe, "firmware_version", "N/A")
                    version_checked = getattr(afe, "version_checked", False)
                    is_configured = getattr(afe, "is_configured", False)
                    is_config_started = getattr(afe, "is_configuration_started", False)

                    await self._send_chunk_str(
                        writer,
                        '<div class="card"><h2>AFE Device: %s</h2><table>'
                        "<tr><th>Online Status</th><td>%s</td></tr>"
                        "<tr><th>Firmware Version</th><td><strong>%s</strong></td></tr>"
                        "<tr><th>Version Checked</th><td>%s</td></tr>"
                        "<tr><th>Configured</th><td>%s</td></tr>"
                        "<tr><th>Configuration Started</th><td>%s</td></tr>"
                        '<tr><th>Configuration (JSON)</th><td><div class="scroll-box"><pre>'
                        % (
                            str(dev_id),
                            '<span class="badge-on">ONLINE</span>' if is_online else '<span class="badge-off">OFFLINE</span>',
                            str(firmware_version),
                            '<span class="badge-on">YES</span>' if version_checked else '<span class="badge-off">NO</span>',
                            '<span class="badge-on">YES</span>' if is_configured else '<span class="badge-off">NO</span>',
                            '<span class="badge-on">YES</span>' if is_config_started else '<span class="badge-off">NO</span>',
                        ),
                        max_chunk=512,
                    )

                    # Stream Configuration JSON Key-by-Key
                    if isinstance(config, (dict, list)):
                        await self._stream_json_key_by_key(writer, config, is_async_writer=True)
                    else:
                        await self._send_chunk_str(writer, str(config), max_chunk=512)

                    await self._send_chunk_str(writer, "</pre></div></td></tr>", max_chunk=512)

                    # Stream Latest Data JSON Key-by-Key
                    raw_status = getattr(afe, "latest_status", {})
                    await self._send_chunk_str(
                        writer,
                        '<tr><th>Latest Data (JSON)</th><td><div class="scroll-box"><pre>',
                        max_chunk=512,
                    )

                    if isinstance(raw_status, (dict, list)):
                        await self._stream_json_key_by_key(writer, raw_status, is_async_writer=True)
                    else:
                        await self._send_chunk_str(writer, str(raw_status), max_chunk=512)

                    await self._send_chunk_str(writer, "</pre></div></td></tr>", max_chunk=512)

                    # Stream Visual Telemetry Card directly to socket
                    if isinstance(raw_status, dict) and "last_data" in raw_status:
                        await self._send_chunk_str(
                            writer,
                            "<tr><th>Telemetry Visual</th><td>",
                            max_chunk=512,
                        )
                        await self.stream_afe_status_html(writer, raw_status)
                        await self._send_chunk_str(writer, "</td></tr>", max_chunk=512)

                    await self._send_chunk_str(writer, "</table></div>", max_chunk=512)
            else:
                await self._send_chunk_str(
                    writer,
                    '<div class="card"><h2>AFE Devices</h2><p>No AFE devices detected.</p></div>',
                    max_chunk=512,
                )

            # 5. Server Metrics
            gc.collect()
            free_ram = gc.mem_free()
            uptime_s = int(utime.time())
            active_clients = len(getattr(self, "client_sockets", []))

            await self._send_chunk_str(
                writer,
                '<div class="card"><h2>Server Metrics</h2><table>'
                "<tr><th>Free RAM</th><td>%d bytes</td></tr>"
                "<tr><th>Active Sockets</th><td>%d / %d</td></tr>"
                "<tr><th>Uptime</th><td>%d s</td></tr>"
                "</table></div>"
                % (free_ram, active_clients, getattr(self, "tcp_requests_max", 0), uptime_s),
                max_chunk=512,
            )

            # 6. SD Card Storage Metrics
            gc.collect()
            total_mb, free_mb, used_pct, sd_mounted = 0.0, 0.0, 0.0, False
            try:
                vfs = uos.statvfs("/sd")
                block_size = vfs[1] if vfs[1] > 0 else vfs[0]
                total_blocks, free_blocks = vfs[2], vfs[3]

                if total_blocks > 0:
                    total_bytes = total_blocks * block_size
                    free_bytes = free_blocks * block_size
                    used_bytes = total_bytes - free_bytes

                    total_mb = total_bytes / (1024 * 1024)
                    free_mb = free_bytes / (1024 * 1024)
                    used_pct = (used_bytes / total_bytes) * 100.0
                    sd_mounted = True
            except OSError:
                sd_mounted = False

            if sd_mounted:
                await self._send_chunk_str(
                    writer,
                    '<div class="card"><h2>SD Card Storage Metrics</h2><table>'
                    '<tr><th>Status</th><td><span class="badge-on">MOUNTED</span></td></tr>'
                    '<tr><th>Total Space</th><td>%.2f MB</td></tr>'
                    '<tr><th>Free Space</th><td>%.2f MB</td></tr>'
                    '<tr><th>Usage</th><td>'
                    '<div style="background:#333;border-radius:3px;overflow:hidden;width:100%%;max-width:200px;display:inline-block;vertical-align:middle;margin-right:8px;">'
                    '<div style="background:%s;width:%.1f%%;height:12px;"></div>'
                    '</div>%.1f%%'
                    '</td></tr>'
                    '</table></div>'
                    % (
                        total_mb,
                        free_mb,
                        "#dc3545" if used_pct > 90 else "#28a745",
                        used_pct,
                        used_pct,
                    ),
                    max_chunk=512,
                )
            else:
                await self._send_chunk_str(
                    writer,
                    '<div class="card"><h2>SD Card Storage Metrics</h2>'
                    '<p><span class="badge-off">UNMOUNTED / NOT FOUND</span></p></div>',
                    max_chunk=512,
                )

            # 7. SD Card Log Files List with Download Buttons
            gc.collect()
            try:
                raw_files = uos.listdir("/sd/logs")
                log_files = self.sort_log_files(raw_files)
            except OSError:
                log_files = []

            await self._send_chunk_str(writer, '<div class="card"><h2>SD Card Log Files</h2>', max_chunk=512)

            if log_files:
                await self._send_chunk_str(
                    writer,
                    "<table><tr><th>Filename</th><th>Size</th><th>Action</th></tr>",
                    max_chunk=512,
                )
                for file_name in log_files:
                    filepath = "/sd/logs/" + file_name
                    try:
                        file_stat = uos.stat(filepath)
                        size_bytes = file_stat[6]
                        size_str = (
                            "%d KB" % (size_bytes // 1024)
                            if size_bytes >= 1024
                            else "%d B" % size_bytes
                        )
                    except OSError:
                        size_str = "N/A"

                    await self._send_chunk_str(
                        writer,
                        '<tr><td><strong>%s</strong></td><td>%s</td>'
                        '<td><a class="btn-dl" href="/download_log?file=%s">Download</a></td></tr>'
                        % (file_name, size_str, file_name),
                        max_chunk=512,
                    )
                await self._send_chunk_str(writer, "</table></div>", max_chunk=512)
            else:
                await self._send_chunk_str(
                    writer,
                    "<p>No log files found in <code>/sd/logs</code>.</p></div>",
                    max_chunk=512,
                )

            # 8. Close Document Tags Properly
            await self._send_chunk_str(writer, "</body></html>", max_chunk=512)

            # Zero-length HTTP chunk to signal end of stream
            writer.write(b"0\r\n\r\n")
            await writer.drain()

        except OSError:
            pass
        finally:
            await self._close_stream_or_socket(writer)

    async def handle_log_download(self, request_path, reader, writer):
        """
        Handles endpoint '/download_log?file=log_1.json'.
        Streams the requested file directly from SD card in 512-byte chunks.
        """
        file_name = None

        # Parse query parameter ?file=<filename>
        if "?" in request_path:
            query_str = request_path.split("?", 1)[1]
            for param in query_str.split("&"):
                if param.startswith("file="):
                    file_name = param.split("=", 1)[1]
                    break

        # Security & Sanity Checks
        if not file_name or "/" in file_name or "\\" in file_name or ".." in file_name:
            await self._send_http_error(writer, 400, "Bad Request: Invalid file parameter")
            return

        filepath = "/sd/logs/" + file_name

        # Check file existence and size
        try:
            file_stat = uos.stat(filepath)
            file_size = file_stat[6]
        except OSError:
            await self._send_http_error(writer, 404, "File Not Found")
            return

        try:
            gc.collect()

            # Stream HTTP Headers
            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                'Content-Disposition: attachment; filename="%s"\r\n'
                "Transfer-Encoding: chunked\r\n"
                "Connection: close\r\n\r\n" % file_name
            )
            await self._send_chunk_raw(writer, header.encode("ascii"), max_chunk=512)

            # Stream file in 512-byte chunks directly from disk
            buf = bytearray(512)
            with open(filepath, "rb") as f:
                while True:
                    nread = f.readinto(buf)
                    if not nread or nread == 0:
                        break

                    # Slice to exact read length
                    chunk_view = memoryview(buf)[:nread]
                    await self._send_chunk_raw(writer, chunk_view, max_chunk=512)
                    gc.collect()

            # Zero-length HTTP chunk signals end of stream.
            if hasattr(writer, "send"):
                await self._send_raw(writer, b"0\r\n\r\n")
            else:
                writer.write(b"0\r\n\r\n")
                await writer.drain()

        except OSError:
            pass
        finally:
            await self._close_stream_or_socket(writer)


    async def _send_http_error(self, writer, code, message):
        """Helper to stream standard HTTP error responses with low memory overhead."""
        try:
            body = "<h1>%d %s</h1>" % (code, message)
            header = (
                "HTTP/1.1 %d %s\r\n"
                "Content-Type: text/html\r\n"
                "Content-Length: %d\r\n"
                "Connection: close\r\n\r\n"
                "%s" % (code, message, len(body), body)
            )
            writer.write(header.encode("ascii"))
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

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

    async def _send_json_dict_chunked(self, sock, obj):
        """Compatibility helper for the raw procedure dispatcher."""
        await self._stream_json_key_by_key(
            sock, obj, is_async_writer=False
        )

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

    # =========================================================================
    # RAW SOCKET POLLING & ZERO-ALLOCATION LOOP
    # =========================================================================

    async def _send_chunk_raw(self, writer, data: bytes, max_chunk: int = 512):
        """Send HTTP chunked data through either a uasyncio writer or raw socket."""
        if not data:
            return

        view = memoryview(data)
        total_len = len(view)
        offset = 0

        while offset < total_len:
            chunk_len = min(max_chunk, total_len - offset)
            sub_chunk = view[offset:offset + chunk_len]

            header = ("%X\r\n" % chunk_len).encode("ascii")

            if hasattr(writer, "send"):
                # Raw MicroPython socket path.
                await self._send_raw(writer, header)
                await self._send_raw(writer, sub_chunk)
                await self._send_raw(writer, b"\r\n")
            else:
                # Existing uasyncio Stream path.
                writer.write(header)
                writer.write(sub_chunk)
                writer.write(b"\r\n")
                await writer.drain()

            offset += chunk_len

    async def _send_chunk_str(self, writer, text: str, max_chunk: int = 512):
        """Encode and stream string data in controlled chunks."""
        if not text:
            return

        for i in range(0, len(text), max_chunk):
            slice_str = text[i:i + max_chunk]
            data = slice_str.encode("utf-8")
            await self._send_chunk_raw(writer, data, max_chunk=max_chunk)

    async def _close_stream_or_socket(self, obj):
        """Close either a raw socket or a uasyncio Stream."""
        if obj is None:
            return

        try:
            obj.close()
        except Exception:
            pass

        if not hasattr(obj, "send"):
            try:
                await obj.wait_closed()
            except Exception:
                pass

    async def _send_http_error(self, writer, code, message):
        """Send a small HTTP error through either raw socket or uasyncio writer."""
        try:
            body = ("<h1>%d %s</h1>" % (code, message)).encode("ascii")
            header = (
                "HTTP/1.1 %d %s\r\n"
                "Content-Type: text/html\r\n"
                "Content-Length: %d\r\n"
                "Connection: close\r\n\r\n"
                % (code, message, len(body))
            ).encode("ascii")

            if hasattr(writer, "send"):
                await self._send_raw(writer, header)
                await self._send_raw(writer, body)
            else:
                writer.write(header)
                writer.write(body)
                await writer.drain()
        except OSError:
            pass
        finally:
            await self._close_stream_or_socket(writer)

    async def _recv_into_buffer(self, sock, buf, offset):
        """
        Receive directly into an existing bytearray.

        This uses MicroPython's socket.readinto(), which is available in the
        v1.16 API and avoids the temporary bytes allocation from recv().
        """
        available = len(buf) - offset
        if available <= 0:
            return 0

        try:
            view = memoryview(buf)[offset:]
            nread = sock.readinto(view)

            if nread is None:
                return -1
            if nread == 0:
                return 0

            return nread

        except OSError as e:
            err = e.errno if hasattr(e, "errno") else (e.args[0] if e.args else None)

            if err in (errno.EAGAIN, errno.EWOULDBLOCK, 11):
                return -1

            raise

    def _accept_client(self):
        """Accept a client and register one fixed-size receive buffer."""
        try:
            client_sock, client_addr = self.server_sock.accept()

            if len(self.client_sockets) >= self.tcp_requests_max:
                try:
                    client_sock.send(self.RESPONSE_SERVER_BUSY)
                except OSError:
                    pass
                try:
                    client_sock.close()
                except OSError:
                    pass
                return

            client_sock.setblocking(False)

            sock_id = id(client_sock)
            buf = bytearray(self.BUFFER_SIZE)

            self.client_sockets[sock_id] = {
                "sock": client_sock,
                "addr": client_addr,
                "buf": buf,
                "length": 0,
                "start_time": utime.ticks_ms(),
                "task": None,
            }
            self.sock_map[sock_id] = client_sock

            self.poller.register(client_sock, uselect.POLLIN)

        except OSError:
            pass

    def _find_newline_and_length(self, buf, length):
        for i in range(length):
            if buf[i] == 10:  # '\n'
                return i + 1
        return -1

    async def _process_client_read(self, client_sock, client_info):
        """
        Read and dispatch one HTTP request using only the raw socket.

        No StreamReader/StreamWriter objects are created. A single 512-byte
        bytearray belongs to the client for the lifetime of the request.
        """
        try:
            gc.collect()

            buf = client_info["buf"]
            length = client_info["length"]

            # -------------------------------------------------------------
            # Read request line.
            # -------------------------------------------------------------
            while True:
                newline_pos = self._find_newline_and_length(buf, length)

                if newline_pos >= 0:
                    request_line_len = newline_pos
                    break

                if length >= len(buf):
                    await self._send_http_error(
                        client_sock, 400, "Request line too long"
                    )
                    return

                nread = await self._recv_into_buffer(
                    client_sock, buf, length
                )

                if nread == 0:
                    return

                if nread < 0:
                    await asyncio.sleep_ms(5)
                    continue

                length += nread
                client_info["length"] = length

            # Strip CR/LF in place logically, without decoding the whole buffer.
            request_line_end = request_line_len
            while request_line_end > 0 and buf[request_line_end - 1] in (10, 13):
                request_line_end -= 1

            first_space = -1
            second_space = -1

            for i in range(request_line_end):
                if buf[i] == 32:
                    if first_space < 0:
                        first_space = i
                    else:
                        second_space = i
                        break

            if first_space <= 0 or second_space <= first_space + 1:
                await self._send_http_error(
                    client_sock, 400, "Bad Request"
                )
                return

            method = bytes(buf[:first_space])
            request_path = bytes(buf[first_space + 1:second_space])

            try:
                method = method.decode("ascii")
                request_path = request_path.decode("utf-8")
            except (UnicodeError, ValueError):
                await self._send_http_error(
                    client_sock, 400, "Invalid Request"
                )
                return

            # -------------------------------------------------------------
            # Drain remaining HTTP headers.
            # -------------------------------------------------------------
            header_end = buf.find(b"\r\n\r\n", request_line_len)

            while header_end < 0:
                if length >= len(buf):
                    await self._send_http_error(
                        client_sock, 400, "Headers Too Large"
                    )
                    return

                nread = await self._recv_into_buffer(
                    client_sock, buf, length
                )

                if nread == 0:
                    return

                if nread < 0:
                    await asyncio.sleep_ms(5)
                    continue

                length += nread
                client_info["length"] = length

                header_end = buf.find(b"\r\n\r\n", request_line_len)

            # -------------------------------------------------------------
            # Route request. Existing handlers now support raw sockets
            # because _send_chunk_raw() detects socket.send().
            # -------------------------------------------------------------
            if method == "GET":
                if request_path.startswith("/download_log"):
                    await self.handle_log_download(
                        request_path, None, client_sock
                    )
                elif request_path in ("/", "/index.html"):
                    await self.send_control_web_page_raw(
                        None, client_sock
                    )
                else:
                    await self._send_http_error(
                        client_sock, 404, "Not Found"
                    )
            else:
                await self._send_http_error(
                    client_sock, 405, "Method Not Allowed"
                )

        except OSError:
            pass
        except Exception as e:
            try:
                await p.print("Client socket processing error:", e)
            except Exception:
                pass
        finally:
            self._close_client(client_sock)
            gc.collect()

    async def socket_poll_task(self):
        """Main non-blocking event loop driven by uselect.poll()."""
        while self.lan_connected and self.server_sock:
            try:
                events = self.poller.poll(0)

                for obj, event in events:
                    if obj == self.server_sock:
                        self._accept_client()
                        continue

                    sock_id = id(obj)
                    client_info = self.client_sockets.get(sock_id)

                    if client_info is None:
                        continue

                    if event & (uselect.POLLHUP | uselect.POLLERR):
                        self._close_client(obj)
                        continue

                    if event & uselect.POLLIN:
                        task = client_info.get("task")

                        if task is None:
                            try:
                                self.poller.unregister(obj)
                            except OSError:
                                pass

                            task = asyncio.create_task(
                                self._process_client_read(
                                    obj, client_info
                                )
                            )
                            client_info["task"] = task

                # ---------------------------------------------------------
                # Client timeout handling. Both accept time and timeout use
                # utime.ticks_ms(), avoiding the old time.time()/ticks mix.
                # ---------------------------------------------------------
                current_ms = utime.ticks_ms()
                timeout_ms = (
                    getattr(self, "CLIENT_TIMEOUT_S", 5) * 1000
                )

                for sock_id, client_info in list(
                    self.client_sockets.items()
                ):
                    if not isinstance(client_info, dict):
                        continue

                    sock = client_info.get("sock")
                    start_time = client_info.get(
                        "start_time", current_ms
                    )

                    if sock is not None and utime.ticks_diff(
                        current_ms, start_time
                    ) > timeout_ms:
                        self._close_client(sock)

            except Exception as e:
                try:
                    await p.print(
                        "Error in socket_poll_task:", e
                    )
                except Exception:
                    pass

            await asyncio.sleep_ms(
                getattr(self, "main_loop_yield_wait_ms", 20)
            )

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
        """Main manager loop for socket server lifecycle and connection health."""
        while True:
            try:
                wdt.feed()

                # 1. Ethernet Connection Management
                if not self.lan_connected:
                    await p.print("Attempting to reconnect Ethernet...")
                    try:
                        await self.connect_ethernet()
                    except (RuntimeError, Exception) as e:
                        await p.print("Ethernet connection failed. Retrying in 10s.")
                        # Sleep in small steps to keep feeding WDT during retry delay
                        for _ in range(10):
                            wdt.feed()
                            await asyncio.sleep(1)
                        continue

                # 2. Server Socket Initialization
                if self.server_sock is None:
                    await p.print("Attempting to start server...")
                    try:
                        gc.collect()
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        sock.setblocking(False)
                        sock.bind(("0.0.0.0", getattr(self, "port", 80)))
                        sock.listen(5)

                        self.server_sock = sock
                        self.poller.register(self.server_sock, uselect.POLLIN)

                        # Cancel old poll task cleanly if active
                        if hasattr(self, "poll_task") and self.poll_task:
                            self.poll_task.cancel()
                            self.poll_task = None

                        self.poll_task = asyncio.create_task(self.socket_poll_task())

                        # Start NTP background task if not already running
                        if not getattr(self, "ntp_task", None):
                            self.ntp_task = asyncio.create_task(self.sync_ntp_loop())

                        ip = self.lan.ifconfig()[0] if hasattr(self, "lan") else "0.0.0.0"
                        await p.print("Server running at http://%s:%d" % (ip, self.port))

                    except Exception as e:
                        await p.print("Failed to start server. Retrying in 10s.")
                        if "sock" in locals() and sock:
                            try:
                                sock.close()
                            except OSError:
                                pass
                        self.server_sock = None

                        for _ in range(10):
                            wdt.feed()
                            await asyncio.sleep(1)
                        continue

                # 3. Periodic LAN Connection Health Check
                current_ms = getattr(self, "millis", utime.ticks_ms)()
                last_check = getattr(self, "last_lan_check_ms", 0)

                if utime.ticks_diff(current_ms, last_check) >= 5000:
                    if hasattr(self, "lan") and not self.lan.isconnected():
                        await p.print("Ethernet disconnected.")
                        self.lan_connected = False

                        # Unregister and close main server socket
                        if self.server_sock:
                            try:
                                self.poller.unregister(self.server_sock)
                            except (KeyError, OSError):
                                pass
                            try:
                                self.server_sock.close()
                            except OSError:
                                pass
                            self.server_sock = None

                        # Cancel polling task on network drop
                        if getattr(self, "poll_task", None):
                            self.poll_task.cancel()
                            self.poll_task = None

                    self.last_lan_check_ms = current_ms

            except Exception as e:
                await p.print("AsyncWebServer main loop error:", e)

            wdt.feed()
            await asyncio.sleep_ms(getattr(self, "main_loop_yield_wait_ms", 50))

    def run(self):
        try:
            asyncio.run(self.start())
        except KeyboardInterrupt:
            print("Server stopped")