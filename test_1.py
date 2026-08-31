import serial
import json
import time
import asyncio as uasyncio
import csv
asyncio = uasyncio

def parse_csv_line(line):
    """
    Splits a CSV line while properly handling quoted fields containing commas.
    Example: '32,,,"0,06"' -> ['32', '', '', '0,06']
    """
    fields = []
    current_field = []
    in_quotes = False

    for char in line:
        if char == '"':
            in_quotes = not in_quotes  # Toggle quote state
        elif char == ',' and not in_quotes:
            fields.append(''.join(current_field).strip())
            current_field = []
        else:
            current_field.append(char)

    fields.append(''.join(current_field).strip())
    return fields


def convert_value(key, value):
    if not value:
        return ''

    if key == 'ID':
        try:
            return int(value)
        except ValueError:
            return value
    elif key in ('SN_AFE', 'SN_SiPM', 'M/S'):
        return value

    # Convert comma decimal separators to standard dots
    clean_val = value.replace(',', '.')
    try:
        return float(clean_val)
    except ValueError:
        return value


async def callibration_reader_csv(csv_file):
    try:
        with open(csv_file, mode='r', encoding='utf-8') as file:
            header_line = file.readline()
            if not header_line:
                return []

            headers = parse_csv_line(header_line.strip())

            rows = []
            for line in file:
                line_str = line.strip()
                if not line_str:
                    continue

                values = parse_csv_line(line_str)
                row_dict = {
                    key: convert_value(key, val)
                    for key, val in zip(headers, values)
                }
                rows.append(row_dict)

            return rows
    except Exception as e:
        print("Error reading CSV file {}: {}".format(csv_file, e))
        return []


async def read_callibration_csv(file, toSi=False):
    callib_data = await callibration_reader_csv(file)
    if not callib_data:
        return [], {}

    callib_data_mean = {}
    uniq_id = set()

    for c in callib_data:
        g = c.get('M/S')
        if not g:
            continue

        if g not in callib_data_mean:
            callib_data_mean[g] = {}

        for k, v in c.items():
            if k == 'ID':
                uniq_id.add(v)
                continue

            if isinstance(v, (float, int)):
                if k not in callib_data_mean[g]:
                    callib_data_mean[g][k] = []
                callib_data_mean[g][k].append(v)

    groups = ('M', 'S')
    for g in groups:
        if g in callib_data_mean:
            group_dict = callib_data_mean[g]
            for k, values in group_dict.items():
                group_dict[k] = sum(values) / len(values) if values else None
            group_dict['ID'] = 0
            group_dict['M/S'] = g

    return callib_data, callib_data_mean


async def get_configuration_from_files(
    afe_id,
    callibration_data_file_csv="dane_kalibracyjne.csv",
    TempLoop_file_csv="TempLoop.csv",
    UID=None
):
    (TempLoop_data, TempLoop_data_mean), (callib_data, callib_data_mean) = await uasyncio.gather(
        read_callibration_csv(TempLoop_file_csv),
        read_callibration_csv(callibration_data_file_csv)
    )

    callibration = {'ID': afe_id}

    for c0 in (callib_data, TempLoop_data):
        for c in c0:
            if c.get('ID') == afe_id:
                if UID is not None and c.get('SN_AFE') != UID:
                    continue
                g = c.get('M/S')
                if g:
                    if g not in callibration:
                        callibration[g] = {}
                    callibration[g].update(c)

    for c0 in (callib_data_mean, TempLoop_data_mean):
        for g in ('M', 'S'):
            if g in c0:
                if g not in callibration:
                    callibration[g] = {}
                for k, default_val in c0[g].items():
                    current_val = callibration[g].get(k)
                    if current_val is None:
                        callibration[g][k] = ''
                    elif str(current_val) == '':
                        callibration[g][k] = default_val

    return callibration

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
    # asyncio.run(main())
    if False:
        port = '/dev/ttyACM0'  # Replace with your serial port
        baudrate = 115200
        json_data = get_json_from_serial(port, baudrate)
        ser = serial.Serial(port, baudrate, timeout=1)
        while True:
            time.sleep(1.0)
            ser.write(b'hub.powerOn()')
            tmp = ser.read_all()
            
    if True:
        from my_utilities import get_configuration_from_files

        async def run_async_config_test():
            """
            Asynchronous wrapper to call get_configuration_from_files.
            """
            callibration = await get_configuration_from_files(36,callibration_data_file_csv="dane_kalibracyjne_k.csv",TempLoop_file_csv="TempLoop_k.csv")
            print(callibration)

        uasyncio.run(run_async_config_test())
