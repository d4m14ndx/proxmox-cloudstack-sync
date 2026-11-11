#!/usr/bin/env python3
"""Report the IPv4 address each running guest NIC is using, read from its ARP.

Runs on a Proxmox node. Listens to ARP for a short time and keeps what guests
themselves send on their tap interfaces. A NIC is reported only when it claims
exactly one address, so routers and hosts with several addresses are skipped.

  pcs-observe-ips.py            print observations
  pcs-observe-ips.py --post     send them to the sync service (URL and token
                                from /etc/pve/priv/pcs-observe.json)
"""
import json
import re
import subprocess
import sys
import urllib.request

SECONDS = 60
CONFIG = "/etc/pve/priv/pcs-observe.json"
LINE = re.compile(
    r"^\S+ (tap\d+i\d+)\s+(\S+)\s+ifindex \d+ ([0-9a-f:]{17}) .*?"
    r"(?:tell (\d+\.\d+\.\d+\.\d+)|Reply (\d+\.\d+\.\d+\.\d+) is-at)")


def observe(seconds=SECONDS):
    out = subprocess.run(
        ["timeout", str(seconds), "tcpdump", "-i", "any", "-nn", "-e", "-l", "arp"],
        capture_output=True, text=True).stdout
    seen = {}
    for line in out.splitlines():
        m = LINE.match(line)
        # "Out" is the host sending to the guest; everything else came from it.
        if not m or m.group(2) == "Out":
            continue
        ip = m.group(4) or m.group(5)
        if ip.startswith(("0.", "127.", "169.254.")):
            continue
        seen.setdefault(m.group(3).upper(), set()).add(ip)
    return [{"mac": mac, "ip": ips.pop()} for mac, ips in sorted(seen.items())
            if len(ips) == 1]


if __name__ == "__main__":
    found = observe()
    if "--post" not in sys.argv:
        print(json.dumps(found, indent=1))
        sys.exit(0)
    cfg = json.load(open(CONFIG))
    req = urllib.request.Request(
        cfg["url"], data=json.dumps({"observations": found}).encode(),
        headers={"Content-Type": "application/json",
                 "X-Observed-Ip-Token": cfg["token"]})
    print(urllib.request.urlopen(req, timeout=30).read().decode())
