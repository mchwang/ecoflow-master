import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ecoflow_api import sign
from planner import afternoon_sun, evening_target, balance, hold_battery, daily_solar_kwh, day_report, decide, fit_hourly_kw, hourly_kwh, room_target, simulate, solar_wh_hourly, target_soc, fit_array_kw, hm12, solar_wh, tou_period

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
    C = {"bal_full_soc": 90, "bal_release_soc": 75, "bal_sun_left_wh": 100, "bal_max_spread": 10}

    def b(self, d1, d2=None, paused=0, grid=True, charging=False, sun=1000):
        d2 = d1 if d2 is None else d2
        return balance(d1, d2, grid, charging, sun, self.C['bal_sun_left_wh'], paused, self.C)[0]

    def test_pause_when_dpu1_nearly_full_and_hold_until_release(self):
        self.assertEqual(self.b(85), 0)
        self.assertEqual(self.b(90), 2)
        self.assertEqual(self.b(80, 82, paused=2), 2)    # hysteresis: stays paused between 75 and 90
        self.assertEqual(self.b(75, 77, paused=2), 0)    # DPU 1 drained enough: resume DPU 2

    def test_making_room_never_drains_dpu1_far_below_dpu2(self):
        self.assertEqual(self.b(84, 88, paused=2), 2)
        self.assertEqual(self.b(83, 88, paused=2), 0)    # 5% below DPU 2: resume, well inside the 10% limit
        self.assertEqual(self.b(90, 95), 0)              # and never pauses there (live 2026-11-13: paused/resumed every 30 s)

    def test_keeps_the_two_within_ten_percent(self):
        self.assertEqual(self.b(60, 51), 0)
        self.assertEqual(self.b(60, 50), 2)              # DPU 1 10% higher: pause DPU 2, the house drains DPU 1
        self.assertEqual(self.b(50, 60), 1)              # DPU 2 10% higher: pause DPU 1
        self.assertEqual(self.b(54, 50, paused=2), 2)    # keeps going until they're within 3%
        self.assertEqual(self.b(53, 50, paused=2), 0)
        self.assertEqual(self.b(47, 50, paused=1), 0)
        self.assertEqual(self.b(50, 60, sun=40), 1)      # spread rule runs with no sun left too
        self.assertEqual(self.b(85, 95), 1)              # a spread wins over making room for DPU 1

    def test_safety_releases(self):
        self.assertEqual(self.b(95, paused=2, grid=False), 0)       # outage: need both
        self.assertEqual(self.b(50, 70, grid=False), 0)
        self.assertEqual(self.b(95, paused=2, charging=True), 0)    # grid charging both
        self.assertEqual(self.b(95, paused=2, sun=40), 0)           # no sun left to make room for
        self.assertEqual(self.b(95, sun=None), 2)                   # no forecast: rule still works on levels


