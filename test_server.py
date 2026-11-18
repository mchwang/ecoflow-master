"""Server-level tests: which copy sends which commands, manual overrides, settings saves.

Runs in demo mode (no EcoFlow calls) against a scratch database; set_force_charge / set_dpu
are wrapped so each test sees exactly which commands would reach the panel.
"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import server  # noqa: E402
from db import DB  # noqa: E402

TZ = server.TZ


def at(h, m=0, day=6):
    return datetime(2026, 11, day, h, m, tzinfo=TZ)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        server.DEMO, server.api, server.LEADER_URL, server.INSTANCE = True, None, "", "me"
        server.CONFIG_FILE, server.db, server.demo = tmp / "config.json", DB(tmp / "t.db"), None
        self.addCleanup(server.db.conn.close)
        server.cfg.clear()
        server.cfg.update(server.DEFAULTS, mode="auto", hold_mode="auto", margin_min=900)  # big margin: charge window open all morning
        for k in ("leader", "holder", "lastWould", "lastBal", "onBattery", "lastHold", "unplannedCharge", "solarReset", "forceSwitch", "offAt", "windSet"):
            server.state.pop(k, None)
        server.state["status"] = None
        server.state["log"].clear()                            # log lines from one test mustn't count in the next
        p1 = mock.patch.object(server, "set_force_charge", wraps=server.set_force_charge)
        p2 = mock.patch.object(server, "set_dpu", wraps=server.set_dpu)
        p3 = mock.patch.object(server, "set_reserve", wraps=server.set_reserve)
        self.force, self.dpu2, self.reserve = p1.start(), p2.start(), p3.start()
        self.addCleanup(mock.patch.stopall)

    def sent(self):
        return [c.args[1] for c in self.force.call_args_list]

    def panel(self):
        return [b["forceCharge"] for b in server.demo["batteries"]]


class Commands(Base):
    def test_auto_and_in_control_charges(self):
        server.tick(at(6))
        self.assertEqual(self.sent(), [True])
        self.assertEqual(self.panel(), ["FORCE_CHARGE_ON"] * 2)

    def test_target_lowered_to_evening_need(self):
        with mock.patch.object(server, "load_profile", return_value={h: 100 for h in range(24)}):
            server.tick(at(6))
        d = server.state["decision"]
        self.assertIn("evening's need", d["effective"]["target_why"])
        self.assertLess(d["effective"]["min_soc"], 60)

    def test_frozen_panel_data_pauses_automation(self):
        server.fetch()[0]["reportedAt"] = at(5, 35).timestamp()  # last upload 25 min before this tick
        with self.assertRaisesRegex(RuntimeError, "frozen.*5:35 AM.*25 min ago"):
            server.tick(at(6))
        self.assertEqual(self.sent(), [])                          # nothing sent on stale data
        self.assertEqual(server.db.readings_since(0), [])          # and nothing recorded
        server.fetch()[0]["reportedAt"] = at(5, 45).timestamp()  # 15 min ago: the panel's normal cadence since 2026-11-13
        server.tick(at(6))
        self.assertEqual(self.sent(), [True])

    def test_dry_run_never_sends(self):
        server.cfg["mode"] = "dry-run"
        server.tick(at(6))
        self.assertEqual(self.sent(), [])
        self.assertTrue(any("[dry-run] would turn force charge ON" in l for l in server.state["log"]))

    def test_standby_never_sends(self):
        server.db.claim_lease("other", time.time(), server.LEASE_TTL, force=True)
        server.tick(at(6))
        self.assertEqual(self.sent(), [])
        self.assertFalse(server.state["control"]["leader"])

    def test_manual_buttons_dont_take_control_away(self):
        server.tick(at(6))                                      # the plan grid-charges
        server.manual_force(False, now=at(6, 5))                # you press Force charge off
        self.assertEqual(self.sent(), [True, False])
        server.tick(at(6, 6))                                   # the plan still wants it: back on
        self.assertEqual(self.sent(), [True, False, True])
        self.assertNotEqual(server.state["decision"]["kind"], "manual")

    def test_own_charge_stopped_after_window_even_on_standby(self):
        server.tick(at(6))                                      # we start a charge
        server.db.claim_lease("other", time.time(), server.LEASE_TTL, force=True)
        server.tick(at(15, 1))                                  # standby now, window over
        self.assertEqual(self.sent(), [True, False])
        self.assertFalse(server.cfg["charging_by_us"])

    def test_logs_when_the_battery_takes_over_the_house(self):
        server.cfg["mode"] = "dry-run"
        s = server.fetch()[0]
        s.update(loadWatt=900, gridWatt=900, soc=34)            # house on grid
        server.tick(at(10))                                      # first reading after a start: nothing logged
        server.tick(at(10, 1))
        taken = lambda: [l for l in server.state["log"] if "took over" in l or "back on grid" in l]
        self.assertEqual(taken(), [])
        s.update(gridWatt=60)                                    # battery now supplies 840 W
        server.tick(at(10, 2))
        self.assertEqual(len(taken()), 1); self.assertIn("Battery took over the house", taken()[0]); self.assertIn("34%", taken()[0])
        s.update(gridWatt=800)                                   # 100 W from battery: in the gap, no change
        server.tick(at(10, 3))
        self.assertEqual(len(taken()), 1)
        s.update(gridWatt=900, soc=20)                           # all from grid again
        server.tick(at(21))
        self.assertEqual(len(taken()), 2); self.assertIn("House back on grid", taken()[0])

    def test_warns_when_the_panel_grid_charges_on_its_own(self):
        server.cfg["mode"] = "dry-run"                           # we never turn force charge on
        s = server.fetch()[0]
        warned = lambda: [l for l in server.state["log"] if "without our force charge" in l or "Unplanned grid charge ended" in l]
        s.update(loadWatt=700, solarWatt=1500, gridWatt=0, soc=52, solarReserveSoc=95)
        server.tick(at(13))
        self.assertEqual(warned(), [])                           # solar covers the house, no grid
        s.update(gridWatt=4000)                                  # 4 kW from grid, house needs none of it
        server.tick(at(13, 9)); server.tick(at(13, 10))
        self.assertEqual(len(warned()), 1); self.assertIn("4000 W", warned()[0]); self.assertIn("solar reserve 95%", warned()[0])
        s.update(gridWatt=300)                                   # tapering: still charging, no new line
        server.tick(at(14))
        self.assertEqual(len(warned()), 1)
        s.update(gridWatt=0, soc=96)
        server.tick(at(14, 18))
        self.assertEqual(len(warned()), 2); self.assertIn("started 1:09 PM", warned()[0])
        server.cfg["charging_by_us"] = True                      # our own charge: never a warning
        s.update(gridWatt=4000)
        server.tick(at(14, 20))
        self.assertEqual(len(warned()), 2)

    def reserves(self):
        return [c.args[1] for c in self.reserve.call_args_list]

    def test_holds_battery_off_peak_then_releases_at_3pm(self):
        soc = server.fetch()[0]["soc"]
        server.tick(at(6))
        self.assertEqual(self.reserves(), [round(soc)])         # held: reserve = current level
        server.tick(at(15, 1))
        self.assertEqual(self.reserves(), [round(soc), 20])     # part-peak: back to the outage floor
        self.assertIsNone(server.cfg["reserve_held"])

    def test_held_reserve_follows_the_battery_up(self):
        s = server.fetch()[0]; s["soc"] = 40
        server.tick(at(6))
        s["soc"] = 41; server.tick(at(6, 1))                    # +1%: no command
        s["soc"] = 43; server.tick(at(6, 2))                    # +3%: raise
        self.assertEqual(self.reserves(), [40, 43])

    def test_outage_and_handover_release_the_battery(self):
        server.tick(at(6))
        server.demo["gridOnline"] = 0
        server.tick(at(6, 1))
        self.assertEqual(self.reserves()[-1], 20)
        server.demo["gridOnline"] = 1
        server.tick(at(6, 2))                                   # held again
        server.db.claim_lease("other", time.time(), server.LEASE_TTL, force=True)
        server.tick(at(6, 3))                                   # another copy in control: never leave it held
        self.assertEqual(self.reserves()[-1], 20)

    def test_releases_when_holding_would_waste_solar(self):
        server.cfg.update(pv_kw=40.0, pv_pr=1.0, lat=1.0)       # a huge array: holding would overflow
        sunny = {"hourly": [(at(h), 900, 0) for h in range(7, 18)], "sunrise": "06:38", "sunset": "17:04"}
        with mock.patch.object(server, "get_weather", return_value=sunny):
            server.tick(at(8))
        self.assertEqual(self.reserves(), [])                   # never held
        self.assertIn("solar", server.state["decision"]["battery"]["why"])

    def test_hold_mode_dry_run_logs_but_still_grid_charges(self):
        server.cfg["hold_mode"] = "dry-run"                     # the default until holding is safe on the panel
        server.tick(at(6))
        self.assertEqual(self.reserves(), [])
        self.assertEqual(self.sent(), [True])                   # grid charging stays automatic
        self.assertTrue(any("would hold the battery" in l for l in server.state["log"]))

    def test_dry_run_never_holds(self):
        server.cfg["mode"] = "dry-run"
        server.tick(at(6))
        self.assertEqual(self.reserves(), [])
        self.assertTrue(any("would hold the battery" in l for l in server.state["log"]))

    def test_force_charge_switches_at_most_every_15_minutes(self):  # 2026-11-16: on/off every ~30 min, 5 min on
        s = server.fetch()[0]
        server.tick(at(6))                                      # short: charge
        s.update(soc=90)                                        # 8.7 kW filled the 2% buffer in 5 min
        server.tick(at(6, 5))
        self.assertEqual(self.sent(), [True])                   # kept on
        self.assertTrue(any("Keeping force charge ON until 6:15 AM" in l for l in server.state["log"]))
        server.db.claim_lease("other", time.time(), server.LEASE_TTL, force=True)
        server.tick(at(6, 6))                                   # on standby: the usual "would", not "Keeping"
        self.assertIn("[standby] would turn force charge OFF", " ".join(server.state["log"]))
        self.assertEqual(sum("Keeping force charge" in l for l in server.state["log"]), 1)
        server.db.claim_lease("me", time.time(), server.LEASE_TTL, force=True)
        server.tick(at(6, 15))
        self.assertEqual(self.sent(), [True, False])
        s.update(soc=10)                                        # short again right away
        server.tick(at(6, 20))
        self.assertEqual(self.sent(), [True, False])            # kept off
        server.tick(at(6, 30))
        self.assertEqual(self.sent(), [True, False, True])
        s.update(soc=90)
        server.tick(at(15, 1))                                  # window over: off at once, no 15 min wait
        self.assertEqual(self.sent(), [True, False, True, False])

    def test_off_resent_while_the_panel_keeps_force_charging(self):  # 2026-11-15/16: OFF ignored, charged into peak
        s = server.fetch()[0]
        server.tick(at(14, 50))                                 # we charge
        server.tick(at(15, 1))                                  # window over: OFF
        def report(h, m):                                       # the panel's upload at h:m still shows DPU 1 on
            s["batteries"][0]["forceCharge"] = "FORCE_CHARGE_ON"
            s["reportedAt"] = at(h, m).timestamp()
        report(14, 55)                                          # an upload from before our OFF: not ignored yet
        server.tick(at(15, 7))
        self.assertEqual(self.sent(), [True, False])
        report(15, 6)                                           # made after our OFF: the panel ignored it
        server.tick(at(15, 7))
        self.assertEqual(self.sent(), [True, False, False])
        self.assertTrue(any("still force charging DPU 1" in l for l in server.state["log"]))
        report(15, 9)
        server.tick(at(15, 10))
        self.assertEqual(len(self.sent()), 3)                   # at most every 5 min
        server.tick(at(15, 12))
        self.assertEqual(len(self.sent()), 4)
        self.assertEqual(self.panel(), ["FORCE_CHARGE_OFF"] * 2)
        server.cfg["mode"] = "dry-run"                          # only the copy in control (in Auto) sends
        report(15, 19)
        server.tick(at(15, 20))
        self.assertEqual(len(self.sent()), 4)

    def test_grid_outage_stops_and_blocks_grid_charging(self):
        server.tick(at(6))                                      # charging from the grid
        server.demo["gridOnline"] = 0                           # outage: battery runs the house
        server.tick(at(6, 1))
        self.assertEqual(self.sent(), [True, False])
        self.assertEqual(server.state["decision"]["kind"], "outage")
        server.tick(at(6, 2))
        self.assertEqual(self.sent(), [True, False])            # nothing more while it's down
        server.demo["gridOnline"] = 1
        server.tick(at(6, 3))
        self.assertEqual(self.sent(), [True, False, True])      # back to the plan when the grid returns

    def test_balancing_ignores_a_partly_learned_dpu1_profile(self):
        s = server.fetch()[0]
        hourly = [(at(h), 500 if 8 <= h <= 17 else 0) for h in range(1, 24)]   # sunny until 5 PM
        partial = server.run_balance(s, server.effective(s), at(8, 30), hourly, True, {8: 0.5})   # only 8 AM learned
        self.assertNotIn("little sun", partial["why"])
        full = {h: 0.0001 for h in range(7, 17)}                                 # every daylight hour learned, tiny
        self.assertIn("little sun", server.run_balance(s, server.effective(s), at(8, 30), hourly, True, full)["why"])

    def test_day_battery_change_uses_wh_and_skips_downtime(self):
        r = lambda m, soc, wh=None, h=9: server.db.add_reading(at(h, m, day=4).timestamp(), soc, 0, 0, 0, 0, 0, remain_wh=wh)
        r(0, 10); r(1, 10, 3000); r(2, 11, 3100)     # before the upgrade no Wh: % used; then Wh: +0.1 kWh
        r(2, 7, 2000, h=11)                            # 2 h later (app was down): that drop isn't counted
        self.assertAlmostEqual(server.day_data(at(9, day=4).date(), at(12), 30720)["report"]["stored_kwh"], 0.1)

    def test_measured_solar_per_hour_for_the_forecast_chart(self):
        r = lambda h, m, w: server.db.add_reading(at(h, m).timestamp(), 50, w, 0, 0, 0, 0)
        for m in range(0, 60, 5):
            r(9, m, 2000)                                      # 9 AM: 2 kW for the whole hour
        for m in (0, 5, 10):
            r(10, m, 1000)
        r(10, 15, 0)                                           # 10 AM: 1 kW for the 15 minutes with readings
        r(8, 0, 500)                                           # 8 AM: one reading, nothing after it: no data, not 0
        self.assertEqual(server.today_solar_kw(at(10, 30)), {9: 2.0, 10: 1.0})
        self.assertNotIn(10, server.today_solar_kw(at(10, 2)))  # 2 minutes in: too early to show

    def test_forecast_chart_runs_until_sunset(self):
        server.cfg.update(lat=1.0)
        wx = {"hourly": [(at(h), 300 if 9 <= h <= 16 else 0, 0) for h in range(1, 24)], "sunrise": "06:38", "sunset": "17:04"}
        with mock.patch.object(server, "get_weather", return_value=wx):
            server.tick(at(12))
        hours = [f["hour"] for f in server.state["decision"]["forecast"]]
        self.assertEqual(hours, [f"{h:02}" for h in range(6, 18)])  # 6 AM (sunrise 6:38) to 5 PM (sunset 5:04), zero-sun hours too

    def test_chart_profile_ignores_todays_readings(self):
        for day, w in ((5, 300), (6, 900)):                    # yesterday 300 W per DPU at 500 W/m2, today 900 W
            for m in range(25):
                server.db.add_reading(at(10, m, day=day).timestamp(), 50, 2 * w, 0, 0, 500, 0, w, w, 50, 50)
        midnight = at(0).timestamp()
        self.assertAlmostEqual(server.dpu_profiles(at(12), 95, midnight)[0][10], 0.6)   # yesterday only: 300/500
        self.assertAlmostEqual(server.dpu_profiles(at(12), 95)[0][10], 1.2)             # planner: both days

    def test_panel_read_again_soon_after_a_command(self):
        class Stop(BaseException):
            pass
        calls = []

        def fake_tick():
            calls.append(time.monotonic())
            if len(calls) == 1:
                server.set_force_charge(server.fetch()[0], True)   # a command in the first poll...
            else:
                raise Stop

        with mock.patch.object(server, "tick", fake_tick), mock.patch.object(server, "REREAD_S", 0.05):
            with self.assertRaises(Stop):
                server.loop()
        self.assertLess(calls[1] - calls[0], 5)                    # ...reads again in seconds, not POLL_S (60)

    def test_frozen_panel_warning_is_shown_plainly(self):
        class Stop(BaseException):
            pass
        stale = server.PanelStale("Panel data is frozen: last reported at 4:12 PM")
        with mock.patch.object(server, "tick", side_effect=stale), \
             mock.patch.object(server.wake, "wait", side_effect=Stop):
            with self.assertRaises(Stop):
                server.loop()
        self.assertEqual(server.state["error"], "Panel data is frozen: last reported at 4:12 PM")   # no "RuntimeError:" prefix

    def test_failed_polls_are_logged_once_per_stretch(self):
        class Stop(BaseException):
            pass
        stale = server.PanelStale("Panel data is frozen: last reported at 4:12 PM")
        with mock.patch.object(server, "tick", side_effect=[stale, stale, RuntimeError("EcoFlow API error 1"), None]), \
             mock.patch.object(server.wake, "wait", side_effect=[None] * 3 + [Stop]), \
             mock.patch.object(server, "log") as log:
            with self.assertRaises(Stop):
                server.loop()
        msgs = [c.args[0] for c in log.call_args_list]
        self.assertEqual(len(msgs), 2)                     # one line when it starts, one when it recovers; not one per minute
        self.assertRegex(msgs[0], r"^Poll failed \(readings may have a gap until it recovers\): Panel data is frozen")
        self.assertRegex(msgs[1], r"^Polling recovered: 3 polls failed since \d+:\d\d [AP]M "
                                  r"\(panel not reporting ×2, RuntimeError ×1\)")

    def test_savings_stored_per_day_and_totalled(self):
        for d in (4, 5):                                   # 1 kW house on solar for 5 min, off-peak, two days
            for m in (0, 5):
                server.db.add_reading(at(9, m, day=d).timestamp(), 50, 1000, 1000, 0, 0, 0)
        server.db.add_reading(at(9, day=6).timestamp(), 50, 0, 0, 0, 0, 0)   # today: nothing used yet
        with mock.patch.object(server, "day_data", wraps=server.day_data) as dd:
            sv = server.refresh_savings(at(12), 30720)
            first = dd.call_count
            server.refresh_savings(at(12, 1), 30720)
            self.assertEqual(dd.call_count - first, 4)     # past days with readings aren't recomputed (only days 1-3 and 6 are)
        kwh = 1000 * 300 / 3.6e6                           # 5 min at 1 kW
        day = round(kwh * 0.22558, 3)                      # EV2-A winter off-peak, as stored (3 decimals)
        self.assertEqual([(d, v, round(k, 3)) for d, v, k, *_ in server.db.daily_savings()],
                         [("2026-11-04", day, round(kwh, 3)), ("2026-11-05", day, round(kwh, 3)), ("2026-11-06", 0.0, 0.0)])
        self.assertEqual((sv["today"], sv["total"], sv["allTime"], sv["days"]), (0.0, round(2 * day, 2), round(2 * day, 2), 3))
        self.assertEqual((sv["monthWithout"], sv["monthGrid"]), (round(2 * day, 2), 0.0))

    def test_records_solar_and_level_per_dpu(self):
        server.fetch()
        server.demo["batteries"][0].update(solarWatt=420, soc=55)
        server.demo["batteries"][1].update(solarWatt=1300, soc=40)
        server.tick(at(10))
        self.assertEqual(server.db.readings_since(0)[-1][5:], (420, 1300))
        self.assertEqual(server.db.conn.execute("SELECT soc1, soc2 FROM readings").fetchone(), (55, 40))

    def test_balancing_pauses_dpu2_only_in_auto(self):
        server.cfg.update(margin_min=30, bal_mode="dry-run")    # no grid charge at 1 AM
        server.fetch()
        server.demo["batteries"][0]["soc"] = 95
        server.tick(at(1))
        self.assertEqual(self.dpu2.call_count, 0)
        server.cfg["bal_mode"] = "auto"
        server.tick(at(1, 1))
        self.dpu2.assert_called_once_with(2, False)
        self.assertEqual(server.cfg["bal_paused"], 2)

    def test_gusts_over_20_mph_raise_the_reserves_and_calm_sets_them_back(self):
        server.cfg.update(margin_min=30, hold_mode="dry-run", lat=1.0)
        server.fetch()
        windy = {"hourly": [], "sunrise": "06:38", "sunset": "17:04", "gusts": [(at(h), 21 if h == 20 else 10) for h in range(24)]}
        calm = {**windy, "gusts": [(at(h), 20) for h in range(24)]}  # 20 is not over the limit
        with mock.patch.object(server, "set_solar_reserve", wraps=server.set_solar_reserve) as sr:
            with mock.patch.object(server, "get_weather", return_value=windy):
                server.tick(at(1))
                server.tick(at(1, 1))                          # already raised: nothing resent
            self.assertEqual((server.demo["backupReserveSoc"], server.demo["solarReserveSoc"]), (50, 60))
            self.assertEqual((self.reserves(), sr.call_args_list), ([50], [mock.call(60)]))
            self.assertEqual(server.state["decision"]["battery"]["floor"], 60)
            self.assertIn("21 mph", server.state["decision"]["battery"]["wind"])
            self.assertTrue(server.cfg["wind_held"])
            with mock.patch.object(server, "get_weather", return_value=calm):
                server.tick(at(2))
                server.tick(at(2, 1))                          # set back once, not every minute
            self.assertEqual((server.demo["backupReserveSoc"], server.demo["solarReserveSoc"]), (20, 20))
            self.assertEqual(self.reserves(), [50, 20])
            self.assertFalse(server.cfg["wind_held"])

    def test_wind_counts_only_the_next_24_hours(self):  # the forecast also holds yesterday's gusts (past_days=1)
        w = {"gusts": [(at(11), 40), (at(13, day=7), 40)]}     # 1 h ago and 25 h ahead
        self.assertEqual(server.outage_floor(at(12), w), (20, None))
        self.assertEqual(server.outage_floor(at(12), {"gusts": [(at(13), 21)]})[0], 60)

    def test_clearing_the_location_sets_wind_reserves_back(self):
        server.cfg.update(margin_min=30, hold_mode="dry-run", lat=1.0)
        server.fetch()
        with mock.patch.object(server, "get_weather", return_value={"hourly": [], "sunrise": "06:38", "sunset": "17:04", "gusts": [(at(5), 40)]}):
            server.tick(at(1))
        server.cfg.update(lat=0.0, lon=0.0)
        server.tick(at(2))                                     # no location: no forecast, and the rule is off
        self.assertEqual((server.demo["backupReserveSoc"], server.demo["solarReserveSoc"]), (20, 20))

    def test_wind_rule_waits_for_control(self):
        server.cfg.update(margin_min=30, hold_mode="dry-run", lat=1.0)
        server.db.claim_lease("other", time.time(), server.LEASE_TTL, force=True)  # another copy is in control
        windy = {"hourly": [], "sunrise": "06:38", "sunset": "17:04", "gusts": [(at(5), 40)]}
        with mock.patch.object(server, "get_weather", return_value=windy):
            server.tick(at(1))
        self.assertEqual(self.reserves(), [])
        self.assertFalse(server.cfg["wind_held"])

    def test_wind_reserves_set_back_after_losing_control(self):  # review: they stayed at 60% for good
        server.cfg.update(margin_min=30, hold_mode="dry-run", lat=1.0)
        server.fetch()
        windy = {"hourly": [], "sunrise": "06:38", "sunset": "17:04", "gusts": [(at(5), 40)]}
        with mock.patch.object(server, "get_weather", return_value=windy):
            server.tick(at(1))
        server.db.claim_lease("other", time.time(), server.LEASE_TTL, force=True)  # another copy takes control
        with mock.patch.object(server, "get_weather", return_value=None):          # restart, no forecast yet
            server.tick(at(2))
        self.assertEqual((server.demo["backupReserveSoc"], server.demo["solarReserveSoc"]), (50, 60))  # kept
        with mock.patch.object(server, "get_weather", return_value={**windy, "gusts": [(at(5), 5)]}):
            server.tick(at(3))
        self.assertEqual((server.demo["backupReserveSoc"], server.demo["solarReserveSoc"]), (20, 20))
        self.assertFalse(server.cfg["wind_held"])

    def test_grid_charge_to_the_wind_reserve_is_not_a_warning(self):
        server.cfg.update(wind_held=True)
        s = {"gridWatt": 4000, "loadWatt": 500, "solarWatt": 0, "gridOnline": 1, "soc": 55, "solarReserveSoc": 60}
        server.note_grid_charge(s, at(1), False, True, 60)
        self.assertFalse(any("Warning" in l for l in server.state["log"]))
        server.note_grid_charge({**s, "soc": 61}, at(1, 30), False, True, 60)  # above it: a real unplanned charge
        self.assertTrue(any("Warning" in l for l in server.state["log"]))

    def test_unplanned_grid_charge_resets_the_panels_solar_reserve(self):
        server.cfg.update(margin_min=30)                       # no planned grid charge at 1 AM
        server.fetch()
        server.demo.update(gridWatt=4000, loadWatt=500, solarWatt=0, solarReserveSoc=95)  # panel charging toward 95%
        with mock.patch.object(server, "set_solar_reserve", wraps=server.set_solar_reserve) as sr:
            server.tick(at(1))
            sr.assert_called_once_with(20)                     # back to the outage reserve
            self.assertEqual(server.demo["solarReserveSoc"], 20)
            self.assertTrue(any("solar reserve 95% -> 20%" in l for l in server.state["log"]))
            server.demo["solarReserveSoc"] = 95                # panel raises it again
            server.tick(at(1, 5))
            self.assertEqual(sr.call_count, 1)                 # at most once per 15 min (the panel's report cadence)
            server.tick(at(1, 16))
            self.assertEqual(sr.call_count, 2)
            server.cfg["mode"] = "dry-run"
            server.demo["solarReserveSoc"] = 95
            server.tick(at(1, 40))
            self.assertEqual(sr.call_count, 2)                 # only the copy in control on Auto sends

    def test_balancing_keeps_dpus_within_ten_percent(self):
        server.cfg.update(margin_min=30, bal_mode="auto")       # no grid charge at 1 AM
        server.fetch()
        b1, b2 = server.demo["batteries"]
        b1["soc"], b2["soc"] = 50, 60                          # DPU 2 10% fuller: pause DPU 1
        server.tick(at(1))
        self.assertEqual([c.args for c in self.dpu2.call_args_list], [(1, False)])
        self.assertEqual(server.cfg["bal_paused"], 1)
        b1["soc"], b2["soc"] = 50, 49                          # within 3%: resume DPU 1
        server.tick(at(1, 1))
        self.assertEqual([c.args for c in self.dpu2.call_args_list], [(1, False), (1, True)])
        self.assertEqual(server.cfg["bal_paused"], 0)
        server.cfg["bal_paused"] = 1                           # paused DPU 1, then DPU 1 ends up 10% fuller
        b1["soc"], b2["soc"] = 70, 60
        server.tick(at(1, 2))                                  # resume DPU 1 before pausing DPU 2, never both off
        self.assertEqual([c.args for c in self.dpu2.call_args_list][2:], [(1, True), (2, False)])
        self.assertEqual(server.cfg["bal_paused"], 2)


class HttpServer(Base):  # starts the real handler; no tests of its own
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def post(self, path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.httpd.server_port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)



class Http(HttpServer):
    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.httpd.server_port}{path}") as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_day_report_for_a_chosen_date(self):
        for m in (0, 1, 2):  # 9:00-9:02 AM: 1 kW house, 600 W solar, 400 W grid
            server.db.add_reading(at(9, m, day=5).timestamp(), 50, 600, 1000, 400, 300, 0)
        code, r = self.get("/api/day?date=2026-11-05")
        self.assertEqual((code, r["date"], r["readings"], r["first"]), (200, "2026-11-05", 3, "2026-11-05"))
        self.assertAlmostEqual(r["report"]["solar_home"], 1.2 / 60, places=3)    # 2 min x 600 W
        self.assertAlmostEqual(r["report"]["grid_kwh"]["off-peak"], 0.8 / 60, places=3)
        self.assertEqual(len(r["hourly"]), 24)                                     # a past day shows all hours
        self.assertEqual(self.get("/api/day?date=2026-13-40")[0], 400)

    def test_state_with_times_in_it_still_loads(self):  # 2026-11-15: a grid-charge start time made /api/state a 502
        server.state["unplannedCharge"] = at(15, 0)
        code, r = self.get("/api/state")
        self.assertEqual((code, r["unplannedCharge"]), (200, at(15, 0).isoformat()))
        server.state.pop("unplannedCharge")
        self.assertEqual(self.get("/api/day?date=2026-11-04")[1]["readings"], 0)

    def test_rejected_save_changes_nothing(self):
        code, r = self.post("/api/config", {"min_soc": 70, "bal_full_soc": 50})  # resume 75 >= pause 50
        self.assertEqual(code, 400)
        self.assertEqual((server.cfg["min_soc"], server.cfg["bal_full_soc"]), (60, 90))

    def test_save_succeeds_even_if_panel_read_fails(self):
        with mock.patch.object(server, "fetch", side_effect=RuntimeError("EcoFlow down")):
            code, r = self.post("/api/config", {"min_soc": 65})
        self.assertEqual((code, r), (200, {"ok": True}))
        self.assertEqual(json.loads(server.CONFIG_FILE.read_text())["min_soc"], 65)
        self.assertIn("EcoFlow down", server.state["error"])


class SignIn(HttpServer):
    def setUp(self):
        super().setUp()
        server.USER, server.PASSWORD = "mch", "pw"
        self.addCleanup(setattr, server, "PASSWORD", "")

    def req(self, method, path, body=None, headers=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        out = r.status, dict(r.getheaders()), r.read().decode()
        c.close()
        return out

    def login(self, pw, headers=None):
        return self.req("POST", "/login", f"user=mch&password={pw}",
                        {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})

    def test_sign_in_page_instead_of_popup(self):
        code, h, body = self.req("GET", "/")
        self.assertEqual(code, 200); self.assertIn("Sign in", body); self.assertNotIn("WWW-Authenticate", h)
        self.assertEqual(self.req("GET", "/api/state")[0], 401)

    def test_wrong_password_no_cookie(self):
        code, h, _ = self.login("nope")
        self.assertEqual(code, 401); self.assertNotIn("Set-Cookie", h)

    def test_cookie_keeps_you_signed_in(self):
        code, h, _ = self.login("pw", {"X-Forwarded-Proto": "https"})
        self.assertEqual(code, 303)
        cookie = h["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Lax", "Secure", "Max-Age=31536000"):
            self.assertIn(flag, cookie)
        session = cookie.split(";")[0]
        self.assertEqual(self.req("GET", "/api/state", headers={"Cookie": session})[0], 200)
        tampered = session[:-1] + ("1" if session[-1] == "0" else "0")  # always differs from the real signature
        self.assertEqual(self.req("GET", "/api/state", headers={"Cookie": tampered})[0], 401)

    def test_expired_or_old_password_session_rejected(self):
        old = "hp_session=" + server.make_session(now=time.time() - 400 * 86400)
        self.assertFalse(server.valid_session(old))
        current = "hp_session=" + server.make_session()
        server.PASSWORD = "changed"
        self.assertFalse(server.valid_session(current))  # changing the password signs everyone out


class PanelData(unittest.TestCase):
    def test_real_panel_shape(self):
        # Trimmed from a real SHP2 + 2 DPU quota: pd303_mc. prefixes, flat dotted keys, empty slot 3 with fullCap 0.
        raw = {
            "backupIncreInfo.backupBatPer": 12, "backupIncreInfo.backupFullCap": 30720,
            "pd303_mc.backupIncreInfo.Energy1Info.batteryPercentage": 13, "pd303_mc.backupIncreInfo.Energy1Info.devInfo.fullCap": 18432,
            "pd303_mc.backupIncreInfo.Energy2Info.batteryPercentage": 10, "pd303_mc.backupIncreInfo.Energy2Info.devInfo.fullCap": 12288,
            "pd303_mc.backupIncreInfo.Energy3Info.batteryPercentage": 0, "pd303_mc.backupIncreInfo.Energy3Info.devInfo.fullCap": 0,
            "pd303_mc.backupIncreInfo.Energy3Info.isConnect": 1, "ch1EnableSet": 1, "ch2EnableSet": 1, "ch3EnableSet": 0,
            # solar = high-voltage + low-voltage PV input; pvChargeWatts is not watts (reads 3 at any output)
            "pd303_mc.backupIncreInfo.Energy1Info.pvChargeWatts": 3, "pd303_mc.backupIncreInfo.Energy1Info.pvHeightChargeWatts": 117,
            "pd303_mc.backupIncreInfo.Energy1Info.pvLowChargeWatts": 0,
            "pd303_mc.backupIncreInfo.Energy2Info.pvChargeWatts": 3, "pd303_mc.backupIncreInfo.Energy2Info.pvHeightChargeWatts": 423,
            "pd303_mc.backupIncreInfo.Energy2Info.pvLowChargeWatts": 34,
            "wattInfo.gridWatt": 713.0, "wattInfo.allHallWatt": 713.0, "pd303_mc.masterIncreInfo.gridSta": 1,
            "foceChargeHight": 95, "loadInfo.hall1Watt": [231.6, 156.8],
            "pd303_mc.loadIncreInfo.hall1IncreInfo.ch1Info.chName": "Office/Bedroom 4",
        }
        s = server.summarize(raw)
        self.assertEqual([b["n"] for b in s["batteries"]], [1, 2])   # empty slot 3 skipped
        self.assertEqual((s["soc"], s["fullCapWh"], s["maxChargeSoc"], s["gridOnline"]), (12, 30720, 95, 1))
        self.assertEqual(s["circuits"][0]["name"], "Office/Bedroom 4")
        self.assertEqual(([b["solarWatt"] for b in s["batteries"]], s["solarWatt"]), ([117, 457], 574))


if __name__ == "__main__":
    unittest.main()
