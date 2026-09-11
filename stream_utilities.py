# stream_utilities.py
import errno
import gc
import json
import uasyncio as asyncio


# =========================================================================
# LOW-RAM RAW SOCKET HELPERS (ASYNC NON-BLOCKING)
# =========================================================================

async def send_raw(sock, data: bytes):
    """Sends raw bytes over a non-blocking socket without high RAM allocation."""
    total_sent = 0
    view = memoryview(data)
    while total_sent < len(view):
        try:
            sent = sock.send(view[total_sent:])
            if sent == 0 or sent is None:
                raise OSError("Socket closed by peer")
            total_sent += sent
        except OSError as e:
            err = e.errno if hasattr(e, "errno") else (e.args[0] if e.args else None)
            if err in (errno.EAGAIN, errno.ETIMEDOUT, 11, 110):
                await asyncio.sleep_ms(10)
                continue
            raise e


async def send_chunk_raw(writer, data: bytes, max_chunk: int = 512):
    """Streams byte data using HTTP chunked encoding in max_chunk byte blocks."""
    if not data:
        return

    view = memoryview(data)
    total_len = len(view)
    offset = 0

    while offset < total_len:
        chunk_len = min(max_chunk, total_len - offset)
        sub_chunk = view[offset : offset + chunk_len]

        # HTTP Chunk header (<hex_size>\r\n)
        header = ("%X\r\n" % chunk_len).encode("ascii")
        writer.write(header)
        writer.write(sub_chunk)
        writer.write(b"\r\n")
        await writer.drain()

        offset += chunk_len


async def send_chunk_str(writer, text: str, max_chunk: int = 512):
    """Encodes and streams string data in controlled chunk sizes."""
    if not text:
        return

    for i in range(0, len(text), max_chunk):
        slice_str = text[i : i + max_chunk]
        data = slice_str.encode("utf-8")
        await send_chunk_raw(writer, data, max_chunk=max_chunk)


async def stream_json_key_by_key(writer_or_sock, obj, is_async_writer=True):
    """
    Recursively serializes and streams JSON structures key-by-key
    to keep memory allocations minimal on constrained MicroPython devices.
    """
    async def _write_bytes(raw_b):
        if is_async_writer:
            await send_chunk_raw(writer_or_sock, raw_b, max_chunk=512)
        else:
            await send_raw(writer_or_sock, raw_b)

    if obj is None:
        await _write_bytes(b"null")
    elif isinstance(obj, bool):
        await _write_bytes(b"true" if obj else b"false")
    elif isinstance(obj, (int, float)):
        await _write_bytes(str(obj).encode("ascii"))
    elif isinstance(obj, str):
        await _write_bytes(json.dumps(obj).encode("utf-8"))
    elif isinstance(obj, dict):
        await _write_bytes(b"{")
        first = True
        for k, v in obj.items():
            if not first:
                await _write_bytes(b",")
            first = False

            await _write_bytes(json.dumps(str(k)).encode("utf-8"))
            await _write_bytes(b":")

            await stream_json_key_by_key(writer_or_sock, v, is_async_writer)
            gc.collect()

        await _write_bytes(b"}")
    elif isinstance(obj, (list, tuple)):
        await _write_bytes(b"[")
        first = True
        for item in obj:
            if not first:
                await _write_bytes(b",")
            first = False

            await stream_json_key_by_key(writer_or_sock, item, is_async_writer)
            gc.collect()

        await _write_bytes(b"]")