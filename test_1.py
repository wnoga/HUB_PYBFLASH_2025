import serial
import json
import time

def get_json_from_serial(port, baudrate, timeout=1):
    """
    Reads data from a serial port and attempts to parse it as JSON.

    Args:
        port (str): The serial port (e.g., 'COM3' on Windows, '/dev/ttyUSB0' on Linux).
        baudrate (int): The baud rate of the serial communication.
        timeout (float): The timeout for reading from the serial port (in seconds).

    Returns:
        dict or None: A dictionary representing the parsed JSON data, or None if no valid JSON is received.
    """
    try:
        ser = serial.Serial(port, baudrate, timeout=timeout)
        while True:
            if ser.in_waiting > 0:
                line = ser.readline().decode('utf-8').rstrip()
                try:
                    data = json.loads(line)
                    return data
                except json.JSONDecodeError:
                    print(f"Invalid JSON received: {line}")
            time.sleep(0.01)
    except serial.SerialException as e:
        print(f"Error opening serial port: {e}")
        return None
    finally:
        if 'ser' in locals() and ser.is_open:
            ser.close()

# Example usage:
if __name__ == "__main__":
    port = '/dev/ttyACM0'  # Replace with your serial port
    baudrate = 115200
    json_data = get_json_from_serial(port, baudrate)
    ser = serial.Serial(port, baudrate, timeout=1)
    while True:
        time.sleep(1.0)
        ser.write(b'hub.powerOn()')
        tmp = ser.read_all()
