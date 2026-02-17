#!/usr/bin/env python

def display_MAC_EVPN(tgen):
    vtep1 = tgen.gears["vtep1"]                             # retrieve the first VTEP
    version = vtep1.vtysh_cmd("show evpn mac vni 1000")     # run a command to display the EVPN MAC table
    print("EVPN MAC table: " + version)                     # print the output to the console
