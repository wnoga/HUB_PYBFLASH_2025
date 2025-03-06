import re
import pyperclip

# Function to parse C enum and generate corresponding Python class with constants
def parse_enum(header_content):
    # Regular expression to match enum definitions
    enum_pattern = r'typedef enum\s*\{(.*?)\}\s*(\w+);'
    
    enums = {}
    
    # Find all enums in the header content
    matches = re.findall(enum_pattern, header_content, re.DOTALL)
    
    for enum_body, enum_name in matches:
        # Process each enum
        enum_lines = enum_body.strip().split('\n')
        enum_values = {}
        
        # Extract the enum values
        for line in enum_lines:
            # Match the enum value with its name and hex/binary value
            match = re.match(r'\s*(\w+)\s*=\s*(0x[\da-fA-F]+|0b[01]+|[\d]+)\s*,?', line)
            if match:
                enum_name_value = match.group(1)
                enum_value = match.group(2)
                
                # Remove the AFECommand_ prefix from the constant names
                enum_name_value = enum_name_value.replace('AFECommand_', '')

                # Convert hex or binary to integer
                if enum_value.startswith('0x'):
                    enum_value = int(enum_value, 16)
                elif enum_value.startswith('0b'):
                    enum_value = int(enum_value, 2)
                else:
                    enum_value = int(enum_value)
                
                # Store in the enum values dictionary
                enum_values[enum_name_value] = hex(enum_value)  # Store as hex
                
        # Store the enum class
        enums[enum_name] = enum_values
    
    return enums

# Function to generate Python classes with constants from the parsed enums
def generate_python_classes(enums):
    # Python class code
    class_code = ""
    
    # For each enum, generate a Python class with constant values
    for enum_name, values in enums.items():
        class_code += f"class {enum_name}:\n"
        for name, value in values.items():
            class_code += f"    {name} = {value}\n"
        class_code += "\n"
    
    return class_code

# Function to get the header content from the clipboard
def get_input_from_clipboard():
    return pyperclip.paste()

# Function to copy the generated Python code to the clipboard
def copy_output_to_clipboard(python_code):
    pyperclip.copy(python_code)

# Main function to convert a header file to Python classes with constants
def convert_header_to_python_classes():
    # Get input from clipboard
    header_content = get_input_from_clipboard()
    
    # Parse the header content and generate the Python classes
    enums = parse_enum(header_content)
    python_code = generate_python_classes(enums)
    
    # Copy the output to the clipboard
    copy_output_to_clipboard(python_code)
    
    print("Python classes with hex constants (prefix removed) have been copied to the clipboard.")

# Example usage
convert_header_to_python_classes()
