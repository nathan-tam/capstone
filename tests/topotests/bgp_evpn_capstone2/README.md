# Host Mobility Simulation Approaches

This folder contains multiple experimental strategies for simulating **host mobility across VTEPs in an EVPN/VXLAN environment**.  

From testing, the result of approach 3.5 resulted in seeing the endpoint on vteps when running "show evpn mac vni all" command, and the ips are reachable. The rest of theoretical approaches are kept in the file just for documentation purposes. 

---

## File Overview

| Approach | Description | Filename |
|---------|-------------|----------|
| **1. Base Case – Modify Host Network Info Directly** | Simulates host movement by changing the host’s MAC/IP on the host OS. Limited scalability. | `01_base_case_host_manipulation.py` |
| **2. Dummy Interface on VTEP** | Dummy interface is created on the VTEP and moved between nodes. Ping works, but EVPN MAC/IP routes do not appear. | `02_vtep_dummy_interface.py` |
| **3. Dummy Interface on Host Attached to VTEP Link** | Dummy interface created on the host and attached to the VTEP-facing link to stay inside FRR-configured networking. | `03_host_dummy_attached_to_vtep.py` |
| **3.5. MACVLAN Interface on Host's bond link towards VTEP** | Instead of a dummy interface as a placeholder for an endpoint, create a macvlan interface that connects with the vtepbond  | `03_host_dummy_attached_to_vtep.py` |

---

## Approach Summaries

### **1. Base Case – Host Network Manipulation**
**File:** `01_base_case_host_manipulation.py`  
This approach attempts to mimic host movement by altering MAC and IP configurations directly on the host.  
Useful for basic sanity checks but not scalable due to bonding MAC limitations.

---

### **2. Dummy Interface on VTEP**
**File:** `02_vtep_dummy_interface.py`  
Creates and moves a dummy interface *on the VTEP*.  
Current results show:  
- Interface reachable via ping  
- Does **not** appear in EVPN MAC tables or BGP logs

---

### **3. Dummy Interface on Host Attached to VTEP Link**
**File:** `03_host_dummy_attached_to_vtep.py`  
Creates dummy interfaces on the host and attaches them to the actual host-to-VTEP link.  
This approach keeps traffic inside FRR-controlled topology and may allow proper MAC/IP learning.

---
### **3.5. MACVLAN Interface on Host's bond link towards VTEP**
**File:** `03_host_dummy_attached_to_vtep.py`  
Creates a macvlan interface _named_ dummyX which is a virtual interface linked to a physical interface vtepbond (dummy interfaces cannot be linked to a bond interface) and can have its own MAC and IP address which is used to mimic an endpoint.

Current results show:  
 - From configuring hosts with macvlan interfaces, show evpn mac vni all command on vteps show the mac address of this interface and the interface can be pinged.
 - Currently, testing host mobility via deleting the interface and creating it on another host.

---

### **4. Dummy Interface on Host Using OSPF**
If Approach 3 does not propagate EVPN information, this uses OSPF to distribute reachability.  
Goal is to test routing-assisted EVPN learning behavior.

---

### **5. Dummy Interface on VTEP + ip neigh Injection**
Attempts to force EVPN behavior by:  
1. Creating dummy interface on VTEP  
2. Injecting ARP/ND entries manually  
3. Checking whether EVPN then advertises endpoint information

---

### **6. Multi-Attached Host With Dynamic Link Manipulation**
A host is connected to all VTEPs simultaneously.  
Host movement is simulated by modifying link states, metrics, or disabling links while a controller host continuously pings.  
Useful for realistic convergence testing.

---

### **Results of apprach 3.5**
    Number of MACs (local and remote) known for this VNI: 8
Flags: N=sync-neighs, I=local-inactive, P=peer-active, X=peer-proxy
MAC               Type   Flags Intf/Remote ES/VTEP            VLAN  Seq #'s
00:00:00:00:00:01 local        hostbond1                            0/0
8e:bb:42:99:02:32 remote       20.20.20.20                          0/0
a6:88:1e:7f:8a:49 remote       20.20.20.20                          0/0
00:00:00:00:ff:02 remote       20.20.20.20                          0/0
00:00:00:00:ff:01 local        hostbond1                            0/0
00:00:00:00:00:02 remote       20.20.20.20                          0/0
2a:d7:78:95:3e:34 local        br1000                         1000  0/0
56:14:71:ca:79:02 local        hostbond1                            0/0

--- Host movement test starting ---
1763406826.9102464

