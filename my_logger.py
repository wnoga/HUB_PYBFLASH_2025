# my_json_logger.py
import json
import os
import time
import uasyncio

from my_utilities import VerbosityLevel, is_delay, is_timeout, millis, p, rtc_unix_timestamp, PreallocatedRingBuffer

class JSONLogger:
    """Asynchronous JSON-line structured logger designed for high-throughput MicroPython flash/SD storage."""

    def __init__(
        self,
        filename="log.json",
        parent_dir="/sd/logs",
        verbosity_level=VerbosityLevel["INFO"],
        keep_file_open=True,
    ):
        self.parent_dir = parent_dir
        self.verbosity_level = verbosity_level
        self.filename_org = filename
        self.filename = None
        self.file = None

        self.print_verbosity_level = VerbosityLevel["CRITICAL"]
        self.keep_file_open = keep_file_open
        self.rtc_synced = False

        # Queue Management
        self.log_queue_max_len = 128
        self.log_queue = PreallocatedRingBuffer(capacity=self.log_queue_max_len)
        # Pre-allocated temporary slot for dequeuing to prevent allocations during read
        self._dequeue_slot = [0, ""]
        self.writer_yield_ms = 50
        
        # Rate Limiting & Synchronization
        self.last_sync = 0
        self.sync_every_ms = 1000
        self.burst_delay_ms = 1
        self.burst_timestamp_ms = 0

        # File State Flags
        self._request_new_file = True
        self._request_rename_file = False
        self.request_print_last_lines = 0

        self.run = True
        self.file_rows = 0
        self.cursor_position_last = 0

    @micropython.native
    def _ensure_directory(self):
        try:
            if not self._path_exists(self.parent_dir):
                os.makedirs(self.parent_dir)
        except OSError as e:
            print("CRITICAL: Failed to create log directory {}: {}".format(self.parent_dir, e))

    @micropython.native
    def _path_exists(self, path):
        try:
            os.stat(path)
            return True
        except OSError:
            return False

    @micropython.native
    def _get_unique_filename(self, filename):
        base, ext = filename.rsplit(".", 1) if "." in filename else (filename, "")
        counter = 1
        current_target = filename
        while self._path_exists(current_target):
            ext_suffix = ".{}".format(ext) if ext else ""
            current_target = "{}_{}{}".format(base, counter, ext_suffix)
            counter += 1
        return current_target

    @micropython.native
    def _should_log(self, level):
        return level <= self.verbosity_level

    async def get_new_file_path(self):
        self._ensure_directory()
        filename_datetime = self.filename_org
        if self.rtc_synced:
            # Format filename using current RTC timestamp
            tm = time.gmtime()
            filename_datetime = "log_{:04d}{:02d}{:02d}_{:02d}{:02d}{:02d}.json".format(
                tm[0], tm[1], tm[2], tm[3], tm[4], tm[5]
            )
        await uasyncio.sleep_ms(0)
        full_path = "{}/{}".format(self.parent_dir, filename_datetime)
        return self._get_unique_filename(full_path)

    async def new_file(self):
        await self.close()
        self.filename = await self.get_new_file_path()
        self.cursor_position_last = 0
        self.file_rows = 0

        if self.keep_file_open:
            try:
                self.file = open(self.filename, "w")
                await uasyncio.sleep_ms(0)
            except Exception as e:
                await p.print("ERROR in new_file opening {}: {}".format(self.filename, e))
                self.file = None

        await p.print("New logger file target set to:", self.filename)

    async def _write_entry(self, level: int, message, chunk_size: int = 64):
        """Internal worker executing hardware file writes by streaming JSON keys directly."""
        if self.burst_delay_ms and is_delay(self.burst_timestamp_ms, self.burst_delay_ms):
            return 0
        self.burst_timestamp_ms = millis()

        if not self.filename:
            return 0

        opened_in_scope = False
        target_file = self.file

        if not self.keep_file_open or target_file is None:
            try:
                target_file = open(self.filename, "a")
                opened_in_scope = True
                await uasyncio.sleep_ms(0)
            except Exception as e:
                await p.print("ERROR opening file for append {}: {}".format(self.filename, e))
                return -1
            
        @micropython.native
        def _json_stream():
            yield '{"timestamp":'
            yield str(millis())
            yield ',"rtc_timestamp":'
            yield str(rtc_unix_timestamp())
            yield ',"level":'
            yield str(level)
            yield ',"message":'
            yield json.dumps(message)
            yield '}\n'

        try:
            self.cursor_position_last = target_file.tell()
            
            buffer = bytearray()
            for part in _json_stream():
                buffer.extend(part.encode())
                
                # Drain in fixed chunk_size slices without del
                while len(buffer) >= chunk_size:
                    mv = memoryview(buffer)
                    target_file.write(mv[:chunk_size])
                    # Reassign buffer to the remaining byte range
                    buffer = bytearray(mv[chunk_size:])
                    await uasyncio.sleep_ms(0)

            # Flush remaining buffer remnant
            if buffer:
                target_file.write(buffer)
                await uasyncio.sleep_ms(0)

            self.file_rows += 1

            if opened_in_scope:
                target_file.flush()
                target_file.close()
        except Exception as e:
            await p.print("ERROR writing to",self.filename, ":", e)
            if opened_in_scope and target_file:
                try:
                    target_file.close()
                except Exception:
                    pass
            elif self.keep_file_open:
                await self.close()
                self.request_new_file()
            return -1

        if level >= self.print_verbosity_level:
            print("LOG:", str(message))

        return 1

    async def log(self, level: int, message):
        """Asynchronously enqueues log records without dynamic memory allocations."""
        if self._should_log(level):
            # Non-blocking yield until buffer space opens up
            while self.log_queue.is_full():
                await uasyncio.sleep_ms(10)

            # In-place write to ring buffer
            self.log_queue.push(level, message)

    async def _process_log_queue(self):
        """Processes enqueued items using the pre-allocated read buffer."""
        if not self.log_queue.is_empty():
            # Pop next record directly into pre-allocated memory slot
            if self.log_queue.pop_into(self._dequeue_slot):
                level = self._dequeue_slot[0]
                message = self._dequeue_slot[1]

                if await self._write_entry(level, message) == 0:
                    # If write was throttled by burst limit, put record back or wait
                    pass

        await uasyncio.sleep_ms(0)
        return len(self.log_queue)
    
    async def sync(self):
        if self.file is not None and self.keep_file_open:
            try:
                self.file.flush()
                await uasyncio.sleep_ms(0)
                self.last_sync = millis()
            except Exception as e:
                await p.print("Error flushing log file: {}".format(e))

    async def sync_process(self):
        if is_timeout(self.last_sync, self.sync_every_ms):
            await self.sync()

    async def close(self):
        if self.file is not None:
            try:
                self.file.flush()
                self.file.close()
                await uasyncio.sleep_ms(0)
            except Exception as e:
                await p.print("Error closing file {}: {}".format(self.filename, e))
            finally:
                self.file = None

    async def clear_old_logs(self):
        """Deletes historical log files residing inside the target log directory."""
        if not self.parent_dir or not self.filename:
            return

        try:
            current_basename = self.filename.rsplit("/", 1)[-1]
            deleted_count = 0
            for file_name in os.listdir(self.parent_dir):
                if file_name != current_basename:
                    path_to_delete = "{}/{}".format(self.parent_dir, file_name)
                    try:
                        os.remove(path_to_delete)
                        deleted_count += 1
                        await uasyncio.sleep_ms(10)
                    except OSError as e:
                        await p.print("Error deleting log file {}: {}".format(path_to_delete, e))
            await p.print("Cleared {} old log file(s).".format(deleted_count))
        except Exception as e:
            await p.print("Error in clear_old_logs: {}".format(e))

    async def _print_last_lines(self, n_lines=10, path=None):
        target_path = path or self.filename
        if not target_path or not self._path_exists(target_path):
            await p.print("Cannot print last lines: File {} unavailable.".format(target_path))
            return

        if target_path == self.filename and self.keep_file_open:
            await self.sync()

        try:
            # Stream lines line-by-line without loading the whole file into RAM
            buf = []
            with open(target_path, "r") as f:
                for line in f:
                    buf.append(line.strip())
                    if len(buf) > n_lines:
                        buf.pop(0)  # Maintain rolling window of last N lines
                    await uasyncio.sleep_ms(0)

            for line in buf:
                await p.print(line)
                await uasyncio.sleep_ms(0)
        except Exception as e:
            await p.print("Error reading log file {}: {}".format(target_path, e))
            
    def print_last_lines(self, n_lines=1):
        self.request_print_last_lines = n_lines

    def request_new_file(self):
        self._request_new_file = True

    def request_rename_file(self):
        self._request_rename_file = True

    async def machine(self):
        """Main service loop tick function. Processes queues, state switches, and periodic disk flushes."""
        if self._request_rename_file:
            self._request_rename_file = False
            await self.sync()
            new_path = await self.get_new_file_path()
            if self.filename and self._path_exists(self.filename):
                await self.close()
                try:
                    os.rename(self.filename, new_path)
                    self.filename = new_path
                    await p.print("Logger renamed to", self.filename)
                except Exception as e:
                    await p.print("Rename error:",e)

                if self.keep_file_open:
                    try:
                        self.file = open(self.filename, "a")
                    except Exception as e:
                        await p.print("Reopen error after rename:", e)
                        self.file = None

        if self._request_new_file:
            self._request_new_file = False
            await self.new_file()

        if self.keep_file_open:
            await self.sync_process()

        if self.request_print_last_lines > 0:
            await self._print_last_lines(self.request_print_last_lines)
            self.request_print_last_lines = 0

        while await self._process_log_queue():
            await uasyncio.sleep_ms(0)

        await uasyncio.sleep_ms(self.writer_yield_ms)