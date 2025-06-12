import _thread
import os
import time
import uasyncio # For p.print


class StatusFlags:
    NONE = 0b00000000
    READY = 0b00000001  # Data is ready to be processed
    SAVED = 0b00000010  # Data has been saved in the log file
    SENT = 0b00000100   # Data has been sent by the server
    # Note: p is not defined here, assuming it's imported if used by print statements
    ERROR = 0b10000000
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
        if dir and not self._path_exists(dir):
            os.makedirs(dir)

        # Create a unique filename if it doesn't exist
        self.filename_write = self._get_unique_filename(self.db_filename)
        self.filename_read = None

        self.state = 0

        self.toSend = None
        self.toSendStatus = StatusFlags.NONE

    def _path_exists(self, path):
        try:
            os.stat(path)
            return True
        except OSError:
            return False

    def _file_path(self, filename, ext=None, dir=None):
        dir = dir or self.dir
        ext = ext or self.ext_db
        return "{}/{}.{}".format(dir, filename, ext)

    def _get_unique_filename(self, filename, dir=None, ext=None):
        """Generates a unique filename by appending a counter if the base filename exists."""
        # path, base_filename = os.path.split(filename)
        counter = 1  # Start counter at 1
        dir = dir or self.dir
        ext = ext or self.ext_db
        filename_new = filename
        while self._path_exists(self._file_path(filename=filename_new, ext=ext, dir=dir)):
            filename_new = f"{filename}_{counter}"
            counter += 1  # Increment counter for the next iteration
        return self._file_path(filename=filename_new, ext=ext, dir=dir)

    def _find_oldest_numbered_file(self, filename, ext=None, dir=None) -> str | None:
        """Helper function to find the oldest file with a numbered suffix."""
        ext = ext or self.ext_db
        dir = dir or self.dir
        prefix = filename + "_"
        files = []
        try:
            files = os.listdir(dir)
        except OSError:
            return None

        matching_files = []
        for file in files:
            if file.startswith(filename) and file.endswith("." + ext):
                if file == filename + "." + ext:
                    return filename
                matching_files.append(file)
        if not matching_files:
            return None
        matching_files.sort()
        return matching_files[0].rsplit(".", 1)[0]

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
        self.rename_if_all_saved_and_sent(
            self._file_path(self.filename_write))
        self.filename_write = self._get_unique_filename(
            os.path.join(self.dir, "data.db"))
        self.reset()

    def all_saved_and_sent(self, path):
        """Check if all rows in the file have both SAVED and SENT status flags set."""
        # with self.lock:
        if True:
            try:
                with open(path, 'r') as f:
                    for line in f:
                        if not line:
                            continue  # Skip empty lines
                        status = ord(line[0])
                        if not (status & (StatusFlags.SAVED | StatusFlags.SENT)) == (StatusFlags.SAVED | StatusFlags.SENT):
                            return False
                    # print("all_saved_and_sent {}".format(path))
                    return True  # All lines have both flags set
            except OSError as e:
                # await p.print("Error all_saved_and_sent: {}".format(e)) # Would need p and async
                # File might not exist yet, which means all are "saved and sent" (vacuously true)
                return True

    async def rename_if_all_saved_and_sent(self, path: str): # Changed to async def
        """Rename the file suffix from .db to .snt if all rows have SAVED and SENT flags."""
        if self.all_saved_and_sent(path):
            base, ext = path.rsplit(".", 1)
            if ext == self.ext_db:
                new_path = base + "." + self.ext_snt
                try:
                    os.rename(path, new_path)
                    # await p.print(f"Renamed database file to: {new_path}") # Would need p
                except OSError as e:
                    await p.print(f"Error renaming file: {e}") # Changed to await p.print

    async def save(self, data, status=StatusFlags.READY): # Changed to async def
        """Append a new line with a status flag (as single char byte), checking for file rotation."""
        self._check_file_size(self.filename_write)  # Check before saving
        with self.lock:
            try:
                with open(self.filename_write, 'a') as f:
                    f.write('{}{}\n'.format(chr(status), data))
            except OSError as e:
                await p.print(f"Error saving data: {e}") # Changed to await p.print

    def end_reading(self):
        # print("End reading {}".format(self.filename_read))
        if not self.filename_read is None:
            self.rename_if_all_saved_and_sent(
                self._file_path(self.filename_read))
        self.filename_read = None
        self.read_pos = 0

    def next(self, exclude_flags=StatusFlags.SAVED | StatusFlags.SENT):
        """Retrieve the next line not having any of the excluded flags."""
        if self.filename_read is None:
            self.filename_read = self._find_oldest_numbered_file(
                self.db_filename, self.ext_db)
            if self.filename_read is None:
                return None
            # print("NEXT: {}".format(self.filename_read))
            self.rename_if_all_saved_and_sent(
                self._file_path(self.filename_read))
            self.read_pos = 0
        if not self._path_exists(self._file_path(self.filename_read)):
            return None
        with self.lock:
            with open(self._file_path(self.filename_read), 'r') as f:
                f.seek(self.read_pos)
                while True:
                    pos = f.tell()
                    line = f.readline()
                    if not line:
                        self.end_reading()
                        return None
                    status = ord(line[0])
                    if not (status & exclude_flags):
                        self.read_pos = f.tell()
                        return (pos, status, line[1:].rstrip())
            self.end_reading()

    async def next_with_callback(self, exclude_flags=StatusFlags.SAVED | StatusFlags.SENT, callback=None): # Changed to async def
        """Retrieve the next line and execute a callback with the line data."""
        line_data = self.next(exclude_flags)
        if line_data and callback:
            try:
                # Assuming callback expects (pos, status, data)
                await callback(line_data) # If callback can be async
            except Exception as e:
                await p.print(f"Error in callback: {e}") # Changed to await p.print
                # Optionally, handle the error, e.g., log it or retry
        return line_data

    def update_status(self, pos, new_status):
        """Update the status flag at a specific byte position."""
        with self.lock:
            # print("update_status {}", self.filename_read)
            with open(self._file_path(self.filename_read), 'r+') as f:
                f.seek(pos)
                current = f.read(1)
                # print("Updating status {} to {}".format(current,new_status))
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

    async def machine(self): # Changed to async def
        if self.state == 0:
            self.state = 1
        elif self.state == 1:
            tmp = self.next()
            if not tmp is None:
                # await p.print("{} -> {}".format(self.filename_read, tmp)) # Would need p
                self.update_status(
                    tmp[0], StatusFlags.READY | StatusFlags.SAVED | StatusFlags.SENT)

