import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ecoflow_api import sign
from planner import ROUND_TRIP, afternoon_sun, balance, daily_solar_kwh, decide, fit_hourly_kw, hourly_kwh, solar_wh_hourly, target_soc, fit_array_kw, hm12, savings, solar_wh, tou_period

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


class AfternoonSun(unittest.TestCase):
    C = {**CFG, "min_soc": 60, "min_soc_cloudy": 80, "sunny_after_wh": 300, "max_soc": 95}

    def test_counts_only_sun_after_deadline(self):
        hourly = [(at(15), 900), (at(16), 380), (at(17), 207), (at(18), 0)]  # 2-3 PM excluded; 3-5 PM counted
        self.assertAlmostEqual(afternoon_sun(at(8), self.C, hourly), 587)
        self.assertIsNone(afternoon_sun(at(8), self.C, []))

    def test_target_by_afternoon_sun(self):
        self.assertEqual(target_soc(self.C, 587)[0], 60)     # sunny afternoon
        self.assertEqual(target_soc(self.C, 120)[0], 80)     # overcast / early sunset
        self.assertEqual(target_soc(self.C, None)[0], 80)    # no forecast: assume no sun
        self.assertEqual(target_soc({**self.C, "max_soc": 75}, 0)[0], 75)  # never above the panel's max charge level
        self.assertIn("little sun after 3 PM", target_soc(self.C, 120)[1])


class PerDpuSolar(unittest.TestCase):
    def test_learns_which_way_each_array_faces(self):
        # a morning-facing array: strong at 9 AM, weak at 3 PM for the same sunlight
        samples = [(9, 3000, 500, 50)] * 20 + [(15, 500, 500, 50)] * 20 + [(12, 2000, 500, 50)] * 5
        p = fit_hourly_kw(samples, max_soc=95)
        self.assertEqual(p, {9: 6.0, 15: 1.0})                      # 12 PM: too few readings yet
        self.assertEqual(fit_hourly_kw([(9, 3000, 500, 95)] * 20, 95), {})  # battery full: throttled, ignored

    def test_forecast_uses_profile_then_fallback(self):
        hourly = [(at(10), 500), (at(16), 500)]                     # 9-10 AM and 3-4 PM at 500 W/m2
        self.assertAlmostEqual(solar_wh_hourly(at(0), at(23), hourly, {9: 6.0}, 2.0), 500 * 6 + 500 * 2)

    def test_daily_kwh_per_dpu(self):
        rows = [(at(9), 1000, 3000), (at(9, 1), 1000, 3000), (at(9, 2), 0, 0), (at(12), 500, 500)]  # gap before noon
        r = daily_solar_kwh(rows)
        self.assertAlmostEqual(r["2026-11-06"][0], 2 * 1000 / 60 / 1000)
        self.assertAlmostEqual(r["2026-11-06"][1], 2 * 3000 / 60 / 1000)

    def test_hourly_solar_home_grid(self):
        # 9:59 -> 10:01 straddles the hour; None solar counts as 0; grid export (negative) isn't "from grid"
        rows = [(at(9, 59), 600, 1200, 600), (at(10, 0), None, 1200, -300), (at(10, 1), 0, 0, 0), (at(13), 0, 0, 0)]
        h = hourly_kwh(rows)
        self.assertEqual(len(h), 24)
        for got, want in zip(h[9], [600 / 60e3, 1200 / 60e3, 600 / 60e3]):
            self.assertAlmostEqual(got, want)
        for got, want in zip(h[10], [0, 1200 / 60e3, 0]):
            self.assertAlmostEqual(got, want)
        self.assertEqual(h[12], [0.0, 0.0, 0.0])                                 # 3 h gap isn't counted


class Balance(unittest.TestCase):
    C = {"bal_full_soc": 90, "bal_release_soc": 75, "bal_sun_left_wh": 100}

    def b(self, d1, held=False, grid=True, charging=False, sun=1000):
        return balance(d1, grid, charging, sun, self.C['bal_sun_left_wh'], held, self.C)[0]

    def test_pause_when_dpu1_nearly_full_and_hold_until_release(self):
        self.assertFalse(self.b(85))
        self.assertTrue(self.b(90))
        self.assertTrue(self.b(80, held=True))    # hysteresis: stays paused between 75 and 90
        self.assertFalse(self.b(75, held=True))   # DPU 1 drained enough: resume DPU 2

    def test_safety_releases(self):
        self.assertFalse(self.b(95, held=True, grid=False))       # outage: need both
        self.assertFalse(self.b(95, held=True, charging=True))    # grid charging both
        self.assertFalse(self.b(95, held=True, sun=40))           # no sun left to make room for
        self.assertTrue(self.b(95, sun=None))                     # no forecast: rule still works on levels


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
