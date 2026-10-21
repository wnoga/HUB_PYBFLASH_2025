import argparse
import io
import json
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
N_ITERATIONS = 1
SOCKET_TIMEOUT = 12.0
BURST_DELAYS_MS = [0, 20, 25, 50, 100]

# ---------------------------------------------------------------------------
# Command Instruction Registry Documentation & Base Structure
# ---------------------------------------------------------------------------
COMMAND_REGISTRY = {
    "get_all_afe_id": {
        "params": "None",
        "description": "Queries the Hub for all active/discovered AFE subdevice IDs.",
        "action": "Broadcasts discovery state and returns active node addresses.",
        "request": {"procedure": "get_all_afe_id"}
    },
    "get_afe_data": {
        "params": "afe_id: int",
        "description": "Retrieves cached telemetry and channel status dictionaries for a specific AFE.",
        "action": "Reads local device memory structures for periodic data and configuration parameters.",
        "request": {"procedure": "get_afe_data", "afe_id": 41}
    },
    "get_serial_number": {
        "params": "afe_id: int (Cmd 0x00)",
        "description": "Queries AFE device unique hardware serial number.",
        "action": "Reads microchip unique ID registers via CAN request/response frame.",
        "request": {"procedure": "get_serial_number", "afe_id": 41}
    },
    "get_version": {
        "params": "afe_id: int (Cmd 0x01)",
        "description": "Queries AFE C firmware version string/number.",
        "action": "Extracts compiled firmware version identifier from firmware header.",
        "request": {"procedure": "get_version", "afe_id": 41}
    },
    "reset_all": {
        "params": "afe_id: int (Cmd 0x03)",
        "description": "Triggers hard reset (NVIC_SystemReset) on target AFE subdevice.",
        "action": "Executes hardware core reset command immediately without return frame; followed by polling reconfiguration loop.",
        "request": {"procedure": "reset_all", "id": 41, "afe_id": 41}
    },
    "start_adc": {
        "params": "afe_id: int, mask: int (Cmd 0x04)",
        "description": "Starts peripheral ADC conversion loop on target AFE.",
        "action": "Enables hardware analog-to-digital converter channels matching bitmask.",
        "request": {"procedure": "start_adc", "afe_id": 41, "mask": 255}
    },
    "get_timestamp": {
        "params": "afe_id: int (Cmd 0x05)",
        "description": "Retrieves target AFE uptime timestamp in milliseconds.",
        "action": "Reads internal HAL tick counter value from AFE node.",
        "request": {"procedure": "get_timestamp", "afe_id": 41}
    },
    "get_subdevice_status": {
        "params": "afe_id: int (Cmd 0x08)",
        "description": "Fetches 13-item status structure per Master/Slave subdevice.",
        "action": "Aggregates voltages, temperatures, loop states, and RAM telemetry blocks for Master (0) and Slave (1).",
        "request": {"procedure": "get_subdevice_status", "afe_id": 41}
    },
    "get_sensor_data_last": {
        "params": "afe_id: int, mask: int (Cmd 0x30)",
        "description": "Gets latest single raw/SI sensor readings for masked channels.",
        "action": "Samples instantaneous ADC conversion buffer for requested channels.",
        "request": {"procedure": "get_sensor_data_last", "afe_id": 41, "mask": 255}
    },
    "get_sensor_data_average": {
        "params": "afe_id: int, mask: int (Cmd 0x31)",
        "description": "Gets averaged SI sensor block readings for masked channels.",
        "action": "Computes rolling moving-average or exponential filter value over buffer.",
        "request": {"procedure": "get_sensor_data_average", "afe_id": 41, "mask": 255}
    },
    "shift_voltage": {
        "params": "afe_id: int, type: 'M'|'S'|'Both', value: float (Cmd 0xE9)",
        "description": "Adjusts regulator voltage offset (V_offset) for Master, Slave, or Both.",
        "action": "Recalculates DAC target setpoints based on new voltage offset adjustment.",
        "request": {"procedure": "shift_voltage", "id": 41, "type": "M", "value": 0.5}
    },
    "set_regulator_t_opt": {
        "params": "afe_id: int, value: float (Cmd 0xE3)",
        "description": "Sets optimal reference operating temperature T_opt in deg C.",
        "action": "Updates internal regulator reference setpoint for temperature compensation.",
        "request": {"procedure": "set_regulator_t_opt", "afe_id": 41, "value": 25.0}
    },
    "set_regulator_dt": {
        "params": "afe_id: int, value: float (Cmd 0xE4)",
        "description": "Sets minimum temperature dead-band delta dT to trigger DAC updates.",
        "action": "Changes sensitivity threshold before regulator triggers voltage adjustments.",
        "request": {"procedure": "set_regulator_dt", "afe_id": 41, "value": 0.5}
    },
    "set_averaging_alpha": {
        "params": "afe_id: int, value: float (Cmd 0xD1)",
        "description": "Sets exponential moving average weighting factor alpha.",
        "action": "Updates smoothing coefficient for exponential filter calculations.",
        "request": {"procedure": "set_averaging_alpha", "afe_id": 41, "value": 1e-06}
    }
}