# Import p for test function if it's going to use await p.print
from my_utilities import p

async def test_SimpleFileDB(): # Changed to async def
    db = SimpleFileDB(dir="./dbs", db_filename="test")
    # Initial save
    await db.save("test1", StatusFlags.READY) # Added await
    await db.save("test2", StatusFlags.READY) # Added await
    await db.save("test3", StatusFlags.READY) # Added await

    # # print(db.filename_write)
    # while True:
    #     tmp = db.next_with_callback(callback=print)
    #     if tmp == None:
    #         break
    #     await p.print(tmp)
    for i in range(100):
        await db.machine() # Added await
    # exit() # exit() is not typically used in async MicroPython like this

    # Read and print all
    await p.print("Initial data:") # Added await
    while True:
        tmp = db.next(exclude_flags=0x00)
        if tmp is None:
            break
        await p.print(tmp) # Added await

    # Update status of the second entry
    db.read_pos = 0
    count = 0
    while True:
        tmp = db.next(exclude_flags=0x00)
        if tmp is None:
            break
        if count == 1:  # Update the second entry
            db.update_status(tmp[0], StatusFlags.READY | StatusFlags.SAVED)
        count += 1


if __name__ == "__main__":
    # To run an async test function:
    # import uasyncio
    # uasyncio.run(test_SimpleFileDB())
    pass
