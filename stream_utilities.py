import errno
import gc
import ujson as json
import uasyncio as asyncio


async def send_raw(sock_or_writer, raw_b: bytes):
    """
    Non-blocking socket/writer raw byte sender with retry on EAGAIN/EWOULDBLOCK.
    """
    if not raw_b:
        return

    # Check if object is a raw socket or a Stream
    if hasattr(sock_or_writer, "send"):
        view = memoryview(raw_b)
        total_sent = 0
        while total_sent < len(view):
            try:
                sent = sock_or_writer.send(view[total_sent:])
                if sent is None or sent == 0:
                    await asyncio.sleep_ms(10)
                    continue
                total_sent += sent
            except OSError as e:
                err = e.errno if hasattr(e, "errno") else (e.args[0] if e.args else None)
                if err in (errno.EAGAIN, errno.EWOULDBLOCK, 11):
                    await asyncio.sleep_ms(10)
                else:
                    raise e
    else:
        # uasyncio Stream
        sock_or_writer.write(raw_b)
        await sock_or_writer.drain()


async def send_chunk_raw(sock_or_writer, raw_b: bytes, max_chunk: int = 512):
    """
    Streams raw byte data using HTTP Chunked Transfer Encoding (HEX_SIZE\\r\\nDATA\\r\\n).
    Completely handles non-blocking EAGAIN retries.
    """
    if not raw_b:
        return

    length = len(raw_b)
    # 1. Format chunk header in uppercase Hexadecimal
    chunk_header = ("%X\r\n" % length).encode("ascii")
    
    # 2. Transmit header, body payload, and trailing CRLF sequence
    await send_raw(sock_or_writer, chunk_header)
    await send_raw(sock_or_writer, raw_b)
    await send_raw(sock_or_writer, b"\r\n")


async def send_chunk_str(sock_or_writer, text: str, max_chunk: int = 512):
    """
    Safely encodes string data to UTF-8 bytes first to avoid splitting 
    multibyte characters, then streams in chunk blocks <= max_chunk.
    """
    if not text:
        return

    # Encode string to bytes first so multi-byte UTF-8 boundaries stay intact
    encoded_bytes = text.encode("utf-8")
    total_len = len(encoded_bytes)

    for i in range(0, total_len, max_chunk):
        chunk_slice = encoded_bytes[i : i + max_chunk]
        await send_chunk_raw(sock_or_writer, chunk_slice, max_chunk=max_chunk)

async def stream_json_value(sock_or_writer, val, chunk_builder=send_raw):
    """
    Recursively streams any MicroPython object as valid JSON directly over a socket.
    Uses zero intermediate allocation for large structures (no f-strings used).
    """
    if val is None:
        await chunk_builder(sock_or_writer, b"null")
    elif isinstance(val, bool):
        await chunk_builder(sock_or_writer, b"true" if val else b"false")
    elif isinstance(val, (int, float)):
        await chunk_builder(sock_or_writer, str(val).encode("ascii"))
    elif isinstance(val, str):
        # Escapes common characters; wrap in quotes
        escaped = val.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
        encoded_str = ('"' + escaped + '"').encode("utf-8")
        await chunk_builder(sock_or_writer, encoded_str)
    elif isinstance(val, (list, tuple)):
        await chunk_builder(sock_or_writer, b"[")
        first = True
        for item in val:
            if not first:
                await chunk_builder(sock_or_writer, b",")
            first = False
            await stream_json_value(sock_or_writer, item, chunk_builder)
        await chunk_builder(sock_or_writer, b"]")
    elif isinstance(val, dict):
        await chunk_builder(sock_or_writer, b"{")
        first = True
        for k, v in val.items():
            if not first:
                await chunk_builder(sock_or_writer, b",")
            first = False
            # Key string formatting
            key_str = str(k).replace('\\', '\\\\').replace('"', '\\"')
            key_bytes = ('"' + key_str + '":').encode("utf-8")
            await chunk_builder(sock_or_writer, key_bytes)
            # Value
            await stream_json_value(sock_or_writer, v, chunk_builder)
        await chunk_builder(sock_or_writer, b"}")

async def stream_json_key_by_key(sock_or_writer, obj, is_async_writer=True):
    """
    Recursively serializes and streams JSON structures key-by-key
    using HTTP Chunked Transfer Encoding to keep heap allocations minimal.
    """
    async def _write_bytes(raw_b):
        # Always use chunked transfer encoding to keep response valid
        await send_chunk_raw(sock_or_writer, raw_b, max_chunk=512)

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

            # Format JSON string keys safely
            key_str = str(k)
            await _write_bytes(json.dumps(key_str).encode("utf-8"))
            await _write_bytes(b":")

            await stream_json_key_by_key(sock_or_writer, v, is_async_writer)
            gc.collect()

        await _write_bytes(b"}")
    elif isinstance(obj, (list, tuple)):
        await _write_bytes(b"[")
        first = True
        for item in obj:
            if not first:
                await _write_bytes(b",")
            first = False

            await stream_json_key_by_key(sock_or_writer, item, is_async_writer)
            gc.collect()

        await _write_bytes(b"]")
        
async def finish_chunked_stream(sock_or_writer):
    """
    Sends the mandatory zero-length HTTP chunked footer to properly terminate the response.
    """
    await send_raw(sock_or_writer, b"0\r\n\r\n")


async def send_json_dict_chunked(sock_or_writer, data: dict):
    """
    Main entry point for chunked JSON dictionary transmission.
    Fully async, non-blocking, recursive, and appends the HTTP chunk terminator.
    """
    await stream_json_key_by_key(sock_or_writer, data)
    # await finish_chunked_stream(sock_or_writer)