"""btmon trace: line patterns, the L2CAP response script, search for the request the kernel dropped."""
import re

# btmon prints a received packet as "> ACL: Handle 256 ..." (before the link type is known) or "> BR-ACL: ...", a sent one with "<"
ACL_RX_RE = re.compile(r"^\s*>\s+(?:BR-|LE-)?ACL\b")
ACL_TX_RE = re.compile(r"^\s*<\s+(?:BR-|LE-)?ACL\b")
NOISE_RE = re.compile(r"^\s*[<>]\s+(?:BR-|LE-)?ACL\b|Number of Completed|Num handles|^\s+Count:|Latency:|#\d+: len|Channel: \d+ len")
HEX_RE = re.compile(r"^\s+[0-9a-f]{2}( [0-9a-f]{2}){3,}")
INJECT_SCRIPT = r"""
import socket, struct, sys
dev, handle, result, ident, scid = [int(x) for x in sys.argv[1:6]]
sig = struct.pack("<BBHHHHH", 3, ident, 8, 0, scid, result, 0)
l2 = struct.pack("<HH", len(sig), 1) + sig
acl = struct.pack("<HH", handle | (2 << 12), len(l2)) + l2
pkt = b"\x02" + acl
s = socket.socket(31, socket.SOCK_RAW, 1)
s.bind((dev,))
print("sent", s.send(pkt), "bytes", pkt.hex())
"""


def find_lost_sdp_request(lines, since):
    """From the HCI trace: the controller's SDP request (PSM 1) that the kernel dropped.
    lines: (timestamp, text) pairs in the order they were recorded; since: the earliest timestamp to look at.
    Returns (ident, scid, "trace"), or the usual first-request values (1, 64, "guess") when the trace has no such request."""
    direction, ident, psm, scid = None, None, None, None
    for ts, line in lines:
        if ts < since:
            continue
        if line.lstrip()[:1] in (">", "<", "@", "="):  # a new packet or event starts: remember its direction
            direction = "rx" if ACL_RX_RE.match(line) else "tx" if ACL_TX_RE.match(line) else None
        m = re.search(r"L2CAP: Connection Request \(0x02\) ident (\d+)", line)
        if m:
            ident, psm, scid = int(m.group(1)), None, None
            continue
        if ident is not None:
            mp = re.search(r"PSM: (\d+)", line)
            if mp:
                psm = int(mp.group(1))
            ms = re.search(r"Source CID: (\d+)", line)
            if ms:
                scid = int(ms.group(1))
            if psm == 1 and scid is not None:
                if direction == "rx":
                    return ident, scid, "trace"
                ident, psm, scid = None, None, None  # our own request (TX): not the lost one
    return 1, 64, "guess"
