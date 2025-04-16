import network
import usocket as socket
import time
import select
import _thread
import ujson

class MyServer():
    def __init__(self, hub):
        self.lan = network.LAN()
        self.s = None
        self.t = None
        self.running = False  # Add running flag to control server loop
        self.hub = hub
        self.host_ip = None
        self.threads = []

    def setup_lan(self, static_ip=None):
        self.lan.active(True)
        
        if static_ip:
            self.lan.ifconfig((static_ip, '255.255.255.0', '192.168.1.1', '8.8.8.8'))
        else:
            self.lan.ifconfig('dhcp')
        
        while not self.lan.isconnected():
            print("Waiting for LAN connection...".format())
            time.sleep(1)
        
        print("Connected to LAN: {}".format(self.lan.ifconfig()))
        return self.lan

    def handle_client(self, connection: socket.socket, address: tuple):
        print("New connection from {}".format(address))
        data = connection.recv(1024).decode()
        print(data)
        connection.close()
        # while slef.running:
        #     try:
        #         data = connection.recv(1024).decode()
        #         if not data:
                    
        # data = separate_json_objects(data)
        # if data is None:
        #     print(f"Error: Invalid initial data from {address}")
        #     connection.close()
        #     if address in self.afe_memory:
        #         afe = self.afe_memory[address]
        #         afe.status = "DISCONNECTED"
        #     return
        
        # for d in data:
        #     j = json.loads(d)
        #     afe = None
        #     if 'afe_id' in j:
        #         afe_id = j['afe_id']
        #         if afe_id in self.afe_memory:
        #             afe = self.afe_memory[afe_id]
        #         else:
        #             afe = AFE_interface(address[0], afe_id, connection)
        #             afe.status = "CONNECTED"
        #             self.afe_memory[afe_id] = afe
        #             print(f"New AFE {afe_id} connected from {address}")
        #         afe.handle_data(j)
        #     else:
        #         connection.sendall(json.dumps({
        #             "status": "ERROR",
        #             "message": "No afe_id in initial data"
        #         }))
        #         continue
        # while True:
        #     afe = None
        #     try:
        #         afe.receive_data()
        #         if afe.status == "DISCONNECTED":
        #             connection.close()
        #             print(f"Connection with AFE {afe.afe_id} from {address} closed.")
        #             return
        #         if afe.validate_data(data):
        #             afe.send_data(json.dumps({"status": "OK"}))
        #         else:
        #                 afe.send_data(json.dumps({
        #                     "status": "ERROR",
        #                 }))

        #     except Exception as e:
        #         print(f"Error handling client from {address}: {e}")
        #         connection.close()
        #         if afe is not None:
        #             afe.status = "DISCONNECTED"
        #         return

    def run(self,**kwargs):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.lan.ifconfig()[0], 5555))
        self.server_socket.listen(1)
        # self.server_socket.setblocking(False)
        print("Server started on: {}:5555".format(self.lan.ifconfig()[0]))
        
        while self.running:
            # readable, _, _ = select.select([self.server_socket], [], [], 1)
            # if readable:
            print("xxxx")
            try:
                connection, address = self.server_socket.accept()
                self.threads.append(_thread.start_new_thread(self.handle_client, (connection, address,)))
            except OSError as e:
                print("Error accepting connection:", e)
            
    # def start_server(self):
    #     self.lan = self.setup_lan()
    #     self.running = True
    #     _thread.start_new_thread(self.run, ())
