import socket

from scapy.all import IP, UDP, Ether, Raw, sendp, sniff

# Configuration
VXLAN_UDP_PORT = 4789
VNI = 100
VTEP_MAP = {
    "192.168.1.10": "10.0.0.1",
    "192.168.1.20": "10.0.0.2",
}


def translate_and_forward(packet):
    if IP in packet:
        dest_ip = packet[IP].dst

        vtep_ip = VTEP_MAP.get(dest_ip)
        if not vtep_ip:
            print(f"{dest_ip} not in map")
            return

        vxlan_header = Raw(b"\x08\x00\x00\x00" + VNI.to_bytes(3, "big") + b"\x00")
        outer_udp = UDP(sport=12345, dport=VXLAN_UDP_PORT)
        # should be just the IP that lives on the vxlan network
        outer_ip = IP(src="192.168.1.1", dst=vtep_ip)
        outer_eth = Ether(src="00:11:22:33:44:55", dst="ff:ff:ff:ff:ff:ff")

        vxlan_packet = outer_eth / outer_ip / outer_udp / vxlan_header / packet[Ether]

        sendp(vxlan_packet, iface="eth0")
        print(f"Forwarded packet to VTEP {vtep_ip}")


def main():
    sniff(iface="eth0", filter="ip", prn=translate_and_forward)


if __name__ == "__main__":
    main()
