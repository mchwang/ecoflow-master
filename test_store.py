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


class DailySavings(unittest.TestCase):
    def test_upsert_keeps_history(self):
        d = DB(Path(TMP) / "t4.db")
        d.save_daily_savings({"2026-10-31": [1.5, 6.0]}, 1)                  # no solar split: stored as 0
        d.save_daily_savings({"2026-11-01": [0.2, 1.0, 1.0, 2.0]}, 2)
        d.save_daily_savings({"2026-11-01": [0.5, 2.0, 3.0, 4.0]}, 3)        # same day recomputed: replaced, not added
        self.assertEqual(d.daily_savings(), [("2026-10-31", 1.5, 6.0, 0.0, 0.0, 0.0, 0.0), ("2026-11-01", 0.5, 2.0, 3.0, 4.0, 0.0, 0.0)])
        self.assertEqual(d.daily_savings("2026-11-01"), [("2026-11-01", 0.5, 2.0, 3.0, 4.0, 0.0, 0.0)])


class PerDpuAndWeather(unittest.TestCase):
    def test_upgrades_an_existing_database(self):
        import sqlite3
        path = Path(TMP) / "old.db"
        c = sqlite3.connect(path)  # schema as first released, with one old row
        c.executescript("CREATE TABLE readings (ts INTEGER PRIMARY KEY, soc REAL, solar_w REAL, load_w REAL, grid_w REAL,"
                        " forecast_ghi REAL, charging INTEGER); INSERT INTO readings VALUES (100, 50, 300, 700, 700, 400, 0);"
                        "CREATE TABLE daily_savings (day TEXT PRIMARY KEY, saved REAL NOT NULL, kwh REAL NOT NULL, updated INTEGER NOT NULL);")
        c.commit(); c.close()
        d = DB(path)
        d.add_reading(200, 51, 500, 700, 200, 450, 0, 120, 380, 52, 49)
        # old solar came from the wrong panel field, so it is cleared once; later readings are kept
        self.assertEqual(d.readings_since(0), [(100, 50, 700, 700, None, None, None), (200, 51, 700, 200, 500, 120, 380)])
        d.conn.close()
        self.assertEqual(DB(path).readings_since(0)[-1], (200, 51, 700, 200, 500, 120, 380))

    def test_battery_energy_by_reading(self):
        d = DB(Path(TMP) / "t6.db")
        d.add_reading(100, 12, 0, 0, 0, 0, 0)                          # older reading without the Wh value
        d.add_reading(200, 12, 0, 0, 0, 0, 0, remain_wh=3700.5)
        d.add_reading(300, 13, 0, 0, 0, 0, 0, remain_wh=3900.25)
        d.add_reading(400, 14, 0, 0, 0, 0, 0, remain_wh=4200.0)        # outside the range asked for
        self.assertEqual(d.battery_wh(0, 400), {200: 3700.5, 300: 3900.25})   # only readings that have it, in range

    def test_learning_uses_stored_weather_then_forecast(self):
        d = DB(Path(TMP) / "t5.db")
        d.add_reading(3600 * 10 + 60, 50, 500, 0, 0, 400, 0, 200, 300, 50, 50)    # hour ending 11*3600
        d.add_reading(3600 * 11 + 60, 50, 500, 0, 0, 450, 0, 210, 290, 50, 50)    # hour ending 12*3600: no weather row
        d.save_weather([(3600 * 11, 520, 400, 120, 10, 18)], now=1)
        rows = d.dpu_samples(0)
        self.assertEqual([r[5] for r in rows], [520, 450])                       # stored estimate, else forecast


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
