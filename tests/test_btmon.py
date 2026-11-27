import os
import re
import unittest

import helpers  # noqa: F401
from dsd.btmon import HEX_RE, NOISE_RE, find_lost_sdp_request

HERE = os.path.dirname(os.path.abspath(__file__))


def lines_of(text, t0=100.0):
    return [(t0 + i * 0.01, l) for i, l in enumerate(text.strip().splitlines()) if not l.startswith("#")]


RX = """
> ACL: Handle 256 flags 0x02 dlen 12
      L2CAP: Connection Request (0x02) ident 1 len 4
        PSM: 1 (0x0001)
        Source CID: 64
"""
TX = RX.replace("> ACL", "< ACL").replace("ident 1", "ident 7").replace("Source CID: 64", "Source CID: 65")


class FindRequest(unittest.TestCase):
    def test_rx_request_is_found(self):
        self.assertEqual(find_lost_sdp_request(lines_of(RX), 0), (1, 64, "trace"))

    def test_br_acl_wording(self):
        self.assertEqual(find_lost_sdp_request(lines_of(RX.replace("> ACL", "> BR-ACL")), 0), (1, 64, "trace"))

    def test_own_tx_request_is_skipped(self):
        self.assertEqual(find_lost_sdp_request(lines_of(TX + RX), 0), (1, 64, "trace"))

    def test_only_tx_means_guess(self):
        self.assertEqual(find_lost_sdp_request(lines_of(TX), 0), (1, 64, "guess"))

    def test_other_psm_is_skipped(self):
        other = RX.replace("PSM: 1 (0x0001)", "PSM: 17 (0x0011)")
        self.assertEqual(find_lost_sdp_request(lines_of(other), 0)[2], "guess")

    def test_values_come_from_the_trace(self):
        r = RX.replace("ident 1", "ident 5").replace("Source CID: 64", "Source CID: 70")
        self.assertEqual(find_lost_sdp_request(lines_of(r), 0), (5, 70, "trace"))

    def test_since_filters_old_lines(self):
        self.assertEqual(find_lost_sdp_request(lines_of(RX, t0=100.0), 200.0)[2], "guess")

    def test_empty(self):
        self.assertEqual(find_lost_sdp_request([], 0), (1, 64, "guess"))


class Fixtures(unittest.TestCase):
    def test_every_fixture(self):
        d = os.path.join(HERE, "fixtures")
        for name in sorted(os.listdir(d)):
            if not name.endswith(".txt"):
                continue
            text = open(os.path.join(d, name)).read()
            m = re.search(r"# expect: ident=(\d+) scid=(\d+)", text)
            if not m:
                continue
            # the lines of a saved report start with a time stamp: it is cut off like the plugin's own ring does
            body = [(float(i), re.sub(r"^\d\d:\d\d:\d\d\.\d+ ", "", l)) for i, l in enumerate(text.splitlines()) if not l.startswith("#")]
            self.assertEqual(find_lost_sdp_request(body, 0), (int(m.group(1)), int(m.group(2)), "trace"), name)


class Noise(unittest.TestCase):
    def test_patterns(self):
        self.assertTrue(HEX_RE.match("        0a 0b 0c 0d"))
        self.assertTrue(NOISE_RE.search("> ACL: Handle 256 flags 0x02 dlen 12"))
        self.assertFalse(NOISE_RE.search("      L2CAP: Connection Request (0x02) ident 1 len 4"))


if __name__ == "__main__":
    unittest.main()
