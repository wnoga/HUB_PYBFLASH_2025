import socket
import json
import time
import math
import statistics
import matplotlib.pyplot as plt

# Configuration
HUB_IP = "192.168.1.223"
HUB_PORT = 5555
N_ITERATIONS = 2  # Number of full benchmark cycles
SOCKET_TIMEOUT = 12.0  # Seconds per command execution timeout

# ---------------------------------------------------------------------------
# Command Instruction Registry Documentation
# ---------------------------------------------------------------------------
COMMAND_DOCS = {
    "get_all_afe_id": {
        "params": "None",
        "returns": "{'status': 'ok', 'result': {'afe_id_list': [41, ...]}}",
        "description": "Queries the Hub for all active/discovered AFE subdevice IDs."
    },
    "get_afe_data": {
        "params": "afe_id: int",
        "returns": "{'status': 'ok', 'afe_id': 41, 'periodic_data': {...}, 'channels': {...}}",
        "description": "Retrieves cached telemetry and channel status dictionaries for a specific AFE."
    },
    "get_serial_number": {
        "params": "afe_id: int",
        "returns": "{'status': 'ok', 'result': str/int}",
        "description": "Queries AFE device unique hardware serial number (Command 0x00)."
    },
    "get_version": {
        "params": "afe_id: int",
        "returns": "{'status': 'ok', 'result': str}",
        "description": "Queries AFE C firmware version string/number (Command 0x01)."
    },
    "reset_all": {
        "params": "afe_id: int",
        "returns": "{'status': 'ok'}",
        "description": "Triggers hard reset (NVIC_SystemReset) on target AFE subdevice (Command 0x03)."
    },
    "start_adc": {
        "params": "afe_id: int",
        "returns": "{'status': 'ok'}",
        "description": "Starts peripheral ADC conversion loop on target AFE (Command 0x04)."
    },
    "get_timestamp": {
        "params": "afe_id: int",
        "returns": "{'status': 'ok', 'result': int_ms}",
        "description": "Retrieves target AFE uptime timestamp in milliseconds (Command 0x05)."
    },
    "get_subdevice_status": {
        "params": "afe_id: int",
        "returns": "{'status': 'ok', 'result': dict}",
        "description": "Fetches 13-item status structure per Master/Slave subdevice (Command 0x08)."
    },
    "get_sensor_data_last": {
        "params": "afe_id: int, mask: int (0x00-0xFF)",
        "returns": "{'status': 'ok', 'result': {ch_name: dp_dict}}",
        "description": "Gets latest single raw/SI sensor readings for masked channels (Command 0x30)."
    },
    "get_sensor_data_average": {
        "params": "afe_id: int, mask: int (0x00-0xFF)",
        "returns": "{'status': 'ok', 'result': {ch_name: dp_dict}}",
        "description": "Gets averaged SI sensor block readings for masked channels (Command 0x31)."
    },
    "shift_voltage": {
        "params": "afe_id: int, type: 'M'|'S'|'Both', value: float",
        "returns": "{'status': 'ok'}",
        "description": "Adjusts regulator voltage offset (V_offset) for Master, Slave, or Both (Command 0xE9)."
    },
    "set_regulator_t_opt": {
        "params": "afe_id: int, value: float",
        "returns": "{'status': 'ok'}",
        "description": "Sets optimal reference operating temperature T_opt in deg C (Command 0xE3)."
    },
    "set_regulator_dt": {
        "params": "afe_id: int, value: float",
        "returns": "{'status': 'ok'}",
        "description": "Sets minimum temperature dead-band delta dT to trigger DAC updates (Command 0xE4)."
    },
    "set_averaging_alpha": {
        "params": "afe_id: int, value: float",
        "returns": "{'status': 'ok'}",
        "description": "Sets exponential moving average weighting factor alpha (Command 0xD1)."
    }
}


class AFEHubClient:
    """Handles raw JSON over TCP socket communication with the AFE Hub."""

    def __init__(self, host, port):
        self.host = host
        self.port = port

    def send_cmd(self, procedure, **kwargs):
        payload = {"procedure": procedure}
        payload.update(kwargs)
        raw_json = json.dumps(payload) + "\n"

        start_t = time.ticks_ms() if hasattr(time, 'ticks_ms') else time.time() * 1000.0

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

            end_t = time.ticks_ms() if hasattr(time, 'ticks_ms') else time.time() * 1000.0
            elapsed_ms = end_t - start_t

            res_text = response_bytes.decode('utf-8').strip()
            if not res_text:
                return False, {"error": "Empty response"}, elapsed_ms

            # Parse JSON or HTTP body fallback
            if res_text.startswith("HTTP/1."):
                body = res_text.split("\r\n\r\n")[-1]
                res_dict = json.loads(body)
            else:
                res_dict = json.loads(res_text)

            is_ok = res_dict.get("status") in ["ok", "OK"]
            return is_ok, res_dict, elapsed_ms

        except Exception as exc:
            end_t = time.ticks_ms() if hasattr(time, 'ticks_ms') else time.time() * 1000.0
            return False, {"error": str(exc)}, end_t - start_t
        finally:
            s.close()


