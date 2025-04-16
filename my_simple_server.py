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

class MySimpleServer():
    def __init__(self, hub: HUBDevice, lock: _thread.allocate_lock, static_ip=None):
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
        self.lock = lock
        
        self.setup_lan_state = 0

    def setup_lan_machine(self):
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
                print("Connected to LAN: {}".format(self.lan.ifconfig()))
            else:
                if (millis() - timestamp_ms) > 15000:
                    self.setup_lan_state = 0
        elif self.setup_lan_state == 2:
            if not self.lan.isconnected():
                self.setup_lan_state = 0 # Recconect
        else:
            self.setup_lan_state = 0

    def handle_client(self, connection: socket.socket, address: tuple):
        print("New connection from {}".format(address))
        connection.settimeout(5.0)
        try:
            data = connection.recv(1024).decode()
            if not data:
                return
            print("Received: {}".format(data))
            try:
                j = ujson.loads(data)
                if "command" in j:
                    if j["command"] == "get_data":
                        self.hub.send_back_data(35)
                    elif j["command"] == "default_procedure":                            
                        self.hub.default_procedure(35)
                    elif j["command"] == "default_set_dac":
                        self.hub.default_set_dac(35)
                    elif j["command"] == "default_get_measurement":
                        self.hub.default_get_measurement(35)
                    elif j["command"] == "default_hv_set":
                        self.hub.default_hv_set(35,True)
                    elif j["command"] == "default_hv_off":
                        self.hub.default_hv_set(35,False)
                    elif j["command"] == "test4":
                        self.hub.test4(35)
                    elif j["command"] == "test3":
                        self.hub.test3(35)
                    elif j["command"] == "test2":
                        self.hub.test2(35)
                    elif j["command"] == "test1":
                        self.hub.test1(35)
            except Exception as e:
                print("Error: {}".format(e))
            connection.sendall(ujson.dumps({"status":"OK"}).encode())
        except OSError as e:
            if e.args[0] == 110:  # ETIMEDOUT
                print("Connection timed out from {}".format(address))
            else:
                print("Error handling client from {}: {}".format(address, e))
        except Exception as e:
            print("Error handling client from {}: {}".format(address, e))
        finally: # Avoid deadlock
            connection.close()
            print("Connection closed from {}".format(address))
    
    def setup_socket(self):
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.settimeout(2.0)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind((self.lan.ifconfig()[0], int(self.port)))
            self.server_socket.listen(1)
            print("Server started on: {}:{}}".format(self.lan.ifconfig()[0],int(self.port)))
        except Exception as e:
            self.server_socket = None
            print("Error setting up server socket: {}", e)
    
    def main_machine(self):
        if (millis() - self.timestamp_ms) < self.wait_ms:
            return
        self.timestamp_ms = millis()
        with self.lock:
            self.setup_lan_machine()
            if self.server_socket is None:
                self.setup_socket()
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
            
    
    def run(self):
        while self.running:
            self.main_machine(self.lock)
            wdt.feed()
                
    def start_server(self):
        self.lan = self.setup_lan()
        self.running = True
        # self.run()
        self.run_thread = _thread.start_new_thread(self.run, ())
        
    def x(self):
        while True:
            pyb.delay(100)
            # print("TEST")