class DayReport(unittest.TestCase):
    OFF, PEAK = 0.22558, 0.41099  # EV2-A winter $/kWh (test day is in November)
    CAP = 10000                   # 10 kWh battery: 1% = 0.1 kWh

    def test_grid_charged_off_peak_used_at_peak(self):
        rows = [(at(2), 50, 500, 1750, 0),    # 2-3 AM: house 0.5 kW from grid + 1.25 kWh into the battery
                (at(3), 60, 500, 500, 0),     # 1.25 kWh in -> +1.0 kWh stored
                (at(17), 60, 1000, 0, 0),     # 5-6 PM peak: battery runs the 1 kW house
                (at(18), 50, 1000, 1000, 0)]
        r = day_report(rows, self.CAP, max_gap_s=50000)   # 3 AM-5 PM is skipped as downtime
        self.assertAlmostEqual(r["grid_home"]["off-peak"], 0.5); self.assertAlmostEqual(r["grid_batt"]["off-peak"], 1.25)
        self.assertAlmostEqual(r["batt_home"], 1.0); self.assertAlmostEqual(r["home_kwh"], 1.5)
        self.assertAlmostEqual(r["stored_kwh"], 0); self.assertAlmostEqual(r["losses_kwh"], 0.25)   # 20% of 1.25 lost
        self.assertAlmostEqual(r["cost_without"], 0.5 * self.OFF + 1.0 * self.PEAK)
        self.assertAlmostEqual(r["grid_cost"], 1.75 * self.OFF)
        self.assertAlmostEqual(r["saved"], 1.0 * self.PEAK - 1.25 * self.OFF)   # same as the old cost-pool model

    def test_solar_is_free_and_gaps_skipped(self):
        rows = [(at(13), 50, 1000, 0, 1000), (at(14), 50, 700, 700, 0),   # 1-2 PM solar runs the house
                (at(14, 1), 50, 700, 700, 0), (at(18), 50, 0, 0, 0)]       # then a 4 h gap (app down): not counted
        r = day_report(rows, self.CAP, max_gap_s=3600)
        self.assertAlmostEqual(r["solar_home"], 1.0); self.assertAlmostEqual(r["saved"], 1.0 * self.OFF)
        self.assertAlmostEqual(r["home_kwh"], 1.0 + 0.7 / 60)

    def test_precise_stored_energy_gives_losses_and_efficiency(self):
        rows = [(at(2), 50, 0, 1000, 0, 5000), (at(3), 50, 0, 0, 0, 5900)]  # 1 kWh in; Wh reading +0.9, % didn't move
        r = day_report(rows, self.CAP, max_gap_s=3600)
        self.assertAlmostEqual(r["stored_kwh"], 0.9); self.assertAlmostEqual(r["losses_kwh"], 0.1)
        self.assertAlmostEqual(r["energy_in"], 1.0); self.assertAlmostEqual(r["loss_pct"], 10)
        self.assertAlmostEqual(r["batt_eff"], 90)                     # 0.9 of the 1.0 kWh charged is still there

    def test_battery_gain_counts_toward_savings(self):
        rows = [(at(2), 50, 0, 1000, 0, 5000), (at(3), 50, 0, 0, 2000, 7900),   # 1 kWh grid charge, then 2 kWh solar
                (at(4), 50, 0, 0, 0, 7900)]                                         # +2.9 kWh in the battery
        r = day_report(rows, self.CAP, max_gap_s=3600)
        self.assertAlmostEqual(r["battery_value"], 2.9 * self.OFF)
        self.assertAlmostEqual(r["saved"], 2.9 * self.OFF - 1.0 * self.OFF)   # grid charge paid, stored solar credited

    def test_meter_gap_is_reported_not_hidden(self):
        rows = [(at(2), 50, 0, 1000, 0, 5000), (at(3), 50, 0, 0, 0, 6100)]  # meters say more stored than went in
        r = day_report(rows, self.CAP, max_gap_s=3600)
        self.assertAlmostEqual(r["losses_kwh"], 0)                    # the flow diagram can't draw a negative
        self.assertAlmostEqual(r["balance_kwh"], -0.1)                # but the balance shows the gap
        self.assertIsNone(r["batt_eff"])                              # and efficiency isn't guessed

    def test_app_down_is_not_counted_as_losses(self):
        # App down 5:01-7:01 PM while the battery ran the house: the level drop over the gap isn't counted either.
        rows = [(at(17), 80, 1000, 0, 0, 24576), (at(17, 1), 80, 1000, 0, 0, 24560),
                (at(19, 1), 73, 1000, 0, 0, 22400), (at(19, 2), 73, 0, 0, 0, 22384)]
        r = day_report(rows, 30720, max_gap_s=300)
        self.assertAlmostEqual(r["drawn_kwh"], 0.032); self.assertAlmostEqual(r["losses_kwh"], 0, places=3)

    def test_efficiency_when_the_day_uses_earlier_charge(self):
        rows = [(at(9), 60, 0, 200, 0), (at(10), 60, 4500, 0, 0), (at(11), 15, 0, 0, 0)]  # 0.2 in, then 4.5 out of 6 -> 1.5
        r = day_report(rows, 10000, max_gap_s=3600)
        self.assertAlmostEqual(r["batt_eff"], (4.5 + 0) / (0.2 + 4.5) * 100)          # ~96%, not 0%

    def test_sources_balance_sinks(self):
        rows = [(at(9), 20, 800, 300, 900), (at(10), 25, 1200, 0, 400), (at(11), 22, 600, 3500, 100), (at(12), 40, 0, 0, 0)]
        r = day_report(rows, self.CAP, max_gap_s=3600)
        into = r["solar_kwh"] + sum(r["grid_kwh"].values()) + r["drawn_kwh"]
        self.assertAlmostEqual(into, r["home_kwh"] + r["stored_kwh"] + r["losses_kwh"])
        self.assertAlmostEqual(r["stored_kwh"], 2.0)   # 20% -> 40% of 10 kWh



