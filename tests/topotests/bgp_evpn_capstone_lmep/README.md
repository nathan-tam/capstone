# LMEP

This folder contains the topotest setup for the Layer-2 Mapping & Encapsulation Protocol (LMEP) and this documentation aims to detail why it exists and how exactly it works.

## Table of Contents

- [Traditional Solutions](#the-problem-lmep-solves)
- [Key Terminology](#key-terminology)
- [How LMEP Works](#how-lmep-works)
- [System Architecture (This Implementation)](#system-architecture-this-implementation)
- [The Test Topology](#the-test-topology)
- [The Registration Protocol (Binary TLV)](#the-registration-protocol-binary-tlv)
- [The Forwarding Model (Scapy Sniffing)](#the-forwarding-model-scapy-sniffing)
- [How to Run Everything](#how-to-run-everything)
- [How to Verify It Works](#how-to-verify-it-works)
- [What the Tests Validate](#what-the-tests-validate)
- [Why This Reduces Control-Plane Traffic](#why-this-reduces-control-plane-traffic)
- [Further Reading](#further-reading)

---

### The Problem LMEP Solves

In a traditional data center network, when a switch receives a packet addressed to a MAC address it has never seen before, it **floods** that packet out every port to find the destination. This is called **flood-and-learn**. Every switch in the fabric sees the flooded traffic, learns where the MAC lives, and stores it in a forwarding table. This works fine for static environments, but in a setting where endpoints (e.g., mobile robots, IoT devices) move frequently between access points, every move triggers a new round of flooding and relearning across the entire fabric.

**EVPN** (Ethernet VPN) improves on this by using **BGP** (Border Gateway Protocol) to distribute MAC addresses between switches. Instead of flooding, switches exchange BGP update messages to tell each other "MAC address X is reachable through me." This eliminates flooding but adds a different cost: every time an endpoint moves, a burst of BGP update messages propagates through the network so that every switch has a consistent view. In a highly mobile environment, this creates significant **control-plane chatter** — network overhead that has nothing to do with actually delivering useful data.

**LMEP** takes a different approach entirely. Instead of having every switch independently learn and share endpoint locations, a single **Mapping Server** acts as the authoritative source of truth. When an endpoint appears or moves, the access point sends a lightweight registration message directly to this server. The server then knows exactly which VTEP (VXLAN Tunnel Endpoint) to forward traffic to. This eliminates flooding and fabric-wide BGP updates for endpoint moves.

---

### Key Terminology

| Term | What It Is |
|---|---|
| **VXLAN** | Virtual Extensible LAN — a tunneling protocol that wraps Layer-2 Ethernet frames inside UDP packets so they can travel across a Layer-3 IP network.
| **VNI** | VXLAN Network Identifier — a number (like `1000`) that identifies which virtual network a VXLAN packet belongs to. Similar to a VLAN ID, but supports up to 16 million networks instead of only 4,096. |
| **VTEP** | VXLAN Tunnel Endpoint — the device (usually a switch) that encapsulates and decapsulates VXLAN traffic. In our test topology, `vtep1`, `vtep2`, and `vtep3` are VTEPs. Each one is the "on-ramp" to the VXLAN overlay for the hosts connected to it. |
| **Spine** | In a spine-leaf architecture, spines are the switches that connect all leaf switches (VTEPs) together. They don't connect directly to hosts, they just route traffic between leaves. |
| **EVPN** | Ethernet VPN — a BGP address family that distributes MAC and IP reachability information between VTEPs. It's the "standard" way modern data centers propagate endpoint locations. |
| **FRRouting** | An open-source routing software suite that implements BGP, OSPF, and other protocols. The VTEPs and spines in our test topology run FRR. |
| **Topotest** | FRRouting's integration testing framework. It creates virtual network topologies using Linux network namespaces and runs routing daemons inside them. |
| **Scapy** | A Python library for crafting, sending, sniffing, and dissecting network packets. The LMEP server uses Scapy to capture packets off a network interface and forward them as VXLAN-encapsulated traffic. |
| **TLV** | Type-Length-Value — a simple binary encoding format where each piece of data is preceded by a type code (what kind of data) and a length (how many bytes). Used for the LMEP registration messages. |
| **Mapping Server** | The central LMEP daemon that stores the `{MAC → VTEP}` mapping and performs packet interception/forwarding. This is `lmep_server.py`. |

---

### How LMEP Works

At a high level, LMEP has two planes:

### Control Plane: Registration

When an endpoint (e.g., a mobile robot) connects to a wireless access point:

1. The AP detects the new connection and immediately sends a **MAC registration message** to the Mapping Server.
2. The message contains: the client's MAC address, its IP address, the VXLAN Network Identifier (VNI), and the IP address of the VTEP that the AP is connected through.
3. The Mapping Server stores this as a `{MAC → VTEP}` mapping entry.

This registration happens **pre-emptively** — the AP sends it as soon as the client starts associating, before the client has fully connected. This minimizes the window where traffic could be "black-holed" (sent to the wrong place because the server doesn't know the client's new location yet).

### Data Plane: Intercept & Forward

When the controller needs to send a command to a robot:

1. **Intercept**: The controller sends a plain Ethernet frame addressed to the robot's MAC address. The Mapping Server's "listening interface" captures this frame.
2. **Lookup**: The server looks up the destination MAC in its mapping table to find the correct VTEP IP.
3. **Encapsulate**: The server wraps the original Ethernet frame inside a VXLAN packet, setting the outer destination IP to the VTEP where the robot is attached.
4. **Forward**: The VXLAN packet travels across the network to the destination VTEP, which strips off the VXLAN header and delivers the original frame to the robot.

Return traffic (robot → controller) does **not** go through LMEP. Since the controller is not mobile, return traffic uses normal VXLAN or IP routing.

---

## System Architecture (This Implementation)

There are three separate components:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Host Machine                                    │
│                                                                        │
│  ┌──────────────────┐        ┌──────────────────────────────────────┐  │
│  │  LMEP Server     │        │  Topotest (pytest)                   │  │
│  │  (lmep_server.py)│ ◄──── │  Virtual topology with FRR routers   │  │
│  │                  │  UDP   │                                      │  │
│  │  • Listens for   │  TLV   │  spine1 ── vtep1 ── host1            │  │
│  │    registrations │  reg   │  spine1 ── vtep2 ── host2            │  │
│  │  • Sniffs packets│        │  spine1 ── vtep3 ── host3            │  │
│  │  • Forwards via  │        │  spine2 ── vtep1/2/3                 │  │
│  │    VXLAN         │        │                                      │  │
│  └──────────────────┘        └──────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

1. **The LMEP Server** (`lmep_server.py`) — runs as a standalone Python process on the host machine. It does two things concurrently:
   - Listens on a UDP port (default `6000`) for binary TLV registration messages.
   - Uses Scapy to sniff packets on a network interface and forward matching traffic as VXLAN.

2. **The Test Script** (`test_evpn_capstone_lmep.py`) — a pytest-based topotest that creates the virtual network topology, starts FRR daemons on all the virtual routers, simulates endpoint movement by creating/destroying macvlan interfaces, and sends binary TLV registration messages to the LMEP server.

3. **The FRR Topology** — a virtual network of spine switches, VTEPs, and hosts running inside Linux network namespaces. Each VTEP runs FRR with BGP/EVPN configured. The FRR configs are stored in `spine1/`, `spine2/`, `vtep1/`, `vtep2/`, `vtep3/` subdirectories.

**Why is the LMEP server external (not inside the topology)?**

The LMEP server runs on the host machine rather than inside a topotest namespace because:
- It keeps the LMEP daemon **independent** from the test lifecycle — you can restart the test without restarting the server.
- It avoids **namespace routing issues** — topotest namespaces are isolated and may not be able to reach each other easily for TCP/UDP connections.
- It makes it easier to **debug** — you can watch the server logs in a separate tmux pane while the test runs.

---

## The Test Topology

The test creates a spine-leaf fabric with 3 VTEPs, each connected to a host:

```
         ┌────────┐     ┌────────┐
         │ spine1 │     │ spine2 │
         └─┬──┬──┬┘     └┬──┬──┬─┘
           │  │  │        │  │  │
     ┌─────┘  │  └────┐  │  │  │
     │     ┌──┘  ┌────┼──┘  │  │
     │     │     │    │  ┌──┘  │
   ┌─┴──┐ ┌┴───┐ ┌┴──┐ │     │
   │vtep1│ │vtep2│ │vtep3│◄────┘
   └──┬──┘ └──┬──┘ └──┬──┘
      │       │       │
   ┌──┴──┐ ┌──┴──┐ ┌──┴──┐
   │host1│ │host2│ │host3│
   └─────┘ └─────┘ └─────┘
```

- **Spines** (`spine1`, `spine2`) — BGP route reflectors connecting all VTEPs. They don't attach to hosts directly.
- **VTEPs** (`vtep1`, `vtep2`, `vtep3`) — leaf switches that terminate VXLAN tunnels. Each has a loopback IP (`10.10.10.10`, `20.20.20.20`, `30.30.30.30`) used as the VTEP tunnel source address.
- **Hosts** (`host1`, `host2`, `host3`) — simulated endpoints connected to VTEPs via bonded interfaces (`vtepbond`). The test creates additional **macvlan** "dummy" interfaces on these hosts to simulate multiple endpoints that can be moved between VTEPs.

Each VTEP has:
- A Linux bridge (`br1000`) serving as the Layer-2 domain.
- A VXLAN interface (`vni1000`) bound to VNI 1000, connecting the bridge to the VXLAN overlay.
- An SVI (Switch Virtual Interface) IP address on the bridge for anycast gateway functionality.

---

## The Registration Protocol (Binary TLV)

When an endpoint moves (or first connects), the test sends a **MAC registration message** to the LMEP server. This message uses a compact binary format called **TLV** (Type-Length-Value), where each piece of data is encoded as:

```
┌──────────┬──────────┬──────────────────────┐
│  Type    │  Length   │  Value               │
│ (1 byte) │ (1 byte) │ (variable length)    │
└──────────┴──────────┴──────────────────────┘
```

A complete registration message contains four TLV fields concatenated together:

| Type | Length | Value | Description |
|---|---|---|---|
| `0x01` | 6 bytes | Client MAC | The MAC address of the mobile endpoint (e.g., `00:00:00:00:ff:01`) |
| `0x02` | 4 bytes | Client IP | The IP address of the endpoint (e.g., `192.168.0.1`) |
| `0x03` | 3 bytes | VNI | The VXLAN Network Identifier (e.g., `1000`) |
| `0x04` | 4 bytes | VTEP IP | The IP address of the VTEP where the endpoint is now attached (e.g., `10.10.10.10`) |

For example, the raw bytes for registering MAC `00:00:00:00:ff:01` at VTEP `10.10.10.10` on VNI `1000` would look like:

```
01 06 00 00 00 00 ff 01   ← Type 0x01, Length 6, MAC bytes
02 04 c0 a8 00 01         ← Type 0x02, Length 4, IP 192.168.0.1
03 03 00 03 e8            ← Type 0x03, Length 3, VNI 1000
04 04 0a 0a 0a 0a         ← Type 0x04, Length 4, VTEP IP 10.10.10.10
```

This message is sent as a **single UDP datagram** to the LMEP server. UDP is used because:
- Registration is **fire-and-forget** — we don't need a response or acknowledgment. The goal is speed: get the mapping updated before the endpoint finishes connecting.
- UDP has **less overhead** than TCP — no connection setup, no handshakes, just send-and-done.
- In a real deployment, the WAP would send this packet the instant a client begins associating, so every millisecond counts.

---

## The Forwarding Model (Scapy Sniffing)

The LMEP server uses **Scapy** (a Python packet manipulation library) to intercept and forward traffic. Here's how it works:

### What `--iface eth0` means

When you start the server with `--iface eth0`, you're telling it **which network interface to sniff on**. The server calls Scapy's `sniff()` function on that interface, which puts it into a mode where it captures every IP packet that arrives on `eth0`.

In a real-world deployment:
- `eth0` would be the **controller-facing interface** — the physical port connected to the network where the controller sends its commands.
- The server sits between the controller and the fabric, intercepting outgoing traffic destined for mobile endpoints.

In the test environment:
- `eth0` is the default network interface of the host machine (or the test VM/container).
- Traffic intended for endpoints in the topotest topology would arrive here for the server to intercept and forward.

### The sniff-lookup-forward cycle

1. **Sniff**: Scapy continuously captures packets arriving on the specified interface. Each captured packet triggers a callback function.
2. **Lookup**: The callback extracts the **destination MAC address** from the Ethernet header and checks it against the server's mapping table (populated by earlier registration messages).
3. **Skip or forward**:
   - If the MAC is **not** in the mapping table → the packet is ignored (it's not destined for a registered endpoint).
   - If the MAC **is** found → the server knows which VTEP to send it to.
4. **Encapsulate**: The server wraps the original Ethernet frame in a VXLAN packet:
   - **Outer Ethernet**: A new Ethernet header with a placeholder source/destination MAC.
   - **Outer IP**: Source IP is the server's own IP (`--source-ip`), destination IP is the VTEP IP from the mapping table.
   - **Outer UDP**: Source port is arbitrary, destination port is `4789` (the standard VXLAN port).
   - **VXLAN header**: Contains the VNI (e.g., `1000`) so the destination VTEP knows which virtual network this belongs to.
   - **Inner frame**: The original Ethernet frame, completely unchanged.
5. **Send**: The encapsulated VXLAN packet is sent out via `sendp()` (Scapy's raw packet send function) on the same interface.

### Why Scapy?

Scapy is used because it can:
- **Capture raw packets** directly from a network interface (like tcpdump, but programmable).
- **Construct packets** from scratch — we need to build custom VXLAN-encapsulated frames with precise header values.
- **Send raw frames** at Layer 2 — normal socket programming can only send at Layer 3 (IP) or above. VXLAN encapsulation requires building the full Ethernet + IP + UDP + VXLAN stack.

The tradeoff is that Scapy requires **root privileges** because it uses raw sockets to access the network interface directly.

---

## How to Run Everything

### Prerequisites

- **Python 3** with Scapy installed (`pip install scapy`)
- **FRRouting** compiled and installed (the topotest framework needs FRR daemons)
- **Root privileges** for both the LMEP server (Scapy needs raw sockets) and the topotest (creates network namespaces)

### Step 1: Start the LMEP Server

Open a terminal (or `tmux` pane) and start the server. It will run until you press Ctrl+C.

```bash
cd /Users/nathantam/Projects/capstone/tests/topotests/bgp_evpn_capstone_lmep/
sudo python3 lmep_server.py \
    --bind-host 0.0.0.0 \
    --port 6000 \
    --iface eth0
```

**What each flag means:**

| Flag | Purpose |
|---|---|
| `--bind-host 0.0.0.0` | Listen for registration messages on **all** network interfaces. Use a specific IP to restrict where registrations can come from. |
| `--port 6000` | The UDP port to listen on for binary TLV registration messages. Must match `LMEP_PORT` in the test. |
| `--iface eth0` | The network interface Scapy will sniff for controller traffic and send VXLAN packets on. This should be the interface that can reach the VXLAN fabric. |

**Optional flags:**

| Flag | Default | Purpose |
|---|---|---|
| `--vxlan-port` | `4789` | The standard VXLAN destination port. Only change this if your fabric uses a non-standard VXLAN port. |
| `--source-ip` | `192.168.1.1` | The outer source IP placed in the VXLAN-encapsulated packets. Should be an IP reachable by the VTEPs. |
| `--log-level` | `INFO` | Set to `DEBUG` for more verbose output, or `WARNING` to reduce noise. |

### Step 2: Run the Test

In a separate terminal:

```bash
sudo pytest -s tests/topotests/bgp_evpn_capstone_lmep/test_evpn_capstone_lmep.py
```

The `-s` flag disables output capture so you can see the test's print statements (packet counts, movement events, etc.) in real time.

### Environment Variables

If the defaults don't match your setup, override them with environment variables:

```bash
LMEP_SERVER_HOST=<reachable-server-ip> \
LMEP_PORT=6000 \
sudo pytest -s tests/topotests/bgp_evpn_capstone_lmep/test_evpn_capstone_lmep.py
```

| Variable | Default | Purpose |
|---|---|---|
| `LMEP_SERVER_HOST` | `127.0.0.1` | IP address where the LMEP server is reachable from the test process |
| `LMEP_PORT` | `6000` | UDP port for TLV registrations (must match `--port` on the server) |
| `LMEP_VXLAN_PORT` | `4789` | VXLAN destination port |
| `LMEP_VNI` | `1000` | Default VXLAN Network Identifier used in registrations |

---

## How to Verify It Works

### 1. Check the server logs

When the test runs, the LMEP server terminal should show log lines like:

```
2026-04-03 00:15:32 INFO Registered MAC 00:00:00:00:ff:01 -> VTEP 10.10.10.10 (client_ip=192.168.0.1 vni=1000 regs=1) from ('127.0.0.1', 54321)
2026-04-03 00:15:33 INFO Forwarded packet dst_mac=00:00:00:00:ff:01 -> VTEP 10.10.10.10 (vni=1000)
```

- `Registered MAC` — confirms a TLV registration message was received and parsed correctly.
- `Forwarded packet` — confirms Scapy intercepted a packet matching a registered MAC and forwarded it via VXLAN.

### 2. Check EVPN MAC state on VTEPs

From within the test (or by attaching to the VTEP namespace), you can query the EVPN MAC table:

```bash
vtysh -c "show evpn mac vni 1000 json"
```

After a host move, the MAC should appear at the new VTEP's IP with an updated sequence number.

### 3. Check BGP control-plane traffic

The test automatically starts `tcpdump` captures on BGP port 179 across spine and VTEP nodes. At teardown, it prints packet counts and MP_REACH/MP_UNREACH NLRI statistics from `tshark` (if available). These numbers let you quantify how much BGP traffic was generated during the mobility scenario.

---

## What the Tests Validate

The test file (`test_evpn_capstone_lmep.py`) contains two test functions:

### `test_host_movement`

This is the main test. It:

1. **Sets up the topology** — creates bridges, VXLAN interfaces, bonds, and macvlan "dummy" interfaces to simulate multiple endpoints distributed across hosts.
2. **Starts packet captures** — begins tcpdump on BGP port 179 on all spines and VTEPs to measure control-plane traffic.
3. **Moves endpoints** — for each "dummy" interface, it:
   - Creates the macvlan on the target host (simulating the endpoint appearing at a new access point).
   - Deletes it from the old host (simulating departure).
   - Sends a binary TLV registration to the LMEP server with the new VTEP IP.
   - Queries `show evpn mac vni 1000 json` to confirm the MAC entry is present in the EVPN table.
4. **Reports results** — stops captures and prints per-node BGP packet totals and MP_REACH/MP_UNREACH NLRI counts.

### `test_get_version`

A simple sanity check that queries the EVPN MAC table on vtep1 to make sure FRR is running and responsive.

---

## Why This Reduces Control-Plane Traffic

Endpoint reachability is learned **once**, at the point of attachment, instead of being inferred across the fabric through distributed protocols.

This reduces control-plane overhead in three ways:

1. **Fewer flooded frames** — when a controller sends a frame to an unknown MAC, the Mapping Server translates it directly instead of the fabric flooding it to every VTEP.
2. **Less MAC-learning churn** — mobile endpoints update the Mapping Server directly when they move. The fabric doesn't need to re-learn the MAC through data-plane observation.
3. **Less distributed state replication** — the full endpoint database lives on the Mapping Server only. It does not need to be replicated to every VTEP via BGP updates.

The net effect: the fabric spends less effort learning and relearning where an endpoint lives, and more effort simply forwarding the packets that matter.

---

## Further Reading

- **`LMEP Standard.md`** — the full protocol specification, including the TLV message format, mobility logic, cleanup process, and the Hierarchical LMEP (H-LMEP) extension.
- **`topology.dot`** — a Graphviz visualization of the test topology.
- **`lmep_server.py`** — the LMEP Mapping Server source code, with inline documentation.
- **`test_evpn_capstone_lmep.py`** — the topotest script that builds the topology, moves endpoints, and measures control-plane traffic.