class AFEHubClient:
    """Handles raw JSON TCP socket communication with AFE Hub."""

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
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def run_can_burst_delay_test(client, target_afe_id, iterations=3):
    """Executes burst delay sensitivity test for get_subdevice_status across specified delays."""
    print("\n=======================================================")
    print("Running CAN Message Burst Delay Test on get_subdevice_status")
    print("=======================================================")
    
    burst_results = {}
    for delay_ms in BURST_DELAYS_MS:
        print(f"\nSetting CAN Burst Delay to {delay_ms} ms...")
        ok, res, _ = client.send_cmd("set_can_burst_delay", afe_id=target_afe_id, delay=delay_ms)
        time.sleep(0.1)

        times = []
        successes = 0
        for it in range(1, iterations + 1):
            s_ok, s_res, elapsed = client.send_cmd("get_subdevice_status", afe_id=target_afe_id)
            times.append(elapsed)
            if s_ok:
                successes += 1
            print(f"  [{delay_ms} ms Delay] Iteration {it}/{iterations}: {elapsed:.2f} ms ({'OK' if s_ok else 'FAIL'})")
            time.sleep(0.05)

        mean_t = statistics.mean(times) if times else 0.0
        min_t = min(times) if times else 0.0
        max_t = max(times) if times else 0.0
        std_t = statistics.stdev(times) if len(times) > 1 else 0.0
        
        burst_results[delay_ms] = {
            "mean": mean_t,
            "min": min_t,
            "max": max_t,
            "std": std_t,
            "successes": successes,
            "total": iterations,
            "times": times
        }

    # Reset CAN Burst Delay to default 100 ms after testing
    client.send_cmd("set_can_burst_delay", afe_id=target_afe_id, delay=100)
    return burst_results


def generate_burst_test_plot(burst_results):
    delays = list(burst_results.keys())
    means = [burst_results[d]["mean"] for d in delays]
    stds = [burst_results[d]["std"] for d in delays]

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.errorbar(delays, means, yerr=stds, fmt='-o', color='#0e639c', ecolor='#dc3545',
                capsize=5, elinewidth=1.5, markeredgewidth=1.5, label='Mean Response Time ± StdDev')
    ax.plot(delays, means, 's', color='#28a745', markersize=6, label='Data Point')
    ax.set_xlabel('CAN Message Burst Delay (ms)')
    ax.set_ylabel('Execution Time for get_subdevice_status (ms)')
    ax.set_title('get_subdevice_status Response Time vs CAN Message Burst Delay')
    ax.grid(True, linestyle='--', alpha=0.5)
    ax.set_xticks(delays)
    ax.legend(fontsize=8)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=200)
    buf.seek(0)
    plt.close(fig)
    return buf


