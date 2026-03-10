# Simulating Host Mobility in Topotest with FRRouting
This documentation details the methodology used to simulate Host Mobility in an EVPN/VXLAN fabric using FRRouting Topotests. It also serves as some basic guidance for running experiments and analysing results. There is a document in the tests/topotests/test_evpn_capstone directory called LISEZ.moi that has more detailed information. The Asymmetrical routing experiment details are located in tests/topotests/test_evpn_capstone_asym in a document called ROUTING_TOPOLOGY_AND_CONFIG.md.
### Overview
"Host Mobility" usually refers to a host migrating from one Access Point to another while retaining its MAC and IP address. The network fabric (EVPN) must detect this move and update its routing tables to send traffic to the new location.<br>
Simulating this in a containerized network test environment (Mininet/Topotest) is challenging because we don't have real hosts to move around. We attempt to simulate this behavior using Linux MACVLAN interfaces.
### Simulation
Instead of moving physical hosts, we use Linux MACVLAN interfaces to represent endpoints. Each host node has a bonded uplink (`vtepbond`) connecting it to its VTEP. MACVLAN interfaces are created on top of this bond, so every MACVLAN looks like a distinct physical device directly attached to the VTEP's bridge.

#### How a single migration works
To move a VM from one VTEP to another the script:
1. Creates a new MACVLAN interface on the destination host with the same name, MAC, and IP.
2. Waits briefly (default 0.2s) so the MAC address exists on both old and new hosts simultaneously.
3. Deletes the MACVLAN from the source host.

The destination VTEP learns the MAC/IP and advertises it via BGP EVPN. Other VTEPs receive the update and re-route traffic to the new location.

#### Random Movement
The simulation runs for a fixed wall-clock duration (default 60s). Each second every VM independently rolls against `VM_MOVE_PROBABILITY` (default 0.1). All VMs that "win" the roll in a given tick are migrated together in bulk. Destinations are created, one overlap sleep is performed for the whole group, then all sources are deleted. This keeps tick duration roughly constant regardless of how many VMs move, so doubling the VM count produces proportionally more total migrations within the same time window.

### Quick Start
This section assumes you have already completed the FRR Workspace Setup Guide from the Notion wiki and already have the FRR container running.
1. Change into the test directory: 
2. Use `sudo -E pytest -s bgp_evpn_capstone` to run the test without pauses.
3. Use `sudo -E pytest -s --pause --vtysh=spine1 bgp_evpn_capstone` to run the test with a pause, dropping you into the `spine1` node before the test runs. Note that the test will continue to run as you're 'consoled' into the node.

### Tunables
All simulation parameters can be overridden with environment variables. Place them before `sudo` so that `sudo -E` passes them through:

```bash
NUM_MOBILE_VMS=64 SIMULATION_DURATION_SECONDS=120 VM_MOVE_PROBABILITY=0.05 \
  sudo -E pytest -s bgp_evpn_capstone_asym
```

| Variable | Default | Description |
|---|---|---|
| `NUM_MOBILE_VMS` | 30 | Number of mobile VM endpoints to create |
| `SIMULATION_DURATION_SECONDS` | 60 | Wall-clock duration of the random-movement simulation |
| `SIMULATION_TICK_SECONDS` | 1.0 | Seconds between each tick (evaluation round) |
| `VM_MOVE_PROBABILITY` | 0.1 | Per-tick probability that any single VM will move (0.0–1.0) |
| `MOBILITY_OVERLAP_SECONDS` | 0.2 | Duplicate-MAC overlap window during each move |
### Packet Capturing
When the test runs it automatically captures BGP packets from a few nodes. If you'd like to get the `.pcap` files off the container and onto your host machine to analyze with Wireshark, the captures are saved at:
`/tmp/topotests/bgp_evpn_capstone.test_evpn_capstone/<node>/<node>_evpn_mobility.pcap`<br>
Note that every time you run the test it will overwrite the previous `.pcap` files.
<br>
Once you have the path to your desired file you can run this command on your host to copy it down:
```command
docker cp <container_id>:/path/to/evpn_mobility.pcap ./evpn_mobility.pcap
```
⚠️**Minor Warning**⚠️
<br>
The test itself will print out some statisics about the capture at the very end, such as the total number of packets captured and how many of those were `MP_UNREACH_NLRI` and `MP_REACH_NLRI` messages.
However, BGP will bundle together multiple `MP_UNREACH_NLRI` and `MP_REACH_NLRI` messages into a single packet. Therefore, it may be necessary to count each instance of the string in the entire capture instead of the number of packets that contain it. Luckily, there's a (somewhat comlpex) command for that. It's dangerous to go alone! Take this:
```command
tshark -r evpn_mobility.pcap -V -Y "bgp" | grep -o "Path Attribute - MP_UNREACH_NLRI" | wc -l
```

#### Wireshark
To filter for BGP Update packets (remember, Withdraw messages are part of Update messages) we can use the following Wireshark filters to find what we're looking for:
* `bgp.type == 2` will show all BGP Update messages captured.
* `bgp.update.path_attribute.type_code == 15` will show all BGP messages that contain `MP_UNREACH_NLRI`, useful for double checking numbers. Type code 14 will show messages with `MP_REACH_NLRI`.

### Measurements
Here are some results from our experiments! All message counts were obtained by searching for all appearances of the attribute string, as described in the previous Packet Capturing section of this document.

#### 64 Robots
Running the test for 120 seconds with 64 robots and a 33% (0.33) chance of movement probability results in these numbers on the `spine1` node:
* 67    `MP_UNREACH_NLRI` packets.
* 1089  `MP_REACH_NLRI` packets.

#### 128 Robots
Running the test for 120 seconds with 128 robots and a 33% (0.33) chance of movement probability results in these numbers on the `spine1` node:
* 170    `MP_UNREACH_NLRI` packets.
* 1869  `MP_REACH_NLRI` packets.

#### 252 Robots
Running the test for 120 seconds with 252 robots and a 33% (0.33) chance of movement probability results in these numbers on the `spine1` node:
* 211    `MP_UNREACH_NLRI` packets.
* 2727   `MP_REACH_NLRI` packets.

##### Analysis
When doubling the number of robots from 64 to 128 we see an increase of 153% in `MP_REACH_NLRI` packets and 71% in `MP_UNREACH_NLRI` packet. When doubling again to 252 we see a 45% increase in `MP_REACH_NLRI` packets. However, we only see a 24% increase in `MP_UNREACH_NLRI`.
<br>
Note that we did not actually double the robots in the last test, simply because our test cannot handle 256 robots due to how our IP address assigning is implemented.