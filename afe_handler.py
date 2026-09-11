import uasyncio as asyncio
import ujson

from stream_utilities import send_raw, stream_json_key_by_key, stream_json_value
from my_utilities import p
import gc

json = ujson


class AFEProcedureHandler:
    def __init__(self, hub, max_procedures_keep_len=32):
        self.hub = hub
        self.max_procedures_keep_len = max_procedures_keep_len
        self.procedure_results = {}
        self.procedure_events = {}

    async def hub_cb(self, msg: dict = None):
        if not msg or "device_id" not in msg:
            await p.print("hub_cb: device_id missing in callback message.")
            return

        device_id = msg["device_id"]
        my_dict = {k: v for k, v in msg.items() if k not in ("frame", "callback")}

        if device_id not in self.procedure_results and len(self.procedure_results) >= self.max_procedures_keep_len:
            await p.print("AFEProcedureHandler: procedure_results cache full. Evicting item.")
            try:
                self.procedure_results.pop(next(iter(self.procedure_results)))
            except (StopIteration, KeyError):
                pass

        try:
            self.procedure_results[device_id] = ujson.dumps(my_dict).encode()
        except Exception as e:
            await p.print("hub_cb: Error serializing result for AFE {}: {}".format(device_id, e))
            self.procedure_results[device_id] = ujson.dumps(
                {"status": "ERROR", "info": "Failed to serialize response"}
            ).encode()

        event = self.procedure_events.get(device_id)
        if event:
            event.set()

    async def _execute_afe_procedure(self, request_json, hub_method_name, timeout_duration, required_params_map=None):
        await p.print("_execute_afe_procedure", request_json)
        afe_id = request_json.get("afe_id", None)
        if afe_id is None:
            return ujson.dumps({"status": "ERROR", "info": "afe_id missing"}).encode()

        hub_call_kwargs = {"callback": self.hub_cb}
        if required_params_map:
            for param_name, converter_func in required_params_map.items():
                param_value_str = request_json.get(param_name, None)
                if param_value_str is None:
                    return ujson.dumps({"status": "ERROR", "info": "Parameter '{}' missing".format(param_name)}).encode()
                try:
                    hub_call_kwargs[param_name] = converter_func(param_value_str)
                except ValueError:
                    return ujson.dumps({"status": "ERROR", "info": "Invalid value for parameter '{}'".format(param_name)}).encode()

        if afe_id not in self.procedure_events and len(self.procedure_events) >= self.max_procedures_keep_len:
            return ujson.dumps({"status": "ERROR", "info": "Server busy, max concurrent procedures reached"}).encode()

        event = asyncio.Event()
        self.procedure_events[afe_id] = event
        self.procedure_results.pop(afe_id, None)

        try:
            hub_method_ref = getattr(self.hub, hub_method_name)
        except AttributeError:
            return ujson.dumps({"status": "ERROR", "info": "Procedure handle missing"}).encode()

        async def _execute_and_wait():
            await p.print("_execute_and_wait")
            await hub_method_ref(afe_id=afe_id, **hub_call_kwargs)
            await event.wait()

        try:
            await asyncio.wait_for(_execute_and_wait(), timeout=timeout_duration)
            result_data = self.procedure_results.pop(afe_id, None)
            await p.print("!!", result_data)
            return result_data if result_data else ujson.dumps({"status": "OK"}).encode()
        except asyncio.TimeoutError:
            await p.print("Timeout executing '{}' for AFE {}".format(hub_method_name, afe_id))
            return ujson.dumps({"status": "ERROR", "info": "Timeout waiting for AFE response"}).encode()
        except Exception as e:
            await p.print("Error executing '{}' for AFE {}: {}".format(hub_method_name, afe_id, e))
            return ujson.dumps({"status": "ERROR", "info": "Server execution error"}).encode()
        finally:
            self.procedure_events.pop(afe_id, None)
            self.procedure_results.pop(afe_id, None)

    async def handle_procedure_raw(self, request_bytes, sock):
        """Processes request payload line and writes JSON responses directly to raw socket."""
        try:
            request_json = ujson.loads(request_bytes)
        except (ValueError, UnicodeError):
            await send_raw(sock, b'{"status":"ERROR","info":"Invalid JSON"}\r\n')
            return

        procedure = request_json.get("procedure")
        if not procedure:
            await send_raw(sock, b'{"status":"ERROR","info":"Procedure missing"}\r\n')
            return

        elif procedure == "get_all_afe_configuration":
            await send_raw(sock, b"{")
            first = True
            
            for afe_device in self.hub.afe_devices:
                if not first:
                    await send_raw(sock, b",")
                first = False
                
                # Format key using direct string concatenation
                key_bytes = ('"' + str(afe_device.device_id) + '":').encode("utf-8")
                await send_raw(sock, key_bytes)
                
                # Stream configuration dict without ujson.dumps
                await stream_json_value(sock, afe_device.configuration, chunk_builder=send_raw)
                
                gc.collect()
                
            await send_raw(sock, b"}\r\n")
            
        elif procedure == "get_all_latest_status":
            await send_raw(sock, b"{")
            first = True
            
            for afe_device in self.hub.afe_devices:
                if not first:
                    await send_raw(sock, b",")
                first = False
                
                # Format key using direct string concatenation
                key_bytes = ('"' + str(afe_device.device_id) + '":').encode("utf-8")
                await send_raw(sock, key_bytes)
                
                # Stream configuration dict without ujson.dumps
                await stream_json_value(sock, afe_device.latest_status, chunk_builder=send_raw)
                
                gc.collect()
                
            await send_raw(sock, b"}\r\n")
            
        elif procedure == "get_all_afe_id":
            ids = [str(afe.device_id) for afe in self.hub.afe_devices]
            res = '{"available_afe":[' + ",".join(ids) + "]}\r\n"
            await send_raw(sock, res.encode())

        elif procedure == "hub_close_all":
            await self.hub.close_all()
            await send_raw(sock, b'{"status":"OK"}\r\n')

        elif procedure == "hub_clear_old_logs":
            await self.hub.clear_old_logs()
            await send_raw(sock, b'{"status":"OK"}\r\n')

        elif procedure == "hub_powerOn":
            await self.hub.powerOn()
            await send_raw(sock, b'{"status":"OK"}\r\n')

        elif procedure == "hub_powerOff":
            await self.hub.powerOff()
            await send_raw(sock, b'{"status":"OK"}\r\n')

        elif procedure == "default_get_measurement_last":
            res = await self._execute_afe_procedure(request_json, "default_get_measurement_last", 20.0)
            await send_raw(sock, res + b"\r\n")

        elif procedure == "afe_configure":
            res = await self._execute_afe_procedure(request_json, "default_configure_afe", 60.0)
            await send_raw(sock, res + b"\r\n")

        elif procedure == "afe_reset":
            res = await self._execute_afe_procedure(request_json, "reset", 10.0)
            await send_raw(sock, res + b"\r\n")

        elif procedure == "afe_set_dac":
            res = await self._execute_afe_procedure(
                request_json, "default_set_dac", 10.0, required_params_map={"dac_master": int, "dac_slave": int}
            )
            await send_raw(sock, res + b"\r\n")

        elif procedure == "afe_set_sipm_voltage_si":
            res = await self._execute_afe_procedure(
                request_json,
                "afe_set_sipm_voltage_si",
                10.0,
                required_params_map={"voltage": float, "afe_subdevice": int},
            )
            await send_raw(sock, res + b"\r\n")

        else:
            await send_raw(sock, b'{"status":"ERROR","info":"Unknown procedure"}\r\n')