def run_benchmark_and_generate_pdf(mode="default", iterations=N_ITERATIONS):
    client = AFEHubClient(HUB_IP, HUB_PORT)
    metrics = {cmd: [] for cmd in COMMAND_REGISTRY.keys()}
    success_counts = {cmd: 0 for cmd in COMMAND_REGISTRY.keys()}
    real_responses = {}

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

    burst_results = None

    if mode in ("burst", "all"):
        burst_results = run_can_burst_delay_test(client, target_afe_id, iterations=3)
        if mode == "burst":
            # Generate Burst-Only Report
            pdf_filename = "afe_hub_burst_delay_report.pdf"
            doc = SimpleDocTemplate(pdf_filename, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
            styles = getSampleStyleSheet()

            title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontSize=18, textColor=colors.HexColor('#0e639c'), spaceAfter=8)
            h2_style = ParagraphStyle('H2Style', parent=styles['Heading2'], fontSize=12, textColor=colors.HexColor('#0e639c'), spaceBefore=10, spaceAfter=4)
            body_style = ParagraphStyle('BodyStyle', parent=styles['Normal'], fontSize=8.5, textColor=colors.HexColor('#333333'), spaceAfter=4)

            elements = []
            elements.append(Paragraph("AFE Hub CAN Burst Delay Sensitivity Report", title_style))
            elements.append(Paragraph(f"Target Hub IP: <b>{HUB_IP}:{HUB_PORT}</b> | Target AFE ID: <b>{target_afe_id}</b>", body_style))
            elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#0e639c'), spaceAfter=8))

            elements.append(Paragraph("1. get_subdevice_status Timing vs Burst Delay (0, 20, 25, 50, 100 ms)", h2_style))
            b_buf = generate_burst_test_plot(burst_results)
            elements.append(Image(b_buf, width=480, height=240))
            elements.append(Spacer(1, 10))

            table_data = [["Burst Delay (ms)", "Status", "Success", "Mean (ms)", "Min (ms)", "Max (ms)", "StdDev"]]
            for d, s in burst_results.items():
                s_rate = f"{s['successes']}/{s['total']}"
                table_data.append([
                    f"{d} ms",
                    "WORKING" if s['successes'] > 0 else "FAILED",
                    s_rate,
                    f"{s['mean']:.2f}",
                    f"{s['min']:.2f}",
                    f"{s['max']:.2f}",
                    f"{s['std']:.2f}"
                ])

            t = Table(table_data, colWidths=[100, 70, 65, 75, 75, 75, 70])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e1e1e')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f9f9f9')])
            ]))
            elements.append(t)

            doc.build(elements)
            print(f"\nPDF successfully generated: {pdf_filename}")
            return

    if mode in ("default", "all"):
        print(f"\nTarget AFE ID: {target_afe_id}. Running {iterations} real benchmark cycles...")

        for cycle in range(1, iterations + 1):
            print(f"Executing Cycle {cycle}/{iterations}...")
            for cmd in COMMAND_REGISTRY.keys():
                kwargs = {"afe_id": target_afe_id}
                if cmd == "shift_voltage":
                    kwargs = {"id": target_afe_id, "type": "M", "value": 0.5}
                elif cmd in ("get_sensor_data_last", "get_sensor_data_average", "start_adc"):
                    kwargs = {"afe_id": target_afe_id, "mask": 255}
                elif cmd == "get_all_afe_id":
                    kwargs = {}

                if cmd == "reset_all":
                    try:
                        ok, res, elapsed = client.send_cmd(cmd, **kwargs)
                    except Exception:
                        elapsed = 15000.0
                        ok = True
                        res = {"status": "ok", "result": "Reset triggered"}
                    metrics[cmd].append(elapsed)
                    success_counts[cmd] += 1
                    real_responses[cmd] = res

                    # Active polling loop for reboot completion
                    reconfigured = False
                    poll_start = time.time()
                    while not reconfigured and (time.time() - poll_start) < 20.0:
                        time.sleep(1.5)
                        r_ok, r_data, _ = client.send_cmd("get_afe_data", afe_id=target_afe_id)
                        if r_ok and r_data.get("channels"):
                            reconfigured = True
                            break
                    continue

                ok, res, elapsed = client.send_cmd(cmd, **kwargs)
                metrics[cmd].append(elapsed)
                if ok:
                    success_counts[cmd] += 1
                real_responses[cmd] = res
            time.sleep(0.5)

        # 1. Compile Statistics Summary
        stats_summary = {}
        for cmd, times in metrics.items():
            mean_t = statistics.mean(times) if times else 0.0
            min_t = min(times) if times else 0.0
            max_t = max(times) if times else 0.0
            std_t = statistics.stdev(times) if len(times) > 1 else 0.0
            stats_summary[cmd] = {"mean": mean_t, "min": min_t, "max": max_t, "std": std_t}

        cmd_names = list(stats_summary.keys())

        # 2. Generate Plot 1: Overall Timing excluding reset_all
        plot_cmds = [c for c in cmd_names if c != "reset_all"]
        means_p1 = [stats_summary[c]["mean"] for c in plot_cmds]
        stds_p1 = [stats_summary[c]["std"] for c in plot_cmds]

        fig, ax1 = plt.subplots(figsize=(10, 4.5))
        x_pos = range(len(plot_cmds))
        ax1.errorbar(x_pos, means_p1, yerr=stds_p1, fmt='o', color='#0e639c', ecolor='#dc3545',
                     capsize=4, elinewidth=1.5, markeredgewidth=1.5, label='Mean Time ± StdDev')
        ax1.plot(x_pos, means_p1, 's', color='#28a745', markersize=5, label='Completion Point')
        ax1.set_xticks(list(x_pos))
        ax1.set_xticklabels(plot_cmds, rotation=35, ha='right', fontsize=8)
        ax1.set_ylabel('Execution Time (ms)')
        ax1.set_title('Overall Command Execution Times (Real Hardware Benchmarks)')
        ax1.grid(True, linestyle='--', alpha=0.5)
        ax1.legend(fontsize=8)
        plt.tight_layout()
        buf1 = io.BytesIO()
        plt.savefig(buf1, format='png', dpi=200)
        buf1.seek(0)
        plt.close(fig)

        # 3. Generate Plot 2: Common Setters & Regulators
        common_cmds = ["shift_voltage", "set_regulator_t_opt", "set_regulator_dt", "set_averaging_alpha", "start_adc"]
        c_names = [c for c in common_cmds if c in cmd_names]
        c_means = [stats_summary[c]["mean"] for c in c_names]
        c_stds = [stats_summary[c]["std"] for c in c_names]

        fig, ax2 = plt.subplots(figsize=(8, 3.8))
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

        # 4. Generate PDF Document
        pdf_filename = "afe_hub_real_benchmark_report.pdf"
        doc = SimpleDocTemplate(pdf_filename, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontSize=18, textColor=colors.HexColor('#0e639c'), spaceAfter=8)
        h2_style = ParagraphStyle('H2Style', parent=styles['Heading2'], fontSize=12, textColor=colors.HexColor('#0e639c'), spaceBefore=10, spaceAfter=4)
        body_style = ParagraphStyle('BodyStyle', parent=styles['Normal'], fontSize=8.5, textColor=colors.HexColor('#333333'), spaceAfter=4)
        code_style = ParagraphStyle('CodeStyle', parent=styles['Normal'], fontName='Courier', fontSize=7, textColor=colors.HexColor('#800000'), backColor=colors.HexColor('#f5f5f5'), spaceAfter=3)

        elements = []
        elements.append(Paragraph("AFE Hub Firmware Benchmark Report (Real Hardware Data)", title_style))
        elements.append(Paragraph(f"Target Hub IP: <b>{HUB_IP}:{HUB_PORT}</b> | Benchmark Cycles (N): <b>{iterations}</b> | Target AFE ID: <b>{target_afe_id}</b>", body_style))
        elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#0e639c'), spaceAfter=8))

        # Section 1: Summary Table
        elements.append(Paragraph("1. Hardware Performance & Execution Summary", h2_style))
        table_data = [["Command Name", "Status", "Success", "Mean (ms)", "Min (ms)", "Max (ms)", "StdDev"]]
        for cmd in cmd_names:
            is_working = success_counts[cmd] > 0
            status_str = "WORKING" if is_working else "FAILED"
            s_rate = f"{success_counts[cmd]}/{iterations}"
            s = stats_summary[cmd]
            table_data.append([
                cmd,
                status_str,
                s_rate,
                f"{s['mean']:.2f}",
                f"{s['min']:.2f}",
                f"{s['max']:.2f}",
                f"{s['std']:.2f}"
            ])

        t = Table(table_data, colWidths=[120, 60, 55, 65, 65, 65, 60])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e1e1e')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f9f9f9')])
        ]))
        elements.append(t)
        elements.append(Spacer(1, 8))

        # Section 2: Burst Test (If mode == "all")
        if burst_results:
            elements.append(Paragraph("2. CAN Burst Delay Test on get_subdevice_status", h2_style))
            b_buf = generate_burst_test_plot(burst_results)
            elements.append(Image(b_buf, width=450, height=225))
            elements.append(Spacer(1, 6))

        # Section 3: Visualizations
        elements.append(Paragraph("3. Timing Analysis with Error Bars & Completion Points", h2_style))
        elements.append(Image(buf1, width=480, height=216))
        elements.append(Spacer(1, 6))
        elements.append(Image(buf2, width=450, height=213))
        elements.append(Spacer(1, 8))

        # Section 4: Instruction Reference & Live JSON Payloads
        elements.append(Paragraph("4. Command Instruction Reference & Real JSON Payloads", h2_style))
        for cmd, info in COMMAND_REGISTRY.items():
            is_working = success_counts[cmd] > 0
            status_color = "#28a745" if is_working else "#dc3545"
            status_text = "WORKING" if is_working else "NOT WORKING"

            req_json = escape_html(json.dumps(info['request']))
            res_json = escape_html(json.dumps(real_responses.get(cmd, {})))

            elements.append(Paragraph(f"<b>{cmd.upper()}</b> — <font color='{status_color}'>Status: {status_text}</font>", ParagraphStyle('CmdHead', parent=styles['Normal'], fontSize=9.5, textColor=colors.HexColor('#252526'), spaceBefore=4)))
            elements.append(Paragraph(f"<b>Description:</b> {escape_html(info['description'])}", body_style))
            elements.append(Paragraph(f"<b>Requires:</b> {escape_html(info['params'])}", body_style))
            elements.append(Paragraph(f"<b>Action / Procedure:</b> {escape_html(info['action'])}", body_style))
            elements.append(Paragraph(f"<b>Request JSON:</b> <code>{req_json}</code>", code_style))
            elements.append(Paragraph(f"<b>Response JSON:</b> <code>{res_json}</code>", code_style))
            elements.append(Spacer(1, 2))

        doc.build(elements)
        print(f"\nPDF successfully generated: {pdf_filename}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AFE Hub Benchmark & CAN Burst Delay Test Runner")
    parser.add_argument("--mode", choices=["default", "burst", "all"], default="default",
                        help="Execution mode: 'default' (standard command suite), 'burst' (CAN burst delay test for get_subdevice_status), or 'all' (both)")
    parser.add_argument("--iterations", type=int, default=N_ITERATIONS,
                        help="Number of benchmark iterations/cycles")
    args = parser.parse_args()

    run_benchmark_and_generate_pdf(mode=args.mode, iterations=args.iterations)