class Simulate(unittest.TestCase):
    CAP = 10000  # 10 kWh battery: 1% = 100 Wh

    def run_(self, soc, ghi, load, start=9, end=12, kw=1.0):
        return simulate(at(start), at(end), soc, [(at(h), ghi) for h in range(1, 24)], {}, kw, {}, load, self.CAP, 10, 95)

    def test_house_drains_battery_down_to_reserve_only(self):
        r = self.run_(30, 0, 1000)                             # 3 h x 1 kW from a 30% battery: 30% -> stops at 10%
        self.assertAlmostEqual(r["soc"], 10); self.assertAlmostEqual(r["wasted_wh"], 0)

    def test_sun_beyond_house_charges_and_overflow_is_wasted(self):
        r = self.run_(50, 1000, 500, kw=2.0)                   # +1.5 kW for 3 h = 4.5 kWh: 50% -> 95%, 0 kWh over
        self.assertAlmostEqual(r["soc"], 95); self.assertAlmostEqual(r["wasted_wh"], 0)
        r = self.run_(80, 1000, 500, kw=2.0)                   # 80% + 45% -> capped at 95%, 3 kWh wasted
        self.assertAlmostEqual(r["soc"], 95); self.assertAlmostEqual(r["wasted_wh"], 3000)
        self.assertAlmostEqual(r["solar_wh"], 6000); self.assertAlmostEqual(r["home_wh"], 1500)

    def test_learned_load_per_hour(self):
        r = simulate(at(9), at(11), 50, [], {}, 0, {9: 2000, 10: 0}, 500, self.CAP, 10, 95)  # 9-10 AM 2 kW, 10-11 AM 0
        self.assertAlmostEqual(r["soc"], 30)

    def test_target_leaves_room_for_afternoon_sun(self):
        hourly = [(at(h), 1000 if 16 <= h <= 18 else 0) for h in range(1, 24)]  # sun 3-6 PM: 3 h x (2 kW - 0.5 kW)
        t, room = room_target(80, at(15), hourly, {}, 2.0, {}, 500, self.CAP, 10, 95)
        self.assertAlmostEqual(t, 50); self.assertAlmostEqual(room, 45)   # 4.5 kWh surplus needs 45% free at 3 PM
        self.assertEqual(room_target(40, at(15), hourly, {}, 2.0, {}, 500, self.CAP, 10, 95)[0], 40)  # already fits

    def test_target_covers_only_the_evening_need(self):
        dark = [(at(h), 0) for h in range(24)]
        # 500 W from 3 PM to midnight = 4.5 kWh, +10% headroom = 4.95 kWh = 49.5% on top of the 10% reserve
        t, need = evening_target(95, at(15), dark, {}, 0, {}, 500, self.CAP, 10, 95)
        self.assertAlmostEqual(need, 49.5); self.assertAlmostEqual(t, 59.5)
        self.assertEqual(evening_target(40, at(15), dark, {}, 0, {}, 500, self.CAP, 10, 95)[0], 40)  # never raises
        self.assertEqual(evening_target(60, at(15), dark, {}, 0, {}, 0, self.CAP, 10, 95)[0], 10)    # no load: reserve
        sunny = [(at(h), 1000 if 16 <= h <= 17 else 0) for h in range(24)]  # sun covers the house 3-5 PM
        self.assertAlmostEqual(evening_target(95, at(15), sunny, {}, 0.5, {}, 500, self.CAP, 10, 95)[1], 38.5)

    def test_decide_uses_simulated_level(self):
        d = decide(at(6), 30, CFG, 5000, projected=20)         # sun would add, but the house uses more
        self.assertEqual(d["projected_soc"], 20); self.assertEqual(d["kind"], "planned")


    def test_charging_says_when_it_will_be_done(self):
        # 52% projected, stops at 62%: 10% of 30.72 kWh = 3072 Wh at 3600 W -> 51.2 min
        d = decide(at(14), 40, CFG, charging_by_us=True, projected=52)
        self.assertEqual(d["kind"], "charging")
        self.assertEqual(d["done_at"], at(14) + timedelta(minutes=51.2))
        self.assertIn("done ~2:51 PM", d["reason"])
        late = decide(at(14, 30), 10, CFG, charging_by_us=True, projected=12)   # can't finish before 3 PM
        self.assertEqual(late["done_at"], at(15))

class HoldBattery(unittest.TestCase):
    def test_rules(self):
        h = lambda period="off-peak", grid=True, waste=0.0, held=False: hold_battery(period, grid, waste, held)[0]
        self.assertTrue(h())                                   # off-peak: house on cheap grid, battery saved for 3 PM
        self.assertFalse(h("part-peak")); self.assertFalse(h("peak"))   # expensive hours: battery runs the house
        self.assertFalse(h(grid=False, held=True))             # outage: battery free for backup
        self.assertFalse(h(waste=0.5, held=True))              # holding would waste solar: use the battery to make room
        self.assertTrue(h(waste=0.1, held=True))               # small overflow: stay held (no flapping)
        self.assertFalse(h(waste=0.1, held=False))             # ...and stay released
        self.assertIn("solar", hold_battery("off-peak", True, 0.5, False)[1])


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

    def test_running_charge_pauses_when_sun_moves_start_later(self):
        # Grid charge began at 8:46 with solar unknown; at 9:00 the array is learned and 7 kWh of sun is due by 3 PM.
        d = decide(at(9), 15, CFG, 7000, charging_by_us=True)        # 60% needs 45%-22.8% of 30.72 kWh -> ~2.4 h + 30 m
        self.assertEqual((d["kind"], d["charge"]), ("planned", False))
        self.assertIn("paused", d["reason"])
        # Close to the new start time (within 20 min) it keeps going rather than flapping off and on.
        near = decide(d["start_at"] - timedelta(minutes=10), 15, CFG, 7000, charging_by_us=True)
        self.assertEqual((near["kind"], near["charge"]), ("charging", True))

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