def run_benchmark(host, port, iterations):
    client = AFEHubClient(host, port)
    metrics = {cmd: [] for cmd in COMMAND_DOCS.keys()}
    success_counts = {cmd: 0 for cmd in COMMAND_DOCS.keys()}

    print("===============================================================")
    print(" AFE HUB BENCHMARK & SYSTEM FUNCTIONALITY TEST SUITE")
    print("===============================================================\n")

    # Step 1: Wait for an active & configured AFE
    print("[STEP 1] Waiting for an active & configured AFE device...")
    target_afe_id = None
    while target_afe_id is None:
        ok, res, _ = client.send_cmd("get_all_afe_id")
        if ok:
            res_val = res.get("result", [])
            if isinstance(res_val, dict):
                afe_list = res_val.get("afe_id_list", [])
            elif isinstance(res_val, list):
                afe_list = res_val
            else:
                afe_list = []

            if len(afe_list) > 0:
                target_afe_id = afe_list[0]
                print(" -> Found AFE Device ID: {}".format(target_afe_id))
            else:
                print(" -> AFE ID list empty, retrying in 2 seconds...")
                time.sleep(2)
        else:
            print(" -> No AFE detected yet, retrying in 2 seconds...")
            time.sleep(2)

    print("\nStarting execution cycles (N = {})...".format(iterations))

    for cycle in range(1, iterations + 1):
        print("\n--- Iteration Cycle {} / {} ---".format(cycle, iterations))

        # Step 2: Reset AFE (Hardware reboot) and wait for configuration completion
        print("[Step 2] Triggering AFE Reset (cmd: reset_all)...")
        # reset_all triggers NVIC_SystemReset immediately without response packet
        try:
            _, _, elapsed = client.send_cmd("reset_all", id=target_afe_id, afe_id=target_afe_id)
        except Exception as e:
            elapsed = 15.0  # fallback timing if connection resets
        
        metrics["reset_all"].append(elapsed)
        success_counts["reset_all"] += 1
        print(" -> Reset command issued. Waiting for device reboot and complete reconfiguration...")

        # Active polling loop waiting for device to finish configuration and bring up channels
        reconfigured = False
        poll_start = time.time()
        timeout_sec = 20.0

        while not reconfigured and (time.time() - poll_start) < timeout_sec:
            time.sleep(1.5)
            ok, data, _ = client.send_cmd("get_afe_data", afe_id=target_afe_id)
            if ok:
                channels = data.get("channels", {})
                if channels and len(channels) > 0:
                    reconfigured = True
                    print(" -> AFE Device {} successfully reconfigured and online!".format(target_afe_id))
                    break
            print(" -> Waiting for reconfiguration... (elapsed: {:.1f}s)".format(time.time() - poll_start))

        if not reconfigured:
            print(" [WARN] Reconfiguration timeout reached, proceeding with test cycle...")

        # Step 3 & 4: Get Configuration and Execute Getters
        print("[Step 3 & 4] Executing Getters...")
        
        getters = [
            ("get_all_afe_id", {}),
            ("get_afe_data", {"afe_id": target_afe_id}),
            ("get_serial_number", {"afe_id": target_afe_id}),
            ("get_version", {"afe_id": target_afe_id}),
            ("get_timestamp", {"afe_id": target_afe_id}),
            ("get_subdevice_status", {"afe_id": target_afe_id}),
            ("get_sensor_data_last", {"afe_id": target_afe_id, "mask": 0xFF}),
            ("get_sensor_data_average", {"afe_id": target_afe_id, "mask": 0xFF}),
        ]

        for cmd, kwargs in getters:
            ok, res, elapsed = client.send_cmd(cmd, **kwargs)
            metrics[cmd].append(elapsed)
            if ok:
                success_counts[cmd] += 1
            print(" -> {:<25}: Success={}, Time={:.2f} ms".format(cmd, ok, elapsed))

        # Step 5: Test Control/Set Functions
        print("[Step 5] Testing Control and Configuration Setters...")

        t_opt_val = 25.0
        dt_val = 0.5
        alpha_val = 1e-6

        setters = [
            ("start_adc", {"afe_id": target_afe_id}),
            ("shift_voltage", {"id": target_afe_id, "type": "M", "value": 0.5}),
            ("set_regulator_t_opt", {"afe_id": target_afe_id, "value": t_opt_val}),
            ("set_regulator_dt", {"afe_id": target_afe_id, "value": dt_val}),
            ("set_averaging_alpha", {"afe_id": target_afe_id, "value": alpha_val}),
        ]

        for cmd, kwargs in setters:
            ok, res, elapsed = client.send_cmd(cmd, **kwargs)
            metrics[cmd].append(elapsed)
            if ok:
                success_counts[cmd] += 1
            print(" -> {:<25}: Success={}, Time={:.2f} ms".format(cmd, ok, elapsed))

        # Step 6: Telemetry Gathering Pause
        print("[Step 6] Gathering telemetry buffer data (1s sleep)...")
        time.sleep(1.0)

    # ---------------------------------------------------------------------------
    # Step 7: Print Verification & Instruction Report
    # ---------------------------------------------------------------------------
    print("\n===============================================================")
    print(" FUNCTION INSTRUCTION & BENCHMARK PERFORMANCE REPORT")
    print("===============================================================\n")

    report_fmt = "{:<26} | {:<8} | {:<10} | {:<10} | {:<10}"
    print(report_fmt.format("Command Name", "Success", "Mean (ms)", "Min (ms)", "Max (ms)"))
    print("-" * 75)

    stats_summary = {}

    for cmd, times in metrics.items():
        succ_rate = "{}/{}".format(success_counts[cmd], iterations)
        if times:
            mean_t = statistics.mean(times)
            min_t = min(times)
            max_t = max(times)
            std_t = statistics.stdev(times) if len(times) > 1 else 0.0
        else:
            mean_t, min_t, max_t, std_t = 0.0, 0.0, 0.0, 0.0

        stats_summary[cmd] = {"mean": mean_t, "min": min_t, "max": max_t, "std": std_t}
        print(report_fmt.format(cmd, succ_rate, "{:.2f}".format(mean_t), "{:.2f}".format(min_t), "{:.2f}".format(max_t)))

    print("\n" + "=" * 75)
    print(" COMMAND INSTRUCTION REFERENCE")
    print("=" * 75)
    for cmd, docs in COMMAND_DOCS.items():
        print("• {}".format(cmd.upper()))
        print("  Description : {}".format(docs["description"]))
        print("  Requires    : {}".format(docs["params"]))
        print("  Returns     : {}".format(docs["returns"]))
        print()

    # ---------------------------------------------------------------------------
    # Plotting Timing Metrics with Error Bars
    # ---------------------------------------------------------------------------
    plot_timing_results(stats_summary, metrics)


