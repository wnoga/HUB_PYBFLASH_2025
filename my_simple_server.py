import network
import usocket as socket
import time
import select
import _thread
import ujson
from HUB import HUBDevice
import pyb

from my_utilities import wdt
from my_utilities import millis
from my_utilities import P as p

class MySimpleServer():
    def __init__(self, hub: HUBDevice, static_ip=None):
        self.lan = network.LAN()
        self.port = 5555
        self.s = None
        self.t = None
        self.running = False  # Add running flag to control server loop
        self.host_ip = None
        # self.can, self.hub = initialize_can_hub()
        self.hub = hub
        # self.hub.afe_devices_max = 1
        # self.hub.discovery_active = True
        # self.hub.rx_process_active = True
        # self.hub.use_tx_delay = True
        # self.hub.afe_manage_active = True
        self.static_ip = static_ip
        self.server_socket = None
        self.connecions = []
        self.timestamp_ms = 0
        self.wait_ms = 100
        
        self.setup_lan_state = 0

    def setup_lan_machine(self):
        try:
            if self.setup_lan_state == 0:
                self.lan = network.LAN()
                self.lan.active(True)
                if self.static_ip:
                    self.lan.ifconfig((self.static_ip, '255.255.255.0', '192.168.1.1', '8.8.8.8'))
                else:
                    self.lan.ifconfig('dhcp')
                timestamp_ms = millis()
                self.setup_lan_state = 1
            elif self.setup_lan_state == 1:
                if self.lan.isconnected():
                    self.setup_lan_state = 2
                    p.print("Connected to LAN: {}".format(self.lan.ifconfig()))
                else:
                    if (millis() - timestamp_ms) > 15000:
                        self.setup_lan_state = 0
            elif self.setup_lan_state == 2:
                if not self.lan.isconnected():
                    self.setup_lan_state = 0 # Recconect
            else:
                self.setup_lan_state = 0
        except:
            self.setup_lan_state = 0

    def handle_client(self, connection: socket.socket, address: tuple):
        p.print("New connection from {}".format(address))
        connection.settimeout(5.0)
        try:
            data = connection.recv(1024).decode()
            if not data:
                return
            p.print("Received: {}".format(data))
            try:
                j = ujson.loads(data)
                # afe_id = j["afe_id"]
                afe_id = 35
                if "procedure" in j:
                    p.print("Procedure: {}".format(j["procedure"]))
                    afe_id = int(j["afe_id"])
                    afe = self.hub.get_afe_by_id(afe_id)
                    if afe is None:
                        pass
                    else:
                        if j["procedure"] == "default_full":
                            self.hub.default_full(afe_id)
                            j["status"] = "OK"
                            connection.sendall(ujson.dumps(j).encode())
                            return
                        if j["procedure"] == "default_get_measurement_last":
                            timestamp_ms = millis()
                            x = True
                            def cb(msg=None):
                                x = False
                                print("EXECUTED CALLBACK ON SERVER: {}".format(msg))
                                my_dict = msg.copy()
                                if 'frame' in my_dict:
                                    my_dict.pop('frame')
                                if 'callback' in my_dict:
                                    my_dict.pop('callback')

                                connection.sendall(ujson.dumps(my_dict).encode())
                                return my_dict
                            self.hub.default_get_measurement_last(afe_id,callback=cb)
                            while x:
                                if (millis()-timestamp_ms > 5000):
                                    return None
                            j["status"] = "OK"
                        else:
                            j["status"] = "ERROR"
                            j["status_info"] = "Not implemented"
                            connection.sendall(ujson.dumps(j).encode())
                            return
                if "command" in j:
                    if j["command"] == "get_data":
                        self.hub.send_back_data(afe_id)
                    elif j["command"] == "default_procedure":                            
                        self.hub.default_procedure(afe_id)
                    elif j["command"] == "default_set_dac":
                        self.hub.default_set_dac(afe_id)
                    elif j["command"] == "default_get_measurement":
                        self.hub.default_get_measurement(afe_id)
                    elif j["command"] == "default_hv_set":
                        self.hub.default_hv_set(afe_id,True)
                    elif j["command"] == "default_hv_off":
                        self.hub.default_hv_set(afe_id,False)
                    elif j["command"] == "test4":
                        self.hub.test4(afe_id)
                    elif j["command"] == "test3":
                        self.hub.test3(afe_id)
                    elif j["command"] == "test2":
                        self.hub.test2(afe_id)
                    elif j["command"] == "test1":
                        self.hub.test1(afe_id)
            except Exception as e:
                p.print("Error: {}".format(e))
            connection.sendall(ujson.dumps({"status":"OK"}).encode())
        except OSError as e:
            if e.args[0] == 110:  # ETIMEDOUT
                p.print("Connection timed out from {}".format(address))
            else:
                p.print("Error handling client from {}: {}".format(address, e))
        except Exception as e:
            p.print("Error handling client from {}: {}".format(address, e))
        finally: # Avoid deadlock
            connection.close()
            p.print("Connection closed from {}".format(address))
    
    def setup_socket(self):
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.settimeout(2.0)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind((self.lan.ifconfig()[0], int(self.port)))
            self.server_socket.listen(1)
        except Exception as e:
            self.server_socket = None
            p.print("Error setting up server socket: {}".format(e))
    
    def main_machine(self):
        if (millis() - self.timestamp_ms) < self.wait_ms:
            return
        self.timestamp_ms = millis()
        # p.print("MAIN MACHINE SERVER")
        # p.print(self.lan.ifconfig())
        self.setup_lan_machine()
        if self.server_socket is None:
            self.setup_socket()
            pass
        try:
            self.server_socket.settimeout(0.01)
            connection, address = self.server_socket.accept()
            # _thread.start_new_thread(self.handle_client, (connection, address,))
            self.handle_client(connection,address)
            # self.connecions.append({"connection":connection,"address":address})
        except:
            pass
        finally:
            try:
                connection.close()
            except:
                pass
            
    
    def main_loop(self):
        while self.running:
            self.main_machine()
            wdt.feed()
                
    def start_server(self):
        self.lan = self.setup_lan()
        self.running = True
        # self.run()
        self.run_thread = _thread.start_new_thread(self.run, ())
        
    def x(self):
        while True:
            pyb.delay(100)
            # p.print("TEST")
