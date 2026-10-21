import gc
import uos as os
import utime as time
import uasyncio as asyncio
from stream_utilities import (
    send_raw,
    send_chunk_raw,
    send_chunk_str,
    stream_json_key_by_key,
    finish_chunked_stream,
)

DASHBOARD_CSS = (
    b"body{font-family:monospace,sans-serif;margin:15px;background:#1e1e1e;color:#d4d4d4;}"
    b".card{background:#252526;padding:15px;border-radius:6px;border:1px solid #3c3c3c;margin-bottom:15px;}"
    b"h2{color:#569cd6;margin-top:0;font-size:1.1em;border-bottom:1px solid #3c3c3c;padding-bottom:5px;display:flex;justify-content:space-between;align-items:center;}"
    b"table{width:100%%;border-collapse:collapse;margin-top:10px;}"
    b"td,th{padding:6px 10px;text-align:left;border-bottom:1px solid #333;font-size:0.85em;vertical-align:top;}"
    b"th{color:#9cdcfe;width:30%%;background-color:#2d2d2d;}"
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
    b".btn-action{background:#388a34;color:#fff;border:none;padding:5px 10px;border-radius:3px;cursor:pointer;font-size:0.85em;margin-right:5px;}"
    b".btn-action:hover{background:#46a742;}"
    b".btn-refresh{background:#0e639c;color:#fff;border:none;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:0.75em;}"
    b".btn-refresh:hover{background:#1177bb;}"
    b".control-group{margin-top:8px;display:flex;gap:5px;align-items:center;flex-wrap:wrap;font-size:0.85em;}"
    b"input[type=number],input[type=text],select{background:#1e1e1e;color:#d4d4d4;border:1px solid #3c3c3c;padding:3px;border-radius:3px;}"
    b"tr:nth-child(even){background:#333;}"
    b"tr:hover {background:#515;}"
)


async def stream_afe_status_html(writer, status_data):
    """Streams parsed live metrics (U_SIPM_MEAS0, U_SIPM_MEAS1, TEMP_LOCAL, TEMP_EXT) directly to HTML."""
    if not isinstance(status_data, dict):
        return

    last = status_data.get("last_data", {})
    if not last:
        return

    def get_val(key_name, int_idx):
        item = last.get(key_name, last.get(int_idx, last.get(str(int_idx), {})))
        if isinstance(item, dict):
            return item.get("value", 0.0)
        return getattr(item, "value", 0.0)

    u0 = get_val("U_SIPM_MEAS0", 2)
    u1 = get_val("U_SIPM_MEAS1", 3)
    temp_loc = get_val("TEMP_LOCAL", 7)
    temp_ext = get_val("TEMP_EXT", 6)

    await send_chunk_str(
        writer,
        '<div class="sys-row">'
        "<span><strong>U_SIPM_0:</strong> %.2f V</span>"
        "<span><strong>U_SIPM_1:</strong> %.2f V</span>"
        "<span><strong>Local Temp:</strong> %.2f °C</span>"
        "<span><strong>Ext Temp:</strong> %.2f °C</span>"
        "</div>" % (u0, u1, temp_loc, temp_ext),
        max_chunk=512,
    )


