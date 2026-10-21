import socket
import json
import sys

def fetch_afe_data(host="192.168.1.223", port=5555, afe_id=1, timeout=10):
    """
    Connects to the MicroPython Hub backend, requests get_afe_data for the given AFE ID,
    and prints the structured telemetry response.
    """
    payload = {
        "procedure": "get_afe_data",
        "afe_id": afe_id
    }
    
    # Serialize request payload to JSON bytes
    request_data = (json.dumps(payload) + "\r\n").encode("utf-8")
    
    print(f"Connecting to MicroPython Hub at {host}:{port} for AFE-{afe_id}...")
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    
    try:
        sock.connect((host, port))
        print("Connected successfully. Sending procedure request...")
        sock.sendall(request_data)
        
        # Read incoming response stream in chunks
        response_chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response_chunks.append(chunk)
            
        raw_response = b"".join(response_chunks).decode("utf-8", "ignore")
        
        # Strip HTTP headers if response was served over standard HTTP (e.g., HTTP/1.1 200 OK...)
        if "{" in raw_response:
            json_str = raw_response[raw_response.index("{"):]
        else:
            json_str = raw_response

        # Parse and pretty print the resulting JSON telemetry structure
        parsed_json = json.loads(json_str)
        
        print("\n--- AFE DATA FETCHED SUCCESSFULLY ---")
        exit()
        print(json.dumps(parsed_json, indent=4))
        
        return parsed_json

    except socket.timeout:
        print("[Error]: Connection timed out waiting for hub response.")
    except ConnectionRefusedError:
        print(f"[Error]: Connection refused by {host}:{port}. Verify the hub is powered on and running.")
    except json.JSONDecodeError as e:
        print(f"[Error]: Failed to parse JSON response: {e}")
        print(f"Raw received data:\n{raw_response[:255]}")
    except Exception as exc:
        print(f"[Error]: An unexpected error occurred: {exc}")
    finally:
        sock.close()

if __name__ == "__main__":
    # Target AFE ID configuration (change if needed)
    target_afe_id = 1
    if len(sys.argv) > 1:
        try:
            target_afe_id = int(sys.argv[1])
        except ValueError:
            pass
            
    fetch_afe_data(afe_id=target_afe_id)