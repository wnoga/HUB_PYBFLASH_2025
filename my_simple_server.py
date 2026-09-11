import errno
import gc
import network
import socket
import struct
import time
import uasyncio as asyncio
import ujson
import uos
import uselect
import utime

from HUB import HUBDevice
from afe_handler import AFEProcedureHandler
import my_utilities
from my_utilities import (
    is_timeout,
    millis,
    p,
    rtc,
    rtc_datetime_pretty,
    wdt,
)
from stream_utilities import (
    send_chunk_raw,
    send_chunk_str,
    send_raw,
    stream_json_key_by_key,
)

json = ujson

NTP_DELTA = 2208988800
NTP_HOST = "pool.ntp.org"


class AsyncWebServer:
    def __init__(self, hub: HUBDevice, dhcp=True, static_ip_config=None, port=5555):
        self.hub = hub
        self.afe_handler = AFEProcedureHandler(hub)
        self.dhcp = dhcp
        self.static_ip_config = static_ip_config
        self.port = port

        self.server_sock = None
        self.lan = network.LAN()
        self.lan_connected = False
        self.last_lan_check_ms = 0

        self.ntp_synced = False
        self.main_loop_yield_wait_ms = 10
        self.sync_ntp_loop_yield_wait_s = 10
        self.sync_ntp_every_s = 60

        self.poller = uselect.poll()
        self.BUFFER_SIZE = 512
        self.client_sockets = {}
        self.sock_map = {}
        self.tcp_requests_max = 10
        self.CLIENT_TIMEOUT_S = 5

        self.poll_task = None
        self.ntp_task = None
        self.RESPONSE_SERVER_BUSY = b'{"status":"ERROR","info":"Server busy"}\r\n'

    def sort_log_files(self, file_list):
        """
        Sorts log files from latest to oldest.
        Handles:
        - Timestamped: log_YYYYMMDD_HHMMSS.json
        - Unsynced indexed: log_1.json, log_2.json
        - Unsynced base: log.json
        """
        def file_sort_key(filename):
            if filename == "log.json":
                return 1, 0
            if filename.startswith("log_") and filename.endswith(".json"):
                core = filename[4:-5]
                if core.isdigit():
                    return 2, int(core)
                return 3, core
            return 0, filename

        return sorted(file_list, key=file_sort_key, reverse=True)

    def _close_client(self, client_sock):
        """Unregisters socket from poller and safely cleans up references."""
        sock_id = id(client_sock)
        if sock_id in self.client_sockets:
            del self.client_sockets[sock_id]
        if sock_id in self.sock_map:
            del self.sock_map[sock_id]

        try:
            self.poller.unregister(client_sock)
        except Exception:
            pass

        try:
            client_sock.close()
        except Exception:
            pass

    def get_webpage_address(self):
        if self.lan_connected:
            return "http://{}:{}".format(self.lan.ifconfig()[0], self.port)
        return None

    async def print_webpage_address(self):
        await p.print(self.get_webpage_address())

    async def connect_ethernet(self):
        self.lan.active(True)
        if not self.dhcp and self.static_ip_config:
            ip, subnet, gateway, dns = self.static_ip_config
            self.lan.ifconfig((ip, subnet, gateway, dns))

        await p.print("Waiting for Ethernet connection...")
        timeout = 10
        while not self.lan.isconnected() and timeout > 0:
            await asyncio.sleep(1)
            timeout -= 1

        if not self.lan.isconnected():
            raise RuntimeError("Ethernet connection failed")

        await p.print("Ethernet connected. IP:", self.lan.ifconfig()[0])
        self.lan_connected = True

    async def _close_stream_or_socket(self, obj):
        """Close either a raw socket or a uasyncio Stream."""
        if obj is None:
            return
        try:
            obj.close()
        except Exception:
            pass

        if not hasattr(obj, "send"):
            try:
                await obj.wait_closed()
            except Exception:
                pass

    async def _send_http_error(self, writer, code, message):
        """Send a small HTTP error through either raw socket or uasyncio writer."""
        try:
            body = ("<h1>%d %s</h1>" % (code, message)).encode("ascii")
            header = (
                "HTTP/1.1 %d %s\r\nContent-Type: text/html\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                % (code, message, len(body))
            ).encode("ascii")

            if hasattr(writer, "send"):
                await send_raw(writer, header)
                await send_raw(writer, body)
            else:
                writer.write(header)
                writer.write(body)
                await writer.drain()
        except OSError:
            pass
        finally:
            await self._close_stream_or_socket(writer)

    async def _recv_into_buffer(self, sock, buf, offset):
        """Receive directly into an existing bytearray using MicroPython's socket.readinto()."""
        available = len(buf) - offset
        if available <= 0:
            return 0
        try:
            view = memoryview(buf)[offset:]
            nread = sock.readinto(view)
            if nread is None:
                return -1
            if nread == 0:
                return 0
            return nread
        except OSError as e:
            err = e.errno if hasattr(e, "errno") else e.args[0] if e.args else None
            if err in (errno.EAGAIN, errno.EWOULDBLOCK, 11):
                return -1
            raise

    def _accept_client(self):
        """Accept a client and register one fixed-size receive buffer."""
        try:
            client_sock, client_addr = self.server_sock.accept()
            if len(self.client_sockets) >= self.tcp_requests_max:
                try:
                    client_sock.send(self.RESPONSE_SERVER_BUSY)
                except OSError:
                    pass
                try:
                    client_sock.close()
                except OSError:
                    pass
                return

            client_sock.setblocking(False)
            sock_id = id(client_sock)
            buf = bytearray(self.BUFFER_SIZE)
            self.client_sockets[sock_id] = {
                "sock": client_sock,
                "addr": client_addr,
                "buf": buf,
                "length": 0,
                "start_time": utime.ticks_ms(),
                "task": None,
            }
            self.sock_map[sock_id] = client_sock
            self.poller.register(client_sock, uselect.POLLIN)
        except OSError:
            pass

    def _find_newline_and_length(self, buf, length):
        for i in range(length):
            if buf[i] == 10:
                return i + 1
        return -1

    async def _process_client_read(self, client_sock, client_info):
        """Read and dispatch one HTTP or procedure request using only the raw socket."""
        try:
            gc.collect()
            buf = client_info["buf"]
            length = client_info["length"]

            while True:
                newline_pos = self._find_newline_and_length(buf, length)
                if newline_pos >= 0:
                    request_line_len = newline_pos
                    break
                if length >= len(buf):
                    await self._send_http_error(client_sock, 400, "Request line too long")
                    return

                nread = await self._recv_into_buffer(client_sock, buf, length)
                if nread == 0:
                    return
                if nread < 0:
                    await asyncio.sleep_ms(5)
                    continue

                length += nread
                client_info["length"] = length
                await asyncio.sleep_ms(0)

            request_line_end = request_line_len
            while request_line_end > 0 and buf[request_line_end - 1] in (10, 13):
                request_line_end -= 1

            first_space = -1
            second_space = -1
            for i in range(request_line_end):
                if buf[i] == 32:
                    if first_space < 0:
                        first_space = i
                    else:
                        second_space = i
                        break

            try:
                procedure_json = json.loads(buf)
                await p.print(procedure_json)
                await self.afe_handler.handle_procedure_raw(buf, client_sock)
                return
            except Exception:
                pass

            if first_space <= 0 or second_space <= first_space + 1:
                await self._send_http_error(client_sock, 400, "Bad Request")
                return

            method = bytes(buf[:first_space])
            request_path = bytes(buf[first_space + 1 : second_space])

            try:
                method = method.decode("ascii")
                request_path = request_path.decode("utf-8")
            except (UnicodeError, ValueError):
                await self._send_http_error(client_sock, 400, "Invalid Request")
                return

            header_end = bytes(buf).find(b"\r\n\r\n", request_line_len)
            while header_end < 0:
                if length >= len(buf):
                    await self._send_http_error(client_sock, 400, "Headers Too Large")
                    return
                nread = await self._recv_into_buffer(client_sock, buf, length)
                if nread == 0:
                    return
                if nread < 0:
                    await asyncio.sleep_ms(5)
                    continue

                length += nread
                client_info["length"] = length
                header_end = bytes(buf).find(b"\r\n\r\n", request_line_len)
                await asyncio.sleep_ms(0)

            await p.print("@", method, "->", request_path)

            if method == "GET":
                if request_path.startswith("/download_log"):
                    await self.handle_log_download(request_path, None, client_sock)
                elif request_path in ("/", "/index.html"):
                    await self.send_control_web_page_raw(None, client_sock)
                else:
                    await self._send_http_error(client_sock, 404, "Not Found")
            else:
                await self._send_http_error(client_sock, 405, "Method Not Allowed")

        except OSError:
            pass
        except Exception as e:
            try:
                line_number = e.__traceback__.tb_lineno
                await p.print(line_number, "Client socket processing error:", e)
            except Exception:
                pass
        finally:
            self._close_client(client_sock)
            gc.collect()

    async def socket_poll_task(self):
        """Main non-blocking event loop driven by uselect.poll()."""
        while self.lan_connected and self.server_sock:
            try:
                events = self.poller.poll(0)
                for obj, event in events:
                    if obj == self.server_sock:
                        self._accept_client()
                        continue

                    sock_id = id(obj)
                    client_info = self.client_sockets.get(sock_id)
                    if client_info is None:
                        continue

                    if event & (uselect.POLLHUP | uselect.POLLERR):
                        self._close_client(obj)
                        continue

                    if event & uselect.POLLIN:
                        task = client_info.get("task")
                        if task is None:
                            try:
                                self.poller.unregister(obj)
                            except OSError:
                                pass
                            task = asyncio.create_task(self._process_client_read(obj, client_info))
                            client_info["task"] = task

                current_ms = utime.ticks_ms()
                timeout_ms = getattr(self, "CLIENT_TIMEOUT_S", 5) * 1000
                for sock_id, client_info in list(self.client_sockets.items()):
                    if not isinstance(client_info, dict):
                        continue
                    sock = client_info.get("sock")
                    start_time = client_info.get("start_time", current_ms)
                    if sock is not None and utime.ticks_diff(current_ms, start_time) > timeout_ms:
                        self._close_client(sock)

            except Exception as e:
                try:
                    await p.print("Error in socket_poll_task:", e)
                except Exception:
                    pass

            await asyncio.sleep_ms(getattr(self, "main_loop_yield_wait_ms", 20))

    async def sync_rtc_with_ntp(self):
        global rtc_synced, p, rtc
        if not self.lan_connected:
            return False

        await p.print("Attempting NTP sync...")
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(3)
            addr = socket.getaddrinfo(NTP_HOST, 123)[0][-1]
            msg = bytearray(48)
            msg[0] = 27
            s.sendto(msg, addr)
            start_t = time.ticks_ms()
            data = None

            while time.ticks_diff(time.ticks_ms(), start_t) < 3000:
                try:
                    data, _ = s.recvfrom(48)
                    if data:
                        break
                except OSError:
                    await asyncio.sleep_ms(100)

            if data:
                secs = struct.unpack("!I", data[40:44])[0]
                unix_secs = secs - NTP_DELTA
                tm = time.gmtime(unix_secs - 946684800)
                rtc.datetime((tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))
                self.ntp_synced = True
                rtc_synced = True

                if not self.hub.logger.rtc_synced:
                    self.hub.logger.rtc_synced = True
                    self.hub.logger.request_rename_file()
                return True

        except Exception as e:
            await p.print("NTP sync failed:", e)
            return False
        finally:
            if s:
                s.close()

    async def sync_ntp_loop(self):
        """Periodically syncs RTC with NTP."""
        while True:
            is_synced = await self.sync_rtc_with_ntp()
            if is_synced:
                await asyncio.sleep(self.sync_ntp_every_s)
            else:
                await asyncio.sleep(self.sync_ntp_loop_yield_wait_s)

    async def start(self):
        """Main manager loop for socket server lifecycle and connection health."""
        while True:
            try:
                wdt.feed()
                if not self.lan_connected:
                    await p.print("Attempting to reconnect Ethernet...")
                    try:
                        await self.connect_ethernet()
                    except (RuntimeError, Exception) as e:
                        await p.print("Ethernet connection failed. Retrying in 10s.")
                        for _ in range(10):
                            wdt.feed()
                            await asyncio.sleep(1)
                        continue

                if self.server_sock is None:
                    await p.print("Attempting to start server...")
                    try:
                        gc.collect()
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        sock.setblocking(False)
                        sock.bind(("0.0.0.0", getattr(self, "port", 80)))
                        sock.listen(5)
                        self.server_sock = sock
                        self.poller.register(self.server_sock, uselect.POLLIN)

                        if hasattr(self, "poll_task") and self.poll_task:
                            self.poll_task.cancel()
                            self.poll_task = None

                        self.poll_task = asyncio.create_task(self.socket_poll_task())
                        if not getattr(self, "ntp_task", None):
                            self.ntp_task = asyncio.create_task(self.sync_ntp_loop())

                        ip = self.lan.ifconfig()[0] if hasattr(self, "lan") else "0.0.0.0"
                        await p.print("Server running at http://%s:%d" % (ip, self.port))
                    except Exception as e:
                        await p.print("Failed to start server. Retrying in 10s.")
                        if "sock" in locals() and sock:
                            try:
                                sock.close()
                            except OSError:
                                pass
                        self.server_sock = None
                        for _ in range(10):
                            wdt.feed()
                            await asyncio.sleep(1)
                        continue

                current_ms = getattr(self, "millis", utime.ticks_ms)()
                last_check = getattr(self, "last_lan_check_ms", 0)

                if utime.ticks_diff(current_ms, last_check) >= 5000:
                    if hasattr(self, "lan") and not self.lan.isconnected():
                        await p.print("Ethernet disconnected.")
                        self.lan_connected = False
                        if self.server_sock:
                            try:
                                self.poller.unregister(self.server_sock)
                            except (KeyError, OSError):
                                pass
                            try:
                                self.server_sock.close()
                            except OSError:
                                pass
                            self.server_sock = None

                        if getattr(self, "poll_task", None):
                            self.poll_task.cancel()
                            self.poll_task = None
                    self.last_lan_check_ms = current_ms

            except Exception as e:
                await p.print("AsyncWebServer main loop error:", e)

            wdt.feed()
            await asyncio.sleep_ms(getattr(self, "main_loop_yield_wait_ms", 50))

    def run(self):
        try:
            asyncio.run(self.start())
        except KeyboardInterrupt:
            print("Server stopped")