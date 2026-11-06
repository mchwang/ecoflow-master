import base64
import os
import tempfile
import unittest
from pathlib import Path

from db import DB

TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP  # before importing server, so tests never touch ./data
import server  # noqa: E402


class Store(unittest.TestCase):
    def test_readings_survive_reopen_and_filter_daylight(self):
        path = Path(TMP) / "t1.db"
        d = DB(path)
        d.add_reading(1000, 40, 0, 600, 600, 0, False)       # night: excluded from solar samples
        d.add_reading(2000, 41, 4000, 600, 0, 500, False)
        d.add_reading(3000, 42, 4200, 650, 0, 520, True)
        d.conn.close()
        d = DB(path)  # reopen: data persisted
        self.assertEqual(d.solar_samples(0), [(4000, 500, 41), (4200, 520, 42)])
        self.assertEqual(d.solar_samples(2500), [(4200, 520, 42)])

    def test_events_newest_first(self):
        d = DB(Path(TMP) / "t2.db")
        for i in range(3):
            d.add_event(i, f"e{i}")
        self.assertEqual(d.recent_events(2), [(2, "e2"), (1, "e1")])


class Lease(unittest.TestCase):
    def test_latest_claim_wins_and_expiry_fails_over(self):
        d = DB(Path(TMP) / "t3.db")
        self.assertEqual(d.claim_lease("vps", 0, 300, force=True), "vps")
        self.assertEqual(d.claim_lease("laptop", 10, 300), "vps")                # vps still fresh: laptop waits
        self.assertEqual(d.claim_lease("laptop", 20, 300, force=True), "laptop")  # laptop starts: takes over
        self.assertEqual(d.claim_lease("vps", 30, 300), "laptop")                # vps goes standby
        self.assertEqual(d.claim_lease("laptop", 200, 300), "laptop")            # renew
        self.assertEqual(d.claim_lease("vps", 400, 300), "laptop")               # renewed at 200: still fresh
        self.assertEqual(d.claim_lease("vps", 501, 300), "vps")                  # laptop silent >300 s: failover
        self.assertEqual(d.peek_lease(502, 300), "vps")                          # peek sees the holder...
        self.assertEqual(d.claim_lease("laptop", 503, 300), "vps")               # ...and peeking changed nothing
        self.assertIsNone(d.peek_lease(900, 300))                                # nobody renewed: no holder


def basic(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


class Auth(unittest.TestCase):
    def setUp(self):
        server.USER, server.PASSWORD = "admin", "s3cret"

    def tearDown(self):
        server.PASSWORD = ""

    def test_requires_correct_credentials(self):
        self.assertTrue(server.authorized(basic("admin", "s3cret")))
        self.assertFalse(server.authorized(basic("admin", "wrong")))
        self.assertFalse(server.authorized(basic("root", "s3cret")))
        self.assertFalse(server.authorized(""))
        self.assertFalse(server.authorized("Basic !!!notbase64"))


if __name__ == "__main__":
    unittest.main()
