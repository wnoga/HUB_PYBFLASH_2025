import network
import usocket as socket
import time
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

    def setup_lan(self, static_ip=None):
        self.lan.active(True)
        
        if static_ip:
            self.lan.ifconfig((static_ip, '255.255.255.0', '192.168.1.1', '8.8.8.8'))
        else:
            self.lan.ifconfig('dhcp')
        
        while not self.lan.isconnected():
            print("Waiting for LAN connection...")
            time.sleep(1)
        
        print("Connected to LAN:", self.lan.ifconfig())
        return self.lan

    def handle_client(self, conn, addr):
        print("Connection from:", addr)
        
        try:
            data = conn.recv(1024).decode('utf-8')
            print("Received:", data)     
            self.hub.parse(data)

        except Exception as e:
            print("Error:", str(e))
        
        conn.close()
    
    def send_msg(self, ip, port, msg):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((ip, port))
                s.send(msg.encode('utf-8'))
                print("Message sent:", msg)
        except Exception as e:
            print("Error sending message:", str(e))

    def server_loop(self):
        while self.running:
            try:
                conn, addr = self.s.accept()
                self.handle_client(conn, addr)
            except Exception as e:
                print("Error during connection handling:", str(e))

    def start_server(self):
        # Set static IP or use DHCP
        USE_STATIC_IP = False  # Change to True and set IP below if needed
        STATIC_IP = '192.168.1.100'
        self.lan = self.setup_lan(STATIC_IP if USE_STATIC_IP else None)

        # Create a socket
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.bind(('0.0.0.0', 5555))
        self.s.listen(5)

        print("Server listening on port 5555...")
        self.running = True  # Start the server loop
        self.t = _thread.start_new_thread(self.server_loop, ())

    def stop_server(self):
        """Stop the server and close the socket."""
        self.running = False
        if self.s:
            self.s.close()
        print("Server stopped.")

