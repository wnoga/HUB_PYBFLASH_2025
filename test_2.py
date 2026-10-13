import json
import socket


def send_status_request(host="192.168.1.223", port=5555, timeout=5.0):
    payload = {"procedure": "get_all_latest_status"}
    message = json.dumps(payload) + "\n"

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, port))
            s.sendall(message.encode("utf-8"))

            # Buffer to accumulate incoming bytes until we hit a newline
            buffer = bytearray()
            while True:
                try:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buffer.extend(chunk)
                    # Stop reading if we find a newline (adjust to b'\r\n' if needed)
                    if b"\n" in buffer:
                        break
                except socket.timeout:
                    # If we timeout mid-stream, break out to process whatever we have so far
                    break

            if not buffer:
                return {"error": "No response received"}

            response_str = buffer.decode("utf-8", errors="ignore").strip()
            try:
                return json.loads(response_str)
            except json.JSONDecodeError:
                return {"raw_response": response_str}

    except Exception as e:
        return {"error": str(e)}


# Quick test execution
if __name__ == "__main__":
    print("Sending request...")
    result = send_status_request()
    print(json.dumps(result, indent=2))
    print(result["41"]["last_data"]["U_SIPM_MEAS0"])
    print(result["41"]["average_data"]["U_SIPM_MEAS0"])