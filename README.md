# Simulating Host Mobility in Topotest with FRRouting
```important
💡Note! You may need to re-setup the FRR Workspace if you are cloning this!
```
This documentation details the methodology used to simulate Host Mobility in an EVPN/VXLAN fabric using FRRouting Topotests.
### Overview
"Host Mobility" usually refers to a host migrating from one Access Point to another while retaining its MAC and IP address. The network fabric (EVPN) must detect this move and update its routing tables to send traffic to the new location.<br>
Simulating this in a containerized network test environment (Mininet/Topotest) is challenging because we don't have real hosts to move around. We attempt to simulate this behavior using Linux MACVLAN interfaces.
### Simulation
Instead of moving physical hosts, we use Linux MACVLAN interfaces to represent endpoints. Essentially, we deploy a dummy switch connected to the VTEP to anchor virtual links (our MACVLAN interfaces) onto. This makes it seem like our MACVLAN interfaces are directly connected to the VTEP (meaning traffic from a MACVLAN interface looks exactly like traffic from a distinct physical device attached to the wire). Note that the interfaces are viewable by issuing `ifconfig` in the Docker container.
#### How it Works, Technically
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
#### Running the Experiment
We assume you have already followed a guide setup your FRRouting workspace.
1. From the root of your workspace, launch the container: `docker start $(whoami)-$(basename /bin/pwd)-frr-ubuntu20`.
2. And attach to it: `docker attach $(whoami)-$(basename /bin/pwd)-frr-ubuntu20`.
3. Navigate to the tests directory: `cd ~/frr/tests/topotests`.
4. Run `sudo -E pytest -s --pause --vtysh=torm11 bgp_evpn_capstone`. This will launch the topology and connect you to router `torm11`.
