# Simulating Host Mobility in Topotest with FRRouting
This documentation details the methodology used to simulate Host Mobility in an EVPN/VXLAN fabric using FRRouting Topotests. It also serves as some basic guidance for running experiments and analysing results. There is a `LISEZMOI.md` file is each tests' respective directory that contains more in-depth information on how the tests function behind the scenes. Those test directories would be:
* ` /tests/topotests/bgp_evpn_capstone/`
* `/tests/topotests/bgp_evpn_capstone_asym/`
### Overview
"Host Mobility" usually refers to a host migrating from one Access Point to another while retaining its MAC and IP address. The network fabric (EVPN) must detect this move and update its routing tables to send traffic to the new location.<br>
Simulating this in a containerized network test environment (Mininet/Topotest) is challenging because we don't have real hosts to move around. We attempt to simulate this behavior using Linux MACVLAN interfaces.
### Simulation
Instead of moving physical hosts, we use Linux MACVLAN interfaces to represent endpoints. Essentially, we deploy a dummy switch connected to the VTEP to anchor virtual links (our MACVLAN interfaces) onto. This makes it seem like our MACVLAN interfaces are directly connected to the VTEP (meaning traffic from a MACVLAN interface looks exactly like traffic from a distinct physical device attached to the wire). Note that the interfaces are viewable by issuing `ifconfig` in the Docker container.
#### High Level Overview
The script performs the following logic to simulate a migration:
1. An endpoint (e.g., `dummy1` with IP `192.168.0.19`) is created on `host1`.
2. `host1` sends traffic. `vtep1` learns the MAC/IP and advertises it via BGP EVPN to the fabric.
3. The script executes `ip link delete dummy1` on `host1`.
4. The endpoint effectively disappears from the original location.
5. The script executes `ip link add link vtepbond name dummy1 type macvlan mode bridge` on `host2`.
6. It assigns the exact same MAC and IP (`192.168.0.19`) to this new interface.
7. The interface is brought up
9. As soon as the migrated interfaces sends a message, `vtep2` will send a BGP RA to the spine.
10. The spine will then advertise that information to the rest of the network.
11. Other VTEPs receive the update and switch their routing path from `vtep1` to `vtep2`.
### Quick Start
This section assumes you have already completed the FRR Workspace Setup Guide from the Notion wiki and already have the FRR container running.
1. Change into the test directory (mentioned in the preamble of this document)
2. Use `sudo -E pytest -s bgp_evpn_capstone` to run the test without pauses.
3. Use `sudo -E pytest -s --pause --vtysh=spine1 bgp_evpn_capstone` to run the test with a pause, dropping you into the `spine1` node before the test runs. Note that the test will continue to run as you're 'consoled' into the node.
### Packet Capturing
When the test runs it automatically captures BGP packets from a few nodes. If you'd like to get the `.pcap` file off the container and onto your host machine to analyze with Wireshark, the files are saved to:
`/tmp/topotests/bgp_evpn_capstone.test_evpn_capstone/<node>/<node>_evpn_mobility.pcap`
<br>
Once you have the path to your desired file you can run this command on your host to copy it down:
```command
docker cp <container_id>:/path/to/evpn_mobility.pcap ./evpn_mobility.pcap
```
The test itself will print out some statisics about the capture at the very end, such as the total number of packets captured and how many of those were `MP_UNREACH_NLRI` and `MP_REACH_NLRI` messages. At time of writing, these numbers are verified to be accurate.
Note that every time you run the test it will overwrite the previous `.pcap` files.

#### Wireshark
Some quick Wireshark BGP Update filtering tips:
* `bgp.type == 2` will show all BGP Update messages captured.
* `bgp.update.path_attribute.type_code == 15` will show all BGP messages that contain `MP_UNREACH_NLRI`, useful for double checking numbers. Type code 14 will show messages with `MP_REACH_NLRI`.

### The Visualizer
The visualizer runs as a Flask server inside the FRR container and is viewed from your host browser.
<br>
Important: you will need to stop and re-build the container again with port mapping enabled. A simple container restart is not enough to add new published ports. Here's a quick guide:
1. Stop and remove the existing container with
```command
docker rm docker rm -f $(whoami)-$(basename /bin/pwd)-frr-ubuntu22
```
2. Follow the FRRouting Workspace Setup guide on Notion until the `docker run` command (which is basically the second step)
3. Run this modified version of the command:
```command
docker run --init -it --privileged \
-p 5000:5000 \
--name $(whoami)-$(basename /bin/pwd)-frr-ubuntu22 \
-v /lib/modules:/lib/modules \
-v $(pwd):/home/frr/frr \
$(whoami)-$(basename /bin/pwd)-frr-ubuntu22:latest bash
```
4. Continue following the setup guide normally.
#### Running the Web Server
There are a few methods to run the server in the background, these instructions use `tmux`.
First, install some Python dependencies:
```command
pip3 install flask flask-socketio requests
```
Start a `tmux` session:
```command
tmux new -s server
```
Navigate to `~/frr/tests/topotest/bgp_evpn_capstone_asym/` and run the server:
```command
python3 visualizer_server.py
```
Open `http://localhost:5000` on your host machine. If this doesn't immediately work, RESTART YOUR CONTAINER. It's probable that your port mapping is messed up and a restart will quickly fix it.
Detach from the `tmux` session and run the Topotest. The visualizer will display the topology and endpoint movement events in real time.
IMPORTANT: If you are on a Mac, this probably won't work. This is because port 5000 is likely already used for AirPlay Receiver. You can turn this off by going to Settings > General > AirDrop and Handoff and toggling off AirPlay Receiver.
### Live Packet Graph
The asymmetrical mobility test includes a standalone live packet chart page that is separate from the topology view. During the test run, packet totals from active captures are sampled and streamed to this chart. This feature is largely frivolous, and does not represent anything interesting.

#### Launching the Chart Companion
1. Ensure the visualizer server is running (same server process used for the topology UI):
```command
python3 tests/topotests/bgp_evpn_capstone_asym/visualizer_server.py
```
2. Run the asymmetrical test normally:
```command
sudo -E pytest -s tests/topotests/bgp_evpn_capstone_asym/test_evpn_capstone_asym.py
```
3. Open the chart companion at `http://127.0.0.1:5000/packet-chart`

#### Optional Environment Flags
You can tune chart behavior with these environment variables before running the test:
* `ENABLE_LIVE_PACKET_GRAPH=true|false` enables/disables packet sampling events.
* `AUTO_OPEN_PACKET_CHART_WINDOW=true|false` enables/disables automatic browser pop-up.
* `AUTO_START_PACKET_CHART_SERVER=true|false` auto-starts `visualizer_server.py` if port 5000 is not already serving.
* `PACKET_SAMPLE_INTERVAL_SECONDS=<float>` controls sampling interval (default `1.0`, minimum `0.2`).
* `PACKET_CHART_URL=<url>` overrides the chart URL (default `/packet-chart`).

Example:
```command
ENABLE_LIVE_PACKET_GRAPH=true \
AUTO_OPEN_PACKET_CHART_WINDOW=true \
PACKET_SAMPLE_INTERVAL_SECONDS=0.75 \
sudo -E pytest -s tests/topotests/bgp_evpn_capstone_asym/test_evpn_capstone_asym.py
```