async def send_control_web_page_raw(server_inst, writer):
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
        await send_chunk_raw(writer, header.encode("ascii"), max_chunk=512)
        
        # 2. Document Shell & Static CSS + JS helper for procedures
        await send_chunk_raw(
            writer,
            b'<!DOCTYPE html><html><head>'
            b'<meta name="viewport" content="width=device-width, initial-scale=1">'
            b'<title>HUB - Status Dashboard</title><style>',
            max_chunk=512,
        )

        await send_chunk_raw(writer, DASHBOARD_CSS, max_chunk=512)

        await send_chunk_raw(
            writer,
            b'</style><script>'
            b'function sendProcedure(proc, data) {'
            b'  fetch("/", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(Object.assign({procedure: proc}, data))})'
            b'  .then(res => res.json()).then(out => { alert("Result: " + JSON.stringify(out)); location.reload(); })'
            b'  .catch(err => alert("Error: " + err));'
            b'}'
            b'function refreshSection(sectionName) {'
            b'  location.reload();'
            b'}'
            b'function setVoltageOffset(afeId) {'
            b'  const val = parseFloat(document.getElementById("offset_val_" + afeId).value);'
            b'  const target = document.getElementById("offset_target_" + afeId).value;'
            b'  sendProcedure("shift_voltage", {id: afeId, type: target, value: val});'
            b'}'
            b'function setCanBurstDelay(afeId) {'
            b'  const delayVal = parseInt(document.getElementById("burst_delay_" + afeId).value);'
            b'  sendProcedure("set_can_burst_delay", {afe_id: afeId, delay: delayVal});'
            b'}'
            b'function testCommand(afeId, procName, valInputId, extraInputId = null) {'
            b'  let data = {afe_id: afeId};'
            b'  if (valInputId) {'
            b'    data.value = document.getElementById(valInputId + "_" + afeId).value;'
            b'  }'
            b'  if (extraInputId) {'
            b'    data.mask = document.getElementById(extraInputId + "_" + afeId).value;'
            b'  }'
            b'  sendProcedure(procName, data);'
            b'}'
            b'</script></head><body>',
            max_chunk=512,
        )

        # 3. System Status
        gc.collect()
        current_ms = getattr(server_inst, "millis", time.ticks_ms)()
        start_time = getattr(server_inst.hub, "discovery_start_time", 0)
        timeout_ms = getattr(server_inst.hub, "discovery_timeout_ms", 0)

        try:
            discovery_timed_out = time.ticks_diff(current_ms, start_time) >= timeout_ms
        except Exception:
            discovery_timed_out = "N/A"

        afe_manage_active = getattr(server_inst.hub, "afe_manage_active", False)
        rx_process_active = getattr(server_inst.hub, "rx_process_active", False)

        await send_chunk_str(
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

        # 4. AFE Devices from self.hub.afes
        afes_data = getattr(server_inst.hub, "afes", {})
        afe_devices = afes_data.values() if isinstance(afes_data, dict) else afes_data

        if afe_devices:
            for afe in afe_devices:
                gc.collect()
                afe_id = getattr(afe, "afe_id", getattr(afe, "device_id", "Unknown"))
                config = getattr(afe, "configuration", {})

                is_configured = getattr(afe, "is_configured", False)
                request_config = getattr(afe, "request_configuration", False)
                burst_delay = getattr(afe, "can_msg_burst_delay_ms", 100)

                # 1. Card Header & Basic Flags
                await send_chunk_str(
                    writer,
                    '<div class="card"><h2>AFE Device: %s</h2><table>'
                    "<tr><th>Configured</th><td>%s</td></tr>"
                    "<tr><th>Config Requested</th><td>%s</td></tr>"
                    "<tr><th>CAN Burst Delay</th><td>%d ms</td></tr>"
                    % (
                        str(afe_id),
                        '<span class="badge-on">YES</span>' if is_configured else '<span class="badge-off">NO</span>',
                        '<span class="badge-on">YES</span>' if request_config else '<span class="badge-off">NO</span>',
                        burst_delay,
                    ),
                    max_chunk=512,
                )

                # 2. Quick Actions Row
                await send_chunk_str(
                    writer,
                    '<tr><th>Actions</th><td>'
                    '<button class="btn-action" onclick="sendProcedure(\'get_afe_data\', {afe_id: %s})">Get AFE Data</button>'
                    '<button class="btn-action" onclick="sendProcedure(\'shift_voltage\', {id: %s, type: \'M\', value: 1.0})">Shift +1.0V</button>'
                    '</td></tr>'
                    % (str(afe_id), str(afe_id)),
                    max_chunk=512,
                )

                # 3. Voltage Offset & CAN Burst Delay Controls
                await send_chunk_str(
                    writer,
                    '<tr><th>Voltage Offset Control</th><td>'
                    '<div class="control-group">'
                    '<select id="offset_target_%s"><option value="M">Master</option><option value="S">Slave</option><option value="Both">Both</option></select>'
                    '<input type="number" id="offset_val_%s" value="0.0" step="0.1">'
                    '<button class="btn-action" onclick="setVoltageOffset(%s)">Apply Offset</button>'
                    '</div></td></tr>'
                    '<tr><th>CAN Burst Delay Control</th><td>'
                    '<div class="control-group">'
                    '<input type="number" id="burst_delay_%s" value="%d" min="0" max="5000" style="width:80px;"> ms'
                    '<button class="btn-action" onclick="setCanBurstDelay(%s)">Set Burst Delay</button>'
                    '</div></td></tr>'
                    % (str(afe_id), str(afe_id), str(afe_id), str(afe_id), burst_delay, str(afe_id)),
                    max_chunk=512,
                )

                # 4. Command Testing Panel (Basic Getters including get_afe_data)
                await send_chunk_str(
                    writer,
                    '<tr><th>Command Test Panel</th><td>'
                    '<div class="control-group">'
                    '<button class="btn-action" onclick="testCommand(%s, \'get_afe_data\', null)">Get AFE Data</button>'
                    '<button class="btn-action" onclick="testCommand(%s, \'get_serial_number\', null)">Get Serial</button>'
                    '<button class="btn-action" onclick="testCommand(%s, \'get_version\', null)">Get Version</button>'
                    '<button class="btn-action" onclick="testCommand(%s, \'start_adc\', null)">Start ADC</button>'
                    '<button class="btn-action" onclick="testCommand(%s, \'get_subdevice_status\', null)">Get Status</button>'
                    '</div>'
                    % (str(afe_id), str(afe_id), str(afe_id), str(afe_id), str(afe_id)),
                    max_chunk=512,
                )

                # 5. Command Testing Panel (Sensor Data Masks)
                await send_chunk_str(
                    writer,
                    '<div class="control-group">'
                    'Mask: <input type="number" id="mask_input_%s" value="255" style="width:50px;">'
                    '<button class="btn-action" onclick="testCommand(%s, \'get_sensor_data_last\', null, \'mask_input\')">Get Last Si</button>'
                    '<button class="btn-action" onclick="testCommand(%s, \'get_sensor_data_average\', null, \'mask_input\')">Get Avg Si</button>'
                    '</div>'
                    % (str(afe_id), str(afe_id), str(afe_id)),
                    max_chunk=512,
                )

                # 6. Command Testing Panel (Regulators & Values) & Live Telemetry Intro
                await send_chunk_str(
                    writer,
                    '<div class="control-group">'
                    'Val: <input type="number" id="cmd_val_%s" value="0" style="width:70px;" step="any">'
                    '<button class="btn-action" onclick="testCommand(%s, \'set_regulator_t_opt\', \'cmd_val\')">Set T_opt</button>'
                    '<button class="btn-action" onclick="testCommand(%s, \'set_regulator_dt\', \'cmd_val\')">Set dT</button>'
                    '<button class="btn-action" onclick="testCommand(%s, \'set_averaging_alpha\', \'cmd_val\')">Set Alpha</button>'
                    '</div>'
                    '</td></tr>'
                    '<tr><th>Live Telemetry <button class="btn-refresh" onclick="refreshSection(\'live\')">Refresh</button></th><td>'
                    % (str(afe_id), str(afe_id), str(afe_id), str(afe_id)),
                    max_chunk=512,
                )

                # Stream parsed main telemetry row
                periodic_data = getattr(afe, "periodic_data", {})
                if periodic_data:
                    if hasattr(periodic_data, "to_dict"):
                        p_dict = periodic_data.to_dict()
                    elif hasattr(periodic_data, "__dict__"):
                        p_dict = periodic_data.__dict__
                    else:
                        p_dict = periodic_data if isinstance(periodic_data, dict) else {}
                    await stream_afe_status_html(writer, p_dict)

                await send_chunk_str(writer, '</td></tr><tr><th>Configuration (JSON)</th><td><div class="scroll-box"><pre>', max_chunk=512)

                if isinstance(config, (dict, list)):
                    await stream_json_key_by_key(writer, config, is_async_writer=True)
                else:
                    await send_chunk_str(writer, str(config), max_chunk=512)

                await send_chunk_str(writer, "</pre></div></td></tr>", max_chunk=512)
                
                # Periodic / Latest Status Data Container
                if hasattr(periodic_data, "to_dict"):
                    raw_status = periodic_data.to_dict()
                elif hasattr(periodic_data, "__dict__"):
                    raw_status = periodic_data.__dict__
                else:
                    raw_status = periodic_data if isinstance(periodic_data, dict) else {}

                await send_chunk_str(
                    writer,
                    '<tr><th>Periodic Sensor Data <button class="btn-refresh" onclick="refreshSection(\'periodic\')">Refresh</button></th><td><div class="scroll-box"><pre>',
                    max_chunk=512,
                )

                if isinstance(raw_status, (dict, list)):
                    await stream_json_key_by_key(writer, raw_status, is_async_writer=True)
                else:
                    await send_chunk_str(writer, str(raw_status), max_chunk=512)

                await send_chunk_str(writer, "</pre></div></td></tr>", max_chunk=512)

                # Channels Telemetry
                channels = getattr(afe, "channels", {})
                if channels:
                    await send_chunk_str(writer, "<tr><th>Channels Telemetry</th><td>", max_chunk=512)
                    await stream_afe_channels_html(writer, channels)
                    await send_chunk_str(writer, "</td></tr>", max_chunk=512)
                
                # Subdevice / Latest Status Data
                latest_status = getattr(afe, "latest_status", {})
                if latest_status:
                    await send_chunk_str(
                        writer,
                        '<tr><th>Subdevice Status (M/S) <button class="btn-refresh" onclick="refreshSection(\'substatus\')">Refresh</button></th><td><div class="scroll-box"><pre>',
                        max_chunk=512,
                    )
                    if isinstance(latest_status, (dict, list)):
                        await stream_json_key_by_key(writer, latest_status, is_async_writer=True)
                    else:
                        await send_chunk_str(writer, str(latest_status), max_chunk=512)
                    await send_chunk_str(writer, "</pre></div></td></tr>", max_chunk=512)

                await send_chunk_str(writer, "</table></div>", max_chunk=512)
        else:
            await send_chunk_str(
                writer,
                '<div class="card"><h2>AFE Devices</h2><p>No AFE devices detected.</p></div>',
                max_chunk=512,
            )

        # 5. Server Metrics
        gc.collect()
        free_ram = gc.mem_free()
        uptime_s = int(time.time())
        active_clients = len(getattr(server_inst, "client_sockets", []))

        await send_chunk_str(
            writer,
            '<div class="card"><h2>Server Metrics <button class="btn-refresh" onclick="refreshSection(\'server\')">Refresh</button></h2><table>'
            "<tr><th>Free RAM</th><td>%d bytes</td></tr>"
            "<tr><th>Active Sockets</th><td>%d / %d</td></tr>"
            "<tr><th>Uptime</th><td>%d s</td></tr>"
            "</table></div>"
            % (free_ram, active_clients, getattr(server_inst, "tcp_requests_max", 0), uptime_s),
            max_chunk=512,
        )

        # 6. SD Card Storage Metrics
        gc.collect()
        total_mb, free_mb, used_pct, sd_mounted = 0.0, 0.0, 0.0, False
        try:
            vfs = os.statvfs("/sd")
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
            await send_chunk_str(
                writer,
                '<div class="card"><h2>SD Card Storage Metrics <button class="btn-refresh" onclick="refreshSection(\'sd\')">Refresh</button></h2><table>'
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
            await send_chunk_str(
                writer,
                '<div class="card"><h2>SD Card Storage Metrics <button class="btn-refresh" onclick="refreshSection(\'sd\')">Refresh</button></h2>'
                '<p><span class="badge-off">UNMOUNTED / NOT FOUND</span></p></div>',
                max_chunk=512,
            )

        # 7. SD Card Log Files List with Download Buttons
        gc.collect()
        try:
            raw_files = os.listdir("/sd/logs")
            sort_func = getattr(server_inst, "sort_log_files", sorted)
            log_files = sort_func(raw_files)
        except OSError:
            log_files = []

        await send_chunk_str(writer, '<div class="card"><h2>SD Card Log Files</h2>', max_chunk=512)

        if log_files:
            await send_chunk_str(
                writer,
                "<table><tr><th>Filename</th><th>Size</th><th>Action</th></tr>",
                max_chunk=512,
            )
            for file_name in log_files:
                filepath = "/sd/logs/" + file_name
                try:
                    file_stat = os.stat(filepath)
                    size_bytes = file_stat[6]
                    size_str = (
                        "%d KB" % (size_bytes // 1024)
                        if size_bytes >= 1024
                        else "%d B" % size_bytes
                    )
                except OSError:
                    size_str = "N/A"

                await send_chunk_str(
                    writer,
                    '<tr><td><strong>%s</strong></td><td>%s</td>'
                    '<td><a class="btn-dl" href="/download_log?file=%s">Download</a></td></tr>'
                    % (file_name, size_str, file_name),
                    max_chunk=512,
                )
            await send_chunk_str(writer, "</table></div>", max_chunk=512)
        else:
            await send_chunk_str(
                writer,
                "<p>No log files found in <code>/sd/logs</code>.</p></div>",
                max_chunk=512,
            )

        # 8. Close Document Tags Properly
        await send_chunk_str(writer, "</body></html>", max_chunk=512)

        # Terminate chunked HTTP response safely
        await finish_chunked_stream(writer)

    except OSError:
        pass


async def stream_afe_channels_html(writer, channels_data):
    """Streams channel configurations and parameters for the AFE device."""
    if not isinstance(channels_data, dict):
        return

    await send_chunk_str(writer, '<div class="sipm-grid">', max_chunk=512)
    for uch, ch_dict in channels_data.items():
        if not ch_dict:
            continue
        await send_chunk_str(
            writer,
            '<div class="sipm-chan"><h3>Channel %s</h3>' % str(uch),
            max_chunk=512,
        )
        for k, v in ch_dict.items():
            await send_chunk_str(
                writer,
                '<div class="metric"><span>%s:</span><span class="metric-val">%s</span></div>' % (str(k), str(v)),
                max_chunk=512,
            )
        await send_chunk_str(writer, "</div>", max_chunk=512)
    await send_chunk_str(writer, "</div>", max_chunk=512)


async def handle_log_download(server_inst, request_path, writer):
    file_name = None

    if "?" in request_path:
        query_str = request_path.split("?", 1)[1]
        for param in query_str.split("&"):
            if param.startswith("file="):
                file_name = param.split("=", 1)[1]
                break

    if not file_name or "/" in file_name or "\\" in file_name or ".." in file_name:
        writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\nInvalid Parameter")
        await writer.drain()
        return

    filepath = "/sd/logs/" + file_name

    try:
        file_stat = os.stat(filepath)
        file_size = file_stat[6]
    except OSError:
        writer.write(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\nFile Not Found")
        await writer.drain()
        return

    try:
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/octet-stream\r\n"
            'Content-Disposition: attachment; filename="%s"\r\n'
            "Content-Length: %d\r\n"
            "Connection: close\r\n\r\n" % (file_name, file_size)
        ).encode("ascii")
        
        writer.write(header)
        await writer.drain()

        buf = bytearray(4096)
        mv = memoryview(buf)
        
        sleep_ms = getattr(server_inst, "log_download_sleep_ms", 0)

        with open(filepath, "rb") as f:
            while True:
                nread = f.readinto(buf)
                if not nread:
                    break

                writer.write(mv[:nread])
                await writer.drain()
                
                if sleep_ms > 0:
                    await asyncio.sleep_ms(sleep_ms)

    except OSError:
        pass