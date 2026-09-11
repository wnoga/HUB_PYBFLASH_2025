
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

async def stream_afe_status_html(writer, status_data):
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
                await asyncio.sleep_ms(0)

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