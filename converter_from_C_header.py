import re
import pyperclip
import json

def parse_enum_to_python_class(header_content):
    """Parses C enum definitions and converts them into Python classes with hex values."""
    enum_pattern = r'typedef\s+enum\s*(?:__attribute__\(\([\w\s]+\)\))?\s*\{([\s\S]*?)\}\s*(\w+);'
    enums = {}

    matches = re.findall(enum_pattern, header_content, re.MULTILINE)

    for enum_body, enum_name in matches:
        enum_values = {}
        enum_lines = re.split(r',\s*\n', enum_body.strip())

        for line in enum_lines:
            line = line.strip()
            if not line or line.startswith('//') or line.startswith('/*'):
                continue

            match = re.match(r'(\w+)\s*=\s*(0x[\da-fA-F]+|0b[01]+|\d+)', line)
            if match:
                key, value = match.groups()
                common_prefix = "AFECommand_"
                key = key.replace(common_prefix, '')

                if value.startswith("0x"):
                    value = int(value, 16)
                elif value.startswith("0b"):
                    value = int(value, 2)
                else:
                    value = int(value)

                enum_values[key] = hex(value)

        enums[enum_name] = enum_values

    return enums

def generate_python_classes(enums):
    """Generates Python classes with constants from parsed enums."""
    class_code = ""

    for enum_name, values in enums.items():
        class_code += f"class {enum_name}:\n"
        if not values:
            class_code += "    pass\n"
        
        for name, value in values.items():
            class_code += f"    {name} = {value}\n"
        class_code += "\n"

    return class_code

def parse_enum_to_python_json(header_content):
    """Parses C enums and converts them to a JSON-like Python dictionary."""
    enums = parse_enum_to_python_class(header_content)
    json_data = json.dumps(enums, indent=4)
    return json_data

def get_input_from_clipboard():
    return pyperclip.paste()

def copy_output_to_clipboard(output):
    pyperclip.copy(output)

def convert_header_to_python_classes():
    header_content = get_input_from_clipboard()
    if not header_content.strip():
        print("Clipboard is empty or does not contain valid C header content.")
        return

    enums = parse_enum_to_python_class(header_content)
    python_code = generate_python_classes(enums)

    if not python_code.strip():
        print("No valid enums found in the clipboard content.")
        return

    copy_output_to_clipboard(python_code)
    print("Python classes with hex constants have been copied to the clipboard.")

def convert_header_to_json():
    header_content = get_input_from_clipboard()
    if not header_content.strip():
        print("Clipboard is empty or does not contain valid C header content.")
        return

    json_output = parse_enum_to_python_json(header_content)
    copy_output_to_clipboard(json_output)
    print("JSON representation of enums has been copied to the clipboard.")

# Example usage
convert_header_to_python_classes()
# convert_header_to_json()