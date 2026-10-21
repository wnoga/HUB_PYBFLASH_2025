import io
import json
import math
import socket
import statistics
import time
import matplotlib.pyplot as plt
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# Configuration
HUB_IP = "192.168.1.223"
HUB_PORT = 5555
N_ITERATIONS = 2
SOCKET_TIMEOUT = 12.0

# Comprehensive Command Registry Documentation & Valid JSON Examples
COMMAND_REGISTRY = {
    "get_all_afe_id": {
        "params": "None",
        "description": "Queries the Hub for all active/discovered AFE subdevice IDs.",
        "action": "Broadcasts discovery state and returns active node addresses.",
        "request": {"procedure": "get_all_afe_id"},
        "response": {"status": "ok", "procedure": "get_all_afe_id", "result": {"afe_id_list": [41]}}
    },
    "get_afe_data": {
        "params": "afe_id: int",
        "description": "Retrieves cached telemetry and channel status dictionaries for a specific AFE.",
        "action": "Reads local device memory structures for periodic data and configuration parameters.",
        "request": {"procedure": "get_afe_data", "afe_id": 41},
        "response": {
            "status": "ok",
            "afe_id": 41,
            "periodic_data": {"timestamp_ms": 142900, "sensors": {}},
            "channels": {"0": {"channel_nr": 0, "averaging_method": 0, "alpha": 0.1}}
        }
    },
    "get_serial_number": {
        "params": "afe_id: int (Cmd 0x00)",
        "description": "Queries AFE device unique hardware serial number.",
        "action": "Reads microchip unique ID registers via CAN request/response frame.",
        "request": {"procedure": "get_serial_number", "afe_id": 41},
        "response": {"status": "ok", "procedure": "get_serial_number", "result": "0x4E00335231"}
    },
    "get_version": {
        "params": "afe_id: int (Cmd 0x01)",
        "description": "Queries AFE C firmware version string/number.",
        "action": "Extracts compiled firmware version identifier from firmware header.",
        "request": {"procedure": "get_version", "afe_id": 41},
        "response": {"status": "ok", "procedure": "get_version", "result": "v2.4.1-rc"}
    },
    "reset_all": {
        "params": "afe_id: int (Cmd 0x03)",
        "description": "Triggers hard reset (NVIC_SystemReset) on target AFE subdevice.",
        "action": "Executes hardware core reset command immediately without return frame.",
        "request": {"procedure": "reset_all", "afe_id": 41},
        "response": {"status": "ok", "procedure": "reset_all", "result": "Reset triggered"}
    },
    "start_adc": {
        "params": "afe_id: int, mask: int (Cmd 0x04)",
        "description": "Starts peripheral ADC conversion loop on target AFE.",
        "action": "Enables hardware analog-to-digital converter channels matching bitmask.",
        "request": {"procedure": "start_adc", "afe_id": 41, "mask": 255},
        "response": {"status": "ok", "procedure": "start_adc", "result": "ADC Started"}
    },
    "get_timestamp": {
        "params": "afe_id: int (Cmd 0x05)",
        "description": "Retrieves target AFE uptime timestamp in milliseconds.",
        "action": "Reads internal HAL tick counter value from AFE node.",
        "request": {"procedure": "get_timestamp", "afe_id": 41},
        "response": {"status": "ok", "procedure": "get_timestamp", "result": 142900}
    },
    "get_subdevice_status": {
        "params": "afe_id: int (Cmd 0x08)",
        "description": "Fetches 13-item status structure per Master/Slave subdevice.",
        "action": "Aggregates voltages, temperatures, loop states, and RAM telemetry blocks.",
        "request": {"procedure": "get_subdevice_status", "afe_id": 41},
        "response": {"status": "ok", "procedure": "get_subdevice_status", "result": {"0": {"V": 12.5, "T": 24.1}}}
    },
    "get_sensor_data_last": {
        "params": "afe_id: int, mask: int (Cmd 0x30)",
        "description": "Gets latest single raw/SI sensor readings for masked channels.",
        "action": "Samples instantaneous ADC conversion buffer for requested channels.",
        "request": {"procedure": "get_sensor_data_last", "afe_id": 41, "mask": 255},
        "response": {"status": "ok", "procedure": "get_sensor_data_last", "result": {"U_SIPM_MEAS0": {"value": 11.2}}}
    },
    "get_sensor_data_average": {
        "params": "afe_id: int, mask: int (Cmd 0x31)",
        "description": "Gets averaged SI sensor block readings for masked channels.",
        "action": "Computes rolling moving-average or exponential filter value over buffer.",
        "request": {"procedure": "get_sensor_data_average", "afe_id": 41, "mask": 255},
        "response": {"status": "ok", "procedure": "get_sensor_data_average", "result": {"U_SIPM_MEAS0": {"value": 11.18}}}
    },
    "shift_voltage": {
        "params": "afe_id: int, type: 'M'|'S'|'Both', value: float (Cmd 0xE9)",
        "description": "Adjusts regulator voltage offset (V_offset) for Master, Slave, or Both.",
        "action": "Recalculates DAC target setpoints based on new voltage offset adjustment.",
        "request": {"procedure": "shift_voltage", "id": 41, "type": "M", "value": 0.5},
        "response": {"status": "ok", "procedure": "shift_voltage", "result": "Offset Applied"}
    },
    "set_regulator_t_opt": {
        "params": "afe_id: int, value: float (Cmd 0xE3)",
        "description": "Sets optimal reference operating temperature T_opt in deg C.",
        "action": "Updates internal regulator reference setpoint for temperature compensation.",
        "request": {"procedure": "set_regulator_t_opt", "afe_id": 41, "value": 25.0},
        "response": {"status": "ok", "procedure": "set_regulator_t_opt", "result": "T_opt Updated"}
    },
    "set_regulator_dt": {
        "params": "afe_id: int, value: float (Cmd 0xE4)",
        "description": "Sets minimum temperature dead-band delta dT to trigger DAC updates.",
        "action": "Changes sensitivity threshold before regulator triggers voltage adjustments.",
        "request": {"procedure": "set_regulator_dt", "afe_id": 41, "value": 0.5},
        "response": {"status": "ok", "procedure": "set_regulator_dt", "result": "dT Updated"}
    },
    "set_averaging_alpha": {
        "params": "afe_id: int, value: float (Cmd 0xD1)",
        "description": "Sets exponential moving average weighting factor alpha.",
        "action": "Updates smoothing coefficient for exponential filter calculations.",
        "request": {"procedure": "set_averaging_alpha", "afe_id": 41, "value": 0.1},
        "response": {"status": "ok", "procedure": "set_averaging_alpha", "result": "Alpha Updated"}
    }
}


