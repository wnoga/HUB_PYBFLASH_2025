import _thread
import os

class StatusFlags:
    READY = 0b00000001  # Data is ready to be processed
    SAVED = 0b00000010  # Data has been saved in the log file
    SENT = 0b00000100   # Data has been sent by the server
    MASK_ALL = 0b00000111


class SimpleFileDB:
    def __init__(self, filename='/sd/test.db'):
        self.filename = filename
        self.lock = _thread.allocate_lock()
        self.read_pos = 0
        self.write_pos = 0

        # Create file if it doesn't exist
        if not os.stat(self.filename):
            with open(self.filename, 'w') as f:
                pass

    def save(self, data, status=StatusFlags.READY):
        """Append a new line with a status flag (as single char byte)."""
        with self.lock:
            with open(self.filename, 'a') as f:
                f.write('{}{}\n'.format(chr(status), data))

    def next(self, exclude_flags=StatusFlags.SAVED | StatusFlags.SENT):
        """Retrieve the next line not having any of the excluded flags."""
        with self.lock:
            with open(self.filename, 'r') as f:
                f.seek(self.read_pos)
                while True:
                    pos = f.tell()
                    line = f.readline()
                    if not line:
                        return None
                    status = ord(line[0])
                    if not (status & exclude_flags):
                        self.read_pos = f.tell()
                        return (pos, status, line[1:].rstrip())

    def update_status(self, pos, new_status):
        """Update the status flag at a specific byte position."""
        with self.lock:
            with open(self.filename, 'r+') as f:
                f.seek(pos)
                current = f.read(1)
                if not current or ord(current) == new_status:
                    return
                f.seek(pos)
                f.write(chr(new_status))

    def clear(self):
        """Clear the entire file."""
        with self.lock:
            with open(self.filename, 'w') as f:
                pass
            self.read_pos = 0
            self.write_pos = 0

    def reset(self):
        """Reset read/write positions."""
        with self.lock:
            self.read_pos = 0
            self.write_pos = 0
