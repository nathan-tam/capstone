# Simulating Host Mobility in Topotest with FRRouting
This documentation details the methodology used to simulate Host Mobility in an EVPN/VXLAN fabric using FRRouting Topotests. It also serves as some basic guidance for running experiments and analysing results. There is a document in the tests/topotests/test_evpn_capstone directory called LISEZ.moi that has more detailed information. The Asymmetrical routing experiment details are located in tests/topotests/test_evpn_capstone_asym in a document called ROUTING_TOPOLOGY_AND_CONFIG.md.
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
1. Change into the test directory: 
2. Use `sudo -E pytest -s bgp_evpn_capstone` to run the test without pauses.
3. Use `sudo -E pytest -s --pause --vtysh=spine1 bgp_evpn_capstone` to run the test with a pause, dropping you into the `spine1` node before the test runs. Note that the test will continue to run as you're 'consoled' into the node.
### Packet Capturing
When the test runs it automatically captures BGP packets from a few nodes. If you'd like to get the `.pcap` file off the container and onto your host machine to analyze with Wireshark the files are saved to:
`/tmp/topotests/bgp_evpn_capstone.test_evpn_capstone/<node>/<node>_evpn_mobility.pcap`
<br>
Once you have the path to your desired file you can run this command on your host to copy it down:
```command
docker cp <container_id>:/path/to/evpn_mobility.pcap ./evpn_mobility.pcap
```
The test itself will print out some statisics about the capture at the very end, such as the total number of packets captured and how many of those were `MP_UNREACH_NLRI` and `MP_REACH_NLRI` messages.
Note that every time you run the test it will overwrite the previous `.pcap` files.

#### Wireshark
To filter for BGP Update packets (remember, Withdraw messages are part of Update messages) we can use the following Wireshark filters to find what we're looking for:
* `bgp.type == 2` will show all BGP Update messages captured.
* `bgp.update.path_attribute.type_code == 15` will show all BGP messages that contain `MP_UNREACH_NLRI`, useful for double checking numbers. Type code 14 will show messages with `MP_REACH_NLRI`.

### The Visualizer
The visualizer runs as a Flask server inside the FRR container and is viewed from your host browser.
💡Important: you will need to stop and re-build the container again with port mapping enabled. A simple container restart is not enough to add new published ports. Here's a quick guide:
1. Stop and remove the existing container with `docker rm docker rm -f $(whoami)-$(basename /bin/pwd)-frr-ubuntu22`
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
There are a few ways you can use to run the server in the background, this instructions use `tmux`.
First, install some Python dependencies:
```command
pip3 install flask flask-socketio requests
```
Start a `tmux` session:
```command
tmux new -s server
```
Navigate to `~/frr/tests/topotest/bgp_evpn_capstone_asym/
And run the server:
```command
python3 visualizer_server.py
```
Open `http://localhost:5000` on your host machine.
Detach from the `tmux` session and run the Topotest. The visualizer will display the topology and endpoint movement events in real time.
