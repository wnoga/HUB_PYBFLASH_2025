import network
import socket as socket
import time
import select
import _thread
import ujson
from HUB import HUBDevice
import pyb
import struct
import machine
import micropython

from my_utilities import wdt
from my_utilities import millis
from my_utilities import p
from my_utilities import rtc, rtc_synced

# NTP constants
NTP_DELTA = 2208988800  # Seconds between NTP epoch (1900) and Unix epoch (1970)
NTP_HOST = "pool.ntp.org"
class MySimpleServer():
    def __init__(self, hub: HUBDevice, static_ip=None):
        self.lan = network.LAN()
        self.port = 5555
        self.ntp_synced = False
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
        self.server_socket:socket.socket = None
        # self.connecions = []
        self.timestamp_ms = 0
        self.wait_ms = 100
        
        self.setup_lan_state = 0
        self.ntp_sync_state = 0

    def sync_ntp_machine(self):
        if not self.lan.isconnected():
            p.print("NTP sync: LAN not connected.")
            self.ntp_sync_state = 0
            return False

        if self.ntp_sync_state == 0:
            p.print("Attempting to sync NTP...")
            try:
                # Create a UDP socket
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(5) # 5 second timeout

                # Get server IP
                addr = socket.getaddrinfo(NTP_HOST, 123)[0][-1]

                # NTP request packet (minimal: version 3, client mode)
                # LI = 0 (no warning), VN = 3 (version 3), Mode = 3 (client)
                # (0 << 6) | (3 << 3) | 3 = 0 | 24 | 3 = 27 (0x1b)
                msg = bytearray(48)
                msg[0] = 0x1B # LI, Version, Mode

                s.sendto(msg, addr)
                data, server_addr = s.recvfrom(48)
                s.close()

                if data:
                    # Extract the transmit timestamp (bytes 40-43)
                    # This is the number of seconds since Jan 1, 1900
                    secs = struct.unpack("!I", data[40:44])[0]
                    
                    # Convert to Unix epoch (seconds since Jan 1, 1970)
                    unix_secs = secs - NTP_DELTA

                    # Get the time tuple (year, month, mday, hour, minute, second, weekday, yearday)
                    # MicroPython's time.gmtime() expects seconds since 2000-01-01.
                    # So, we need to adjust unix_secs.
                    # Seconds from 1970-01-01 to 2000-01-01 is 946684800
                    secs_since_2000 = unix_secs - 946684800
                    
                    tm = time.gmtime(secs_since_2000)

                    # Set RTC: (year, month, day, weekday, hours, minutes, seconds, subseconds)
                    # weekday: Mon=1, Sun=7. time.gmtime returns Mon=0, Sun=6.
                    rtc.datetime((tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))
                    self.ntp_sync_state = 1
                    # p.print("NTP sync successful. Time set to: {}".format(time.gmtime()))
                    current_time_tuple_for_log = time.gmtime() # Get current time after setting RTC
                    p.print("NTP sync successful. Time set to: {}".format(current_time_tuple_for_log))
                    rtc_synced = True
                    # Rename logger file with the new timestamp
                    try:
                        year, month, day, hour, minute, second = current_time_tuple_for_log[0:6]
                        new_log_filename = "log_synced_{:04d}{:02d}{:02d}_{:02d}{:02d}{:02d}.json".format(
                            year, month, day, hour, minute, second
                        )
                        if not "log_synced_" in self.hub.logger.filename:
                            self.hub.logger.rename_current_file(new_log_filename)
                        # p.print("Logger file active name changed to: {}".format(self.hub.logger.filename))
                    except Exception as e_rename:
                        p.print("Failed to rename logger file: {}".format(e_rename))
            except Exception as e:
                p.print("NTP sync failed: {}".format(e))
                self.ntp_sync_state = 0
                return False
        elif self.ntp_sync_state == 1:
            # Check if time is reasonable (after Unix epoch + some buffer, e.g., year 2023)
            # time.time() in MicroPython is seconds since 2000-01-01
            if time.time() > (23 * 365 * 24 * 60 * 60): # Check if time is after Jan 1, 2023
                self.ntp_synced = True
                return True
        return False

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
                    return True
                else:
                    if (millis() - timestamp_ms) > 15000:
                        self.setup_lan_state = 0
            elif self.setup_lan_state == 2:
                if not self.lan.isconnected():
                    self.setup_lan_state = 0 # Recconect
                return True
            else:
                self.setup_lan_state = 0
        except:
            self.setup_lan_state = 0
        return None

    def handle_client(self, connection: socket.socket, address: tuple):
        p.print("New connection from {}".format(address))
        connection.settimeout(10.0)
        try:
            data = connection.recv(1024).decode()
            if not data:
                return
            timestamp_ms = millis()
            p.print("Received: {}".format(data))
            toSend = []
            try:
                j = ujson.loads(data)
                # afe_id = j["afe_id"]
                # afe_id = 35
                if "procedure" in j:
                    p.print("Procedure: {}".format(j["procedure"]))
                    # afe_id = int(j["afe_id"])
                    # afe = self.hub.get_afe_by_id(afe_id)

                    afe_id = j.get("afe_id",None)
                    procedure = j["procedure"]
                    if procedure == "default_full":
                        if afe_id is None:
                            return
                        afe = self.hub.get_afe_by_id(afe_id)
                        if afe is None:
                            return
                        self.hub.default_full(afe_id)
                        # j["status"] = "OK"
                        toSend.append(j)
                        # connection.sendall(ujson.dumps(j).encode())
                    elif procedure == "default_get_measurement_last":
                        # print("Before X")
                        # x = True
                        def cb(msg=None):
                            print("EXECUTED CALLBACK ON SERVER: {}".format(msg))
                            my_dict = msg.copy()
                            if 'frame' in my_dict:
                                my_dict.pop('frame')
                            if 'callback' in my_dict:
                                my_dict.pop('callback')
                            try:
                                connection.sendall(ujson.dumps(my_dict).encode())
                                connection.close()
                            except:
                                pass
                            # toSend.append(my_dict)
                            # print("Appended: ", my_dict)
                            # x = False
                            # return my_dict
                        self.hub.default_get_measurement_last(afe_id,callback=cb)
                        # while x:
                        #     if (millis()-timestamp_ms > 5000):
                        #         return None
                        # print("After X")
                        # toSend_dict = {}
                        # print(toSend)
                        # for d in toSend:
                        #     for k,v in d.items():
                        #         toSend_dict.update(k)
                        # print(ujson.dumps(toSend_dict))
                        # connection.sendall(toSe)
                        # j["status"] = "OK"
                    elif procedure == "get_all_afe_configuration":
                        my_dict = {}
                        for afe in self.hub.afe_devices:
                            my_dict[afe.device_id] = afe.configuration
                        print(ujson.dumps(my_dict))
                        connection.sendall(ujson.dumps(my_dict).encode())
                     # Check for 'get_unix_timestamp' command
                    #  elif procedure == "get_unix_timestamp":
                    #     if self.ntp_synced:
                    #         connection.sendall(ujson.dumps({"unix_timestamp": time.time()}).encode())
                    #     else:
                            
                    else:
                        connection.sendall(ujson.dumps({"status": "ERROR", "status_info": "NTP not synced"}).encode())
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
            # connection.sendall(ujson.dumps({"status":"OK"}).encode())
        except OSError as e:
            if e.args[0] == 110:  # ETIMEDOUT
                p.print("Connection timed out from {}".format(address))
            else:
                p.print("Error handling client from {}: {}".format(address, e))
        except Exception as e:
            p.print("Error handling client from {}: {}".format(address, e))
        # finally: # Avoid deadlock
        #     connection.close()
        #     p.print("Connection closed from {}".format(address))
    
    def setup_socket(self):
        try:
            try:
                self.server_socket.close()
            except:
                pass
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # self.server_socket.settimeout(2.0)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind((self.lan.ifconfig()[0], int(self.port)))
            self.server_socket.listen(1)
        except Exception as e:
            self.server_socket = None
            p.print("Error setting up server socket: {}".format(e))
    
    def main_machine(self):
        # if (millis() - self.timestamp_ms) < self.wait_ms:
        #     return
        # self.timestamp_ms = millis()
        # p.print("MAIN MACHINE SERVER")
        # p.print(self.lan.ifconfig())
        if self.setup_lan_machine(): # If True, then lan is set
            self.sync_ntp_machine()
            if self.server_socket is None:
                self.setup_socket()
            # try:
            # self.server_socket.settimeout(0.01)
            connection, address = self.server_socket.accept()
            # _thread.start_new_thread(self.handle_client, (connection, address,))
            # with connection:
            self.handle_client(connection,address)
            # self.connecions.append({"connection":connection,"address":address})
            # except:
            #     pass
            # finally:
            #     try:
            #         connection.close()
            #     except:
            #         pass
    
    def main_machine_scheduled(self,_):
        self.main_machine()       
    
    def main_loop(self):
        while self.running:
            # micropython.schedule(self.main_machine_scheduled, 0)
            self.main_machine()
            # print("SERVER")
            time.sleep_us(10)
            pass
            # time.sleep_ms(100)
            # time.sleep(0.01)
            # wdt.feed()
                
    def start_server(self):
        self.lan = self.setup_lan()
        self.running = True
        # self.run()
        self.run_thread = _thread.start_new_thread(self.run, ())
        
    def x(self):
        while True:
            pyb.delay(100)
            # p.print("TEST")