def plot_timing_results(stats_summary, metrics):
    cmd_names = list(stats_summary.keys())
    means = [stats_summary[cmd]["mean"] for cmd in cmd_names]
    stds = [stats_summary[cmd]["std"] for cmd in cmd_names]

    # Plot 1: Full Command Suite Timing with Errorbars
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10))

    x_pos = range(len(cmd_names))
    ax1.errorbar(x_pos, means, yerr=stds, fmt='o', color='#0e639c', ecolor='#dc3545',
                 capsize=5, elinewidth=2, markeredgewidth=2, label='Mean Time ± StdDev')
    
    # Mark points where commands completed
    ax1.plot(x_pos, means, 's', color='#28a745', markersize=6, label='Completion Point')
    
    ax1.set_xticks(list(x_pos))
    ax1.set_xticklabels(cmd_names, rotation=45, ha='right', fontsize=9)
    ax1.set_ylabel('Execution Time (ms)')
    ax1.set_title('Overall Command Execution Times (N iterations with Error Bars)')
    ax1.grid(True, linestyle='--', alpha=0.6)
    ax1.legend()

    # Plot 2: Common Setter & Regulator Commands Comparison
    common_cmds = [
        "shift_voltage", "set_regulator_t_opt", "set_regulator_dt", 
        "set_averaging_alpha", "start_adc"
    ]
    common_indices = [cmd_names.index(c) for c in common_cmds if c in cmd_names]
    c_names = [cmd_names[i] for i in common_indices]
    c_means = [means[i] for i in common_indices]
    c_stds = [stds[i] for i in common_indices]

    c_pos = range(len(c_names))
    ax2.bar(c_pos, c_means, yerr=c_stds, align='center', alpha=0.7, color='#388a34',
            ecolor='#1e1e1e', capsize=6, label='Setter Mean Time')
    ax2.plot(c_pos, c_means, 'ro', label='Completion Trigger Point')

    ax2.set_xticks(list(c_pos))
    ax2.set_xticklabels(c_names, rotation=25, ha='right', fontsize=9)
    ax2.set_ylabel('Execution Time (ms)')
    ax2.set_title('Secondary Benchmark: Common Setting & Regulator Commands')
    ax2.grid(True, linestyle='--', alpha=0.6)
    ax2.legend()

    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    run_benchmark(HUB_IP, HUB_PORT, N_ITERATIONS)