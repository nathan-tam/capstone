#!/usr/bin/env python3

import sys
import time
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from lib.topogen import get_topogen
from lib.topotest import run_and_expect

# Setup topology
os.system("cd /home/frr/frr/tests/topotests && python3 -m pytest bgp_evpn_capstone/test_evpn_capstone.py::test_bgp_convergence -v -s")

print("\n\n=== DEBUGGING MAC LEARNING ===\n")

# After test, check what's in the MAC tables
os.system("sudo vtysh -c 'show evpn vni 1000 mac' 2>/dev/null || echo 'VNI not found'")