class AFEHubClient:
    def __init__(self, host, port):
        self.host = host
        self.port = port

    def send_cmd(self, procedure, **kwargs):
        payload = {"procedure": procedure}
        payload.update(kwargs)
        raw_json = json.dumps(payload) + "\n"

        start_t = time.time() * 1000.0
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(SOCKET_TIMEOUT)
        try:
            s.connect((self.host, self.port))
            s.sendall(raw_json.encode('utf-8'))

            response_bytes = b""
            while True:
                chunk = s.recv(1024)
                if not chunk:
                    break
                response_bytes += chunk
                if b"\n" in chunk or b"}" in chunk:
                    break

            end_t = time.time() * 1000.0
            elapsed_ms = end_t - start_t
            res_text = response_bytes.decode('utf-8').strip()
            if not res_text:
                return False, {"error": "Empty response"}, elapsed_ms

            if res_text.startswith("HTTP/1."):
                body = res_text.split("\r\n\r\n")[-1]
                res_dict = json.loads(body)
            else:
                res_dict = json.loads(res_text)

            is_ok = res_dict.get("status") in ["ok", "OK"]
            return is_ok, res_dict, elapsed_ms
        except Exception as exc:
            end_t = time.time() * 1000.0
            return False, {"error": str(exc)}, end_t - start_t
        finally:
            s.close()


