import socket
import struct
import threading
import time
from collections import defaultdict

from scapy.all import IP, UDP, Ether, Raw, sendp, sniff

MAC_REGISTER_TYPE = 0x01
CLIENT_IP_TYPE = 0x02
VNI_TYPE = 0x03
VTEP_IP_TYPE = 0x04
SOURCE_IP = "192.168.1.1"

VXLAN_UDP_PORT = 4789


class LMEPServer:
    def __init__(self, host="0.0.0.0", port=4789):
        self.host = host
        self.port = port
        self.mac_table = defaultdict(
            dict
        )  # {MAC: {"vtep_ip": str, "timestamp": float, "vni": 100}}
        self.lock = threading.Lock()
        self.running = False

    def start(self):
        self.running = True
        threading.Thread(target=self._listen_for_registrations, daemon=True).start()
        sniff(iface="eth0", filter="ip", prn=self._translate_and_forward)

    def stop(self):
        self.running = False

    def _listen_for_registrations(self):
        print(f"LMEP started on {self.host}:{self.port}")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((self.host, self.port))
            print(f"Listening for MAC registration messages on {self.host}:{self.port}")

            while self.running:
                data, addr = sock.recvfrom(1024)
                print(f"Received data from {addr}")
                self._process_registration(data)

    def _translate_and_forward(self, packet):
        if IP in packet:
            dest_ip = packet[IP].dst

            vtepInfo = self.lookup_mac(dest_ip)
            if not vtepInfo:
                print(f"{dest_ip} not in map")
                return

            vxlan_header = Raw(
                b"\x08\x00\x00\x00" + vtepInfo["vni"].to_bytes(3, "big") + b"\x00"
            )
            outer_udp = UDP(sport=12345, dport=VXLAN_UDP_PORT)
            outer_ip = IP(src=SOURCE_IP, dst=vtepInfo["vtep_ip"])
            # might have to replace this
            outer_eth = Ether(src="00:11:22:33:44:55", dst="ff:ff:ff:ff:ff:ff")

            vxlan_packet = (
                outer_eth / outer_ip / outer_udp / vxlan_header / packet[Ether]
            )

            sendp(vxlan_packet, iface="eth0")
            print(f"Forwarded packet to VTEP {vtepInfo['vtep_ip']}")

    def _process_registration(self, data):
        try:
            mac, vtep_ip = None, None
            offset = 0

            while offset < len(data):
                tlv_type = data[offset]
                tlv_length = data[offset + 1]
                tlv_value = data[offset + 2 : offset + 2 + tlv_length]

                if tlv_type == MAC_REGISTER_TYPE:
                    mac = ":".join(f"{b:02x}" for b in tlv_value)
                elif tlv_type == VTEP_IP_TYPE:
                    vtep_ip = socket.inet_ntoa(tlv_value)

                offset += 2 + tlv_length

            if mac and vtep_ip:
                with self.lock:
                    self.mac_table[mac] = {
                        "vtep_ip": vtep_ip,
                        "timestamp": time.time(),
                        "vni": 100,
                    }
                print(f"Registered MAC {mac} to VTEP {vtep_ip}")
            else:
                print(f"Invalid registration message: {data}")
        except Exception as e:
            print(f"Error processing registration message: {e}")

    def lookup_mac(self, mac):
        with self.lock:
            entry = self.mac_table.get(mac)
            if entry:
                return entry
            return None


if __name__ == "__main__":
    server = LMEPServer()
    try:
        server.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
