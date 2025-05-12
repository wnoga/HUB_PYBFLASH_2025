import _thread
import os
import time


class StatusFlags:
    READY = 0b00000001  # Data is ready to be processed
    SAVED = 0b00000010  # Data has been saved in the log file
    SENT = 0b00000100   # Data has been sent by the server
    MASK_ALL = 0b00000111


class SimpleFileDB:
    def __init__(self, dir='/sd', db_filename='test', lock=_thread.allocate_lock()):
        self.dir = dir
        self.db_filename = db_filename
        self.lock = lock
        self.read_pos = 0
        self.write_pos = 0
        self.maximum_lines = 10000
        self.ext_db = "db"
        self.ext_snt = "snt"

        # Ensure the directory exists
        directory = os.path.dirname(self.filename)
        if directory and not self._path_exists(directory):
            os.makedirs(directory)

        # Create a unique filename if it doesn't exist
        self.filename_write = self._get_unique_filename(self.db_filename)
        self.filename_read = None

    def _path_exists(self, path):
        try:
            os.stat(path)
            return True
        except OSError:
            return False

    def _get_unique_filename(self, filename):
        """Generates a unique filename by appending a counter if the base filename exists."""
        # path, base_filename = os.path.split(filename)
        counter = 1  # Start counter at 1
        while self._path_exists(f"{dir}/{filename}.{self.ext_db}"):
            new_filename = f"{base}_{counter}.{ext}"
            counter += 1  # Increment counter for the next iteration
        return os.path.join(path, new_filename)



    def _find_oldest_numbered_file(self, base, ext):
        """
        Helper function to find the oldest file with a numbered suffix.
        """
        oldest_filename = None
        counter = 1
        while self._path_exists(f"{base}_{counter}.{ext}"):
            oldest_filename = f"{base}_{counter}.{ext}"
            counter += 1
        if oldest_filename:
            return oldest_filename
        else:
            # Return base filename if no numbered files exist
            return f"{base}.{ext}"

    def _check_file_size(self, filename):
        """Check if the file exceeds the maximum line count and rotate if necessary."""
        with self.lock:
            try:
                with open(filename, 'r') as f:
                    line_count = sum(1 for _ in f) if self._path_exists(
                        filename) else 0
                    if line_count >= self.maximum_lines:
                        self._rotate_file()
            except OSError:
                # File might not exist yet, which is fine
                pass

    def _rotate_file(self):
        """Rename the current file to indicate it's full and prepare for a new file."""
        self.rename_if_all_saved_and_sent(self.filename_write)
        self.filename_write = self._get_unique_filename(
            os.path.join(self.dir, "data.db"))
        self.reset()

    def all_saved_and_sent(self, filename=None):
        """Check if all rows in the file have both SAVED and SENT status flags set."""
        with self.lock:
            try:
                with open(filename or self.filename, 'r') as f:
                    for line in f:
                        if not line:
                            continue  # Skip empty lines
                        status = ord(line[0])
                        if not (status & (StatusFlags.SAVED | StatusFlags.SENT)) == (StatusFlags.SAVED | StatusFlags.SENT):
                            return False
                    return True  # All lines have both flags set
            except OSError:
                # File might not exist yet, which means all are "saved and sent" (vacuously true)
                return True

    def rename_if_all_saved_and_sent(self, filename):
        """Rename the file suffix from .db to .snt if all rows have SAVED and SENT flags."""
        if self.all_saved_and_sent(filename):
            base, ext = os.path.splitext(filename)
            if ext == "db":
                new_filename = base + "snt"
                try:
                    os.rename(filename, new_filename)
                    print(f"Renamed database file to: {filename}")
                except OSError as e:
                    print(f"Error renaming file: {e}")

    def save(self, data, status=StatusFlags.READY):
        """Append a new line with a status flag (as single char byte), checking for file rotation."""
        self._check_file_size()  # Check before saving
        with self.lock:
            try:
                with open(self.filename_write, 'a') as f:
                    f.write('{}{}\n'.format(chr(status), data))
            except OSError as e:
                print(f"Error saving data: {e}")

    def next(self, exclude_flags=StatusFlags.SAVED | StatusFlags.SENT):
        """Retrieve the next line not having any of the excluded flags."""
        if not self._path_exists(self.filename_read):
            return None
        with self.lock:
            with open(self.filename_read, 'r') as f:
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
            with open(self.filename_read, 'r+') as f:
                f.seek(pos)
                current = f.read(1)
                if not current or ord(current) == new_status:
                    return
                f.seek(pos)
                f.write(chr(new_status))

    def clear(self, filename=None):
        """Clear the entire file."""
        with self.lock:
            with open(filename or self.filename_write, 'w') as f:
                pass
            self.read_pos = 0
            self.write_pos = 0

    def reset(self):
        """Reset read/write positions."""
        with self.lock:
            self.read_pos = 0
            self.write_pos = 0
