import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ecoflow_api import sign
from planner import decide, fit_array_kw, solar_wh, tou_period

TZ = ZoneInfo("America/Los_Angeles")
CFG = {"capacity_wh": 30720, "charge_w": 4000, "min_soc": 60, "max_soc": 95,
       "offpeak_start": "00:00", "offpeak_end": "15:00", "margin_min": 30}


def at(h, m=0, month=11):
    return datetime(2026, month, 6, h, m, tzinfo=TZ)


def sky(peak_ghi):
    """Hourly (end_of_hour, W/m2): bell curve 7am-5pm peaking at noon, 0 at night."""
    return [(at(0) + timedelta(hours=h), max(0, peak_ghi * (1 - abs(h - 12.5) / 5.5))) for h in range(1, 25)]


class Solar(unittest.TestCase):
    def test_counts_only_daylight_before_deadline(self):
        full_day = solar_wh(at(0), at(23, 59), sky(800), 10, 1)
        before_3 = solar_wh(at(0), at(15), sky(800), 10, 1)
        self.assertGreater(full_day, before_3)
        self.assertEqual(solar_wh(at(18), at(23), sky(800), 10, 1), 0)  # after sunset: nothing left

    def test_partial_hour(self):
        hourly = [(at(13), 1000)]  # 12:00-13:00 at 1000 W/m2, 1 kW array -> 1000 Wh for the full hour
        self.assertAlmostEqual(solar_wh(at(12, 30), at(15), hourly, 1, 1), 500)

    def test_cloudy_day_makes_less(self):
        self.assertLess(solar_wh(at(6), at(15), sky(150), 8, .8), solar_wh(at(6), at(15), sky(800), 8, .8))


class Learn(unittest.TestCase):
    def test_fits_array_and_round_trips_through_forecast(self):
        # an 8 kW-effective array: measured W = 8 x W/m2
        samples = [(8 * g, g, 40) for g in (100, 300, 600, 900)]
        kw, n = fit_array_kw(samples, max_soc=95)
        self.assertAlmostEqual(kw, 8); self.assertEqual(n, 4)
        self.assertAlmostEqual(solar_wh(at(11), at(12), [(at(12), 500)], kw, 1), 4000)

    def test_skips_dim_hours_and_throttled_full_battery(self):
        samples = [(8 * 600, 600, 40), (500, 20, 40), (1000, 800, 95)]  # dawn noise; clipped at full
        self.assertEqual(fit_array_kw(samples, max_soc=95), (8, 1))

    def test_no_data(self):
        self.assertEqual(fit_array_kw([], 95), (0.0, 0))


class Planner(unittest.TestCase):
    def test_never_charges_in_peak_or_part_peak(self):
        for h in (15, 17, 22):
            self.assertFalse(decide(at(h), 10, CFG)["charge"])

    def test_sunny_day_no_grid(self):
        pv = solar_wh(at(6), at(15), sky(800), 8, .8)  # ~30 kWh
        d = decide(at(6), 12, CFG, pv)
        self.assertFalse(d["charge"]); self.assertGreaterEqual(d["projected_soc"], 60)
        self.assertIn("OK", d["reason"])

    def test_cloudy_day_charges_only_the_shortfall(self):
        pv = solar_wh(at(6), at(15), sky(150), 8, .8)  # ~5 kWh -> 12% + ~16% = ~28%
        d = decide(at(6), 12, CFG, pv)
        self.assertFalse(d["charge"])
        # shortfall (60 - projected)% of 30.72 kWh at 3.6 kW effective + 30 min margin, counted back from 15:00
        missing = (60 - d["projected_soc"]) / 100 * 30720
        expect = at(15) - timedelta(minutes=missing / 3600 * 60 + 30)
        self.assertLess(abs((d["start_at"] - expect).total_seconds()), 60)
        self.assertTrue(decide(d["start_at"] + timedelta(minutes=1), 12, CFG, pv)["charge"])

    def test_no_solar_info_charges_full_shortfall(self):
        d = decide(at(6), 12, CFG, 0)
        self.assertEqual(d["projected_soc"], 12)
        self.assertEqual(d["start_at"].strftime("%H:%M"), "10:24")  # 48% of 30.72 kWh / 3.6 kW = 4h06 + 30m

    def test_keeps_charging_past_min_then_stops(self):
        self.assertTrue(decide(at(13), 61, CFG, 0, charging_by_us=True)["charge"])   # within 2% buffer
        self.assertFalse(decide(at(13), 62, CFG, 0, charging_by_us=True)["charge"])
        self.assertFalse(decide(at(15), 30, CFG, 0, charging_by_us=True)["charge"])  # deadline passed

    def test_tou(self):
        self.assertEqual(tou_period(at(17, month=7))[:2], ("peak", "summer"))
        self.assertEqual(tou_period(at(15))[0], "part-peak")
        self.assertEqual(tou_period(at(23))[0], "part-peak")
        self.assertEqual(tou_period(at(2))[0], "off-peak")


class Signing(unittest.TestCase):
    def test_flatten_sorted_and_signed(self):
        # Hand-computed: sorted "params.ch1ForceCharge=FORCE_CHARGE_ON&sn=X&accessKey=a&nonce=1&timestamp=2"
        import hashlib, hmac
        msg = "cmdCode=PD303_APP_SET&params.ch1ForceCharge=FORCE_CHARGE_ON&params.on=true&sn=X&accessKey=a&nonce=1&timestamp=2"
        want = hmac.new(b"s", msg.encode(), hashlib.sha256).hexdigest()
        got = sign({"sn": "X", "cmdCode": "PD303_APP_SET", "params": {"ch1ForceCharge": "FORCE_CHARGE_ON", "on": True}}, "a", "s", "1", "2")
        self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