Number of MACs (local and remote) known for this VNI: 13
Flags: N=sync-neighs, I=local-inactive, P=peer-active, X=peer-proxy
MAC               Type   Flags Intf/Remote ES/VTEP            VLAN  Seq #'s
00:00:00:00:00:01 local        hostbond1                            0/0
ee:58:de:7b:f2:04 remote       20.20.20.20                          0/0
00:00:00:00:ff:03 remote       20.20.20.20                          209/210
00:00:00:00:00:04 remote       40.40.40.40                          0/0
00:00:00:00:ff:01 remote       20.20.20.20                          215/217
6e:bf:a2:bb:e9:49 remote       30.30.30.30                          0/0
00:00:00:00:00:02 remote       20.20.20.20                          0/0
ce:3e:76:e5:f1:06 remote       20.20.20.20                          0/0
ea:33:23:d4:a0:67 remote       40.40.40.40                          0/0
6a:65:8c:fb:50:6e local        br1000                         1000  0/0
00:00:00:00:ff:02 local        hostbond1                            206/205
00:00:00:00:00:03 remote       30.30.30.30                          0/0
82:17:55:ae:73:17 local        hostbond1                            0/0


--- Host moved to host2 ---
1763406826.966382

PING 192.168.0.19 (192.168.0.19) 56(84) bytes of data.
[1763406825.969252] 64 bytes from 192.168.0.19: icmp_seq=1 ttl=64 time=1060 ms
[1763406825.969275] 64 bytes from 192.168.0.19: icmp_seq=2 ttl=64 time=952 ms
[1763406825.969279] 64 bytes from 192.168.0.19: icmp_seq=3 ttl=64 time=848 ms
[1763406825.969281] 64 bytes from 192.168.0.19: icmp_seq=4 ttl=64 time=745 ms
[1763406825.969284] 64 bytes from 192.168.0.19: icmp_seq=5 ttl=64 time=640 ms
[1763406825.969287] 64 bytes from 192.168.0.19: icmp_seq=6 ttl=64 time=537 ms
[1763406825.969290] 64 bytes from 192.168.0.19: icmp_seq=7 ttl=64 time=432 ms
[1763406825.969292] 64 bytes from 192.168.0.19: icmp_seq=8 ttl=64 time=329 ms
[1763406825.969295] 64 bytes from 192.168.0.19: icmp_seq=9 ttl=64 time=224 ms
[1763406825.969298] 64 bytes from 192.168.0.19: icmp_seq=10 ttl=64 time=121 ms
[1763406826.052553] 64 bytes from 192.168.0.19: icmp_seq=11 ttl=64 time=0.054 ms
[1763406826.160434] 64 bytes from 192.168.0.19: icmp_seq=12 ttl=64 time=0.055 ms
[1763406826.264588] 64 bytes from 192.168.0.19: icmp_seq=13 ttl=64 time=0.054 ms
[1763406826.368644] 64 bytes from 192.168.0.19: icmp_seq=14 ttl=64 time=0.055 ms
[1763406826.472472] 64 bytes from 192.168.0.19: icmp_seq=15 ttl=64 time=0.058 ms
[1763406826.576538] 64 bytes from 192.168.0.19: icmp_seq=16 ttl=64 time=0.069 ms
[1763406826.680554] 64 bytes from 192.168.0.19: icmp_seq=17 ttl=64 time=0.058 ms
[1763406826.784922] 64 bytes from 192.168.0.19: icmp_seq=18 ttl=64 time=0.054 ms
[1763406826.888690] 64 bytes from 192.168.0.19: icmp_seq=19 ttl=64 time=0.054 ms
[1763406827.096639] 64 bytes from 192.168.0.19: icmp_seq=21 ttl=64 time=0.162 ms
[1763406827.200483] 64 bytes from 192.168.0.19: icmp_seq=22 ttl=64 time=0.053 ms
[1763406827.304786] 64 bytes from 192.168.0.19: icmp_seq=23 ttl=64 time=0.057 ms
[1763406827.408591] 64 bytes from 192.168.0.19: icmp_seq=24 ttl=64 time=0.054 ms
[1763406827.512508] 64 bytes from 192.168.0.19: icmp_seq=25 ttl=64 time=0.055 ms
[1763406827.616490] 64 bytes from 192.168.0.19: icmp_seq=26 ttl=64 time=0.065 ms
[1763406827.720805] 64 bytes from 192.168.0.19: icmp_seq=27 ttl=64 time=0.064 ms
[1763406827.824744] 64 bytes from 192.168.0.19: icmp_seq=28 ttl=64 time=0.070 ms
[1763406827.928421] 64 bytes from 192.168.0.19: icmp_seq=29 ttl=64 time=0.063 ms

---

## ▶ How to Run an Approach

Script can be executed as:

```bash
sudo -E pytest -s --topology-only AA_capstone_topotests/AA_custom_TG_evpn/03_host_dummy_attached_to_vtep.py
