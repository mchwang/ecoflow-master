import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ecoflow_api import sign
from planner import ROUND_TRIP, decide, fit_array_kw, hm12, savings, solar_wh, tou_period

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


class Savings(unittest.TestCase):
    OFF, PEAK = 0.22558, 0.41099  # EV2-A winter $/kWh (test day is in November)

    def test_grid_charged_off_peak_used_at_peak(self):
        rows = [(at(2), 500, 1750, 0),    # 2-3 AM: 1.25 kWh from grid into the battery -> 1.0 kWh usable
                (at(3), 500, 500, 0),
                (at(17), 1000, 0, 0),     # 5-6 PM peak: battery runs the 1 kW house
                (at(18), 1000, 1000, 0)]
        r = savings(rows, at(0), max_gap_s=3600)
        self.assertAlmostEqual(r["today"], 1.0 * self.PEAK - 1.25 * self.OFF)  # 80% round trip on the grid energy
        self.assertAlmostEqual(r["today_kwh"], 1.0)

    def test_solar_is_free(self):
        rows = [(at(13), 1000, 0, 1000), (at(14), 1000, 0, 1000)]  # solar runs the house directly
        self.assertAlmostEqual(savings(rows, at(0), max_gap_s=3600)["today"], 1.0 * self.OFF)

    def test_solar_stored_then_used_is_free_minus_losses(self):
        rows = [(at(12), 0, 0, 1250), (at(13), 0, 0, 0),  # 1.25 kWh solar into battery -> 1.0 usable, cost 0
                (at(18), 1000, 0, 0), (at(19), 0, 0, 0)]
        r = savings(rows, at(0), max_gap_s=3600)
        self.assertAlmostEqual(r["today"], 1.0 * self.PEAK)
        self.assertEqual(ROUND_TRIP, 0.8)

    def test_grid_only_saves_nothing_and_gaps_skipped(self):
        rows = [(at(9), 700, 700, 0), (at(9, 1), 700, 700, 0), (at(12), 1000, 0, 0)]  # 3 h gap: app down
        self.assertEqual(savings(rows, at(0))["today"], 0)

    def test_today_vs_total(self):
        rows = [(at(13) - timedelta(days=1), 1000, 0, 1000), (at(14) - timedelta(days=1), 0, 0, 0),
                (at(13), 1000, 0, 1000), (at(14), 0, 0, 0)]
        r = savings(rows, at(0), max_gap_s=3600)
        self.assertAlmostEqual(r["today"], self.OFF); self.assertAlmostEqual(r["total"], 2 * self.OFF)
        self.assertEqual(sorted(r["by_day"]), ["2026-11-05", "2026-11-06"])
        self.assertAlmostEqual(r["by_day"]["2026-11-05"][0], self.OFF); self.assertAlmostEqual(r["by_day"]["2026-11-06"][1], 1.0)


class Planner(unittest.TestCase):
    def test_never_charges_in_peak_or_part_peak(self):
        for h in (15, 17, 22):
            self.assertFalse(decide(at(h), 10, CFG)["charge"])
            self.assertEqual(decide(at(h), 10, CFG)["kind"], "outside")

    def test_sunny_day_no_grid(self):
        pv = solar_wh(at(6), at(15), sky(800), 8, .8)  # ~30 kWh
        d = decide(at(6), 12, CFG, pv)
        self.assertFalse(d["charge"]); self.assertGreaterEqual(d["projected_soc"], 60)
        self.assertIn("OK", d["reason"]); self.assertEqual(d["kind"], "ok")

    def test_cloudy_day_charges_only_the_shortfall(self):
        pv = solar_wh(at(6), at(15), sky(150), 8, .8)  # ~5 kWh -> 12% + ~16% = ~28%
        d = decide(at(6), 12, CFG, pv)
        self.assertFalse(d["charge"])
        # shortfall (60 - projected)% of 30.72 kWh at 3.6 kW effective + 30 min margin, counted back from 15:00
        missing = (60 - d["projected_soc"]) / 100 * 30720
        expect = at(15) - timedelta(minutes=missing / 3600 * 60 + 30)
        self.assertLess(abs((d["start_at"] - expect).total_seconds()), 60)
        self.assertEqual(d["kind"], "planned")
        later = decide(d["start_at"] + timedelta(minutes=1), 12, CFG, pv)
        self.assertTrue(later["charge"]); self.assertEqual(later["kind"], "charging")

    def test_no_solar_info_charges_full_shortfall(self):
        d = decide(at(6), 12, CFG, 0)
        self.assertEqual(d["projected_soc"], 12)
        self.assertEqual(d["start_at"].strftime("%H:%M"), "10:24")  # 48% of 30.72 kWh / 3.6 kW = 4h06 + 30m

    def test_keeps_charging_past_min_then_stops(self):
        self.assertTrue(decide(at(13), 61, CFG, 0, charging_by_us=True)["charge"])   # within 2% buffer
        self.assertFalse(decide(at(13), 62, CFG, 0, charging_by_us=True)["charge"])
        self.assertFalse(decide(at(15), 30, CFG, 0, charging_by_us=True)["charge"])  # deadline passed

    def test_12_hour_times(self):
        self.assertEqual([hm12(x) for x in ("00:00", "09:05", "12:00", "15:00", "23:59")],
                         ["12 AM", "9:05 AM", "12 PM", "3 PM", "11:59 PM"])
        self.assertIn("starts 10:24 AM", decide(at(6), 12, CFG, 0)["reason"])

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
