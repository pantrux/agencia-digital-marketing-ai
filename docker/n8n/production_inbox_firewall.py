#!/usr/bin/python3
"""Administra sólo la regla que une el contenedor n8n con su receptor local."""

import os
import subprocess
import sys

IPTABLES = "/usr/sbin/iptables"
RULE = [
    "-i",
    "br-ag-n8n-in",
    "-s",
    "172.31.240.2/32",
    "-d",
    "172.31.240.1/32",
    "-p",
    "tcp",
    "--dport",
    "3160",
    "-m",
    "comment",
    "--comment",
    "agencia-digital-marketing-ai:n8n-production-inbox:v1",
    "-j",
    "ACCEPT",
]


def rule(action):
    args = [IPTABLES, "-w", "3", action, "INPUT", *(["1"] if action == "-I" else []), *RULE]
    return subprocess.run(args, capture_output=True, timeout=5).returncode


if __name__ == "__main__":
    if os.geteuid() != 0 or os.uname().nodename != "vm-media-01" or len(sys.argv) != 2:
        sys.exit(1)
    current = rule("-C")
    if sys.argv[1] == "apply":
        sys.exit(0 if current == 0 else rule("-I") if current == 1 else current)
    if sys.argv[1] == "remove":
        sys.exit(0 if current == 1 else rule("-D") if current == 0 else current)
    sys.exit(1)