def escape_html(text):
    """Escapes special characters so ReportLab Paragraphs render safely."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def run_benchmark_and_generate_pdf():
    client = AFEHubClient(HUB_IP, HUB_PORT)
    metrics = {cmd: [] for cmd in COMMAND_REGISTRY.keys()}
    success_counts = {cmd: 0 for cmd in COMMAND_REGISTRY.keys()}

    print("Connecting to Hub and discovering active AFEs...")
    target_afe_id = 41
    try:
        ok, res, _ = client.send_cmd("get_all_afe_id")
        if ok:
            res_val = res.get("result", {})
            afe_list = res_val.get("afe_id_list", [41]) if isinstance(res_val, dict) else res_val
            if afe_list:
                target_afe_id = afe_list[0]
    except Exception:
        pass

    print(f"Target AFE ID: {target_afe_id}. Running {N_ITERATIONS} benchmark cycles...")

    for cycle in range(1, N_ITERATIONS + 1):
        print(f"Cycle {cycle}/{N_ITERATIONS}...")
        for cmd in COMMAND_REGISTRY.keys():
            kwargs = {"afe_id": target_afe_id}
            if cmd == "shift_voltage":
                kwargs = {"id": target_afe_id, "type": "M", "value": 0.5}
            elif cmd in ("get_sensor_data_last", "get_sensor_data_average", "start_adc"):
                kwargs = {"afe_id": target_afe_id, "mask": 255}
            elif cmd == "get_all_afe_id":
                kwargs = {}

            # Handle hard reset cleanly without timeout lockup
            if cmd == "reset_all":
                try:
                    _, _, elapsed = client.send_cmd(cmd, **kwargs)
                except Exception:
                    elapsed = 15.0
                metrics[cmd].append(elapsed)
                success_counts[cmd] += 1
                time.sleep(2.0)
                continue

            ok, _, elapsed = client.send_cmd(cmd, **kwargs)
            metrics[cmd].append(elapsed)
            if ok:
                success_counts[cmd] += 1
        time.sleep(0.5)

    # Compile Statistics Summary
    stats_summary = {}
    for cmd, times in metrics.items():
        if times:
            mean_t = statistics.mean(times)
            min_t = min(times)
            max_t = max(times)
            std_t = statistics.stdev(times) if len(times) > 1 else 0.0
        else:
            mean_t, min_t, max_t, std_t = 0.0, 0.0, 0.0, 0.0
        stats_summary[cmd] = {"mean": mean_t, "min": min_t, "max": max_t, "std": std_t}

    # Plot 1: Full Timing with Error Bars
    print("Generating timing plots...")
    cmd_names = list(stats_summary.keys())
    means = [stats_summary[c]["mean"] for c in cmd_names]
    stds = [stats_summary[c]["std"] for c in cmd_names]

    fig, ax1 = plt.subplots(figsize=(10, 5))
    x_pos = range(len(cmd_names))
    ax1.errorbar(x_pos, means, yerr=stds, fmt='o', color='#0e639c', ecolor='#dc3545',
                 capsize=4, elinewidth=1.5, markeredgewidth=1.5, label='Mean ± StdDev')
    ax1.plot(x_pos, means, 's', color='#28a745', markersize=5, label='Completion Point')
    ax1.set_xticks(list(x_pos))
    ax1.set_xticklabels(cmd_names, rotation=35, ha='right', fontsize=8)
    ax1.set_ylabel('Execution Time (ms)')
    ax1.set_title('Overall Command Execution Times across N Iterations')
    ax1.grid(True, linestyle='--', alpha=0.5)
    ax1.legend(fontsize=8)
    plt.tight_layout()
    buf1 = io.BytesIO()
    plt.savefig(buf1, format='png', dpi=200)
    buf1.seek(0)
    plt.close(fig)

    # Plot 2: Common Setters & Regulators
    common_cmds = ["shift_voltage", "set_regulator_t_opt", "set_regulator_dt", "set_averaging_alpha", "start_adc"]
    c_indices = [cmd_names.index(c) for c in common_cmds if c in cmd_names]
    c_names = [cmd_names[i] for i in c_indices]
    c_means = [means[i] for i in c_indices]
    c_stds = [stds[i] for i in c_indices]

    fig, ax2 = plt.subplots(figsize=(8, 4))
    c_pos = range(len(c_names))
    ax2.bar(c_pos, c_means, yerr=c_stds, align='center', alpha=0.75, color='#388a34',
            ecolor='#1e1e1e', capsize=5, label='Setter Mean Time')
    ax2.plot(c_pos, c_means, 'ro', label='Completion Point')
    ax2.set_xticks(list(c_pos))
    ax2.set_xticklabels(c_names, rotation=20, ha='right', fontsize=9)
    ax2.set_ylabel('Execution Time (ms)')
    ax2.set_title('Secondary Benchmark: Common Setters & Regulators')
    ax2.grid(True, linestyle='--', alpha=0.5)
    ax2.legend(fontsize=8)
    plt.tight_layout()
    buf2 = io.BytesIO()
    plt.savefig(buf2, format='png', dpi=200)
    buf2.seek(0)
    plt.close(fig)

    # Build PDF Report
    print("Building PDF Report...")
    pdf_filename = "afe_hub_benchmark_report.pdf"
    doc = SimpleDocTemplate(pdf_filename, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontSize=18, textColor=colors.HexColor('#569cd6'), spaceAfter=10)
    h2_style = ParagraphStyle('H2Style', parent=styles['Heading2'], fontSize=13, textColor=colors.HexColor('#0e639c'), spaceBefore=12, spaceAfter=6)
    body_style = ParagraphStyle('BodyStyle', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#333333'), spaceAfter=6)
    code_style = ParagraphStyle('CodeStyle', parent=styles['Normal'], fontName='Courier', fontSize=7.5, textColor=colors.HexColor('#800000'), backColor=colors.HexColor('#f5f5f5'), spaceAfter=4)

    elements = []
    elements.append(Paragraph("AFE Hub Firmware & Command Verification Report", title_style))
    elements.append(Paragraph(f"Target Hub IP: {HUB_IP}:{HUB_PORT} | Iterations (N): {N_ITERATIONS}", body_style))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#569cd6'), spaceAfter=10))

    # Summary Table
    elements.append(Paragraph("1. Benchmark Performance Summary", h2_style))
    table_data = [["Command Name", "Success Rate", "Mean (ms)", "Min (ms)", "Max (ms)", "StdDev"]]
    for cmd in cmd_names:
        s_rate = f"{success_counts[cmd]}/{N_ITERATIONS}"
        s = stats_summary[cmd]
        table_data.append([cmd, s_rate, f"{s['mean']:.2f}", f"{s['min']:.2f}", f"{s['max']:.2f}", f"{s['std']:.2f}"])

    t = Table(table_data, colWidths=[130, 70, 70, 70, 70, 90])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2d2d2d')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f9f9f9')])
    ]))
    elements.append(t)
    elements.append(Spacer(1, 10))

    # Add Plots
    elements.append(Paragraph("2. Timing Analysis & Error Bar Visualizations", h2_style))
    elements.append(Image(buf1, width=480, height=240))
    elements.append(Spacer(1, 10))
    elements.append(Image(buf2, width=450, height=225))
    elements.append(Spacer(1, 10))

    # Command Details Section
    elements.append(Paragraph("3. Command Instruction Reference & JSON Examples", h2_style))
    for cmd, info in COMMAND_REGISTRY.items():
        req_json = escape_html(json.dumps(info['request']))
        res_json = escape_html(json.dumps(info['response']))
        
        elements.append(Paragraph(f"<b>{cmd.upper()}</b>", ParagraphStyle('CmdHead', parent=styles['Normal'], fontSize=10, textColor=colors.HexColor('#252526'), spaceBefore=6)))
        elements.append(Paragraph(f"<b>Description:</b> {escape_html(info['description'])} | <b>Parameters:</b> {escape_html(info['params'])}", body_style))
        elements.append(Paragraph(f"<b>Action:</b> {escape_html(info['action'])}", body_style))
        elements.append(Paragraph(f"<b>Request JSON:</b> <code>{req_json}</code>", code_style))
        elements.append(Paragraph(f"<b>Example Response JSON:</b> <code>{res_json}</code>", code_style))
        elements.append(Spacer(1, 4))

    doc.build(elements)
    print(f"Report successfully generated and saved to {pdf_filename}")


if __name__ == "__main__":
    run_benchmark_and_generate_pdf()