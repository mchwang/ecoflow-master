"""Server-level tests: which copy sends which commands, manual overrides, settings saves.

Runs in demo mode (no EcoFlow calls) against a scratch database; set_force_charge / set_dpu2
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
        server.cfg.update(server.DEFAULTS, mode="auto", margin_min=900)  # big margin: charge window open all morning
        for k in ("leader", "holder", "lastWould", "lastBal"):
            server.state.pop(k, None)
        server.state["status"] = None
        p1 = mock.patch.object(server, "set_force_charge", wraps=server.set_force_charge)
        p2 = mock.patch.object(server, "set_dpu2", wraps=server.set_dpu2)
        self.force, self.dpu2 = p1.start(), p2.start()
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

    def test_dry_run_never_sends(self):
        server.cfg["mode"] = "dry-run"
        server.tick(at(6))
        self.assertEqual(self.sent(), [])
        self.assertIn("[dry-run] would turn force charge ON", server.state["log"][0])

    def test_standby_never_sends(self):
        server.db.claim_lease("other", time.time(), server.LEASE_TTL, force=True)
        server.tick(at(6))
        self.assertEqual(self.sent(), [])
        self.assertFalse(server.state["control"]["leader"])

    def test_manual_off_stays_off_for_the_day(self):
        server.tick(at(6))
        server.manual_force(False, now=at(6, 5))
        server.tick(at(6, 6))
        self.assertEqual(self.sent(), [True, False])            # not switched back on
        self.assertEqual(server.state["decision"]["kind"], "manual")
        server.tick(at(6, day=7))                               # next morning: automation resumes
        self.assertEqual(self.sent(), [True, False, True])

    def test_own_charge_stopped_after_window_even_on_standby(self):
        server.tick(at(6))                                      # we start a charge
        server.db.claim_lease("other", time.time(), server.LEASE_TTL, force=True)
        server.tick(at(15, 1))                                  # standby now, window over
        self.assertEqual(self.sent(), [True, False])
        self.assertFalse(server.cfg["charging_by_us"])

    def test_balancing_pauses_dpu2_only_in_auto(self):
        server.cfg.update(margin_min=30, bal_mode="dry-run")    # no grid charge at 1 AM
        server.fetch()
        server.demo["batteries"][0]["soc"] = 95
        server.tick(at(1))
        self.assertEqual(self.dpu2.call_count, 0)
        server.cfg["bal_mode"] = "auto"
        server.tick(at(1, 1))
        self.dpu2.assert_called_once_with(False)
        self.assertTrue(server.cfg["bal_held"])


class Http(Base):
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


class PanelData(unittest.TestCase):
    def test_real_panel_shape(self):
        # Trimmed from a real SHP2 + 2 DPU quota: pd303_mc. prefixes, flat dotted keys, empty slot 3 with fullCap 0.
        raw = {
            "backupIncreInfo.backupBatPer": 12, "backupIncreInfo.backupFullCap": 30720,
            "pd303_mc.backupIncreInfo.Energy1Info.batteryPercentage": 13, "pd303_mc.backupIncreInfo.Energy1Info.devInfo.fullCap": 18432,
            "pd303_mc.backupIncreInfo.Energy2Info.batteryPercentage": 10, "pd303_mc.backupIncreInfo.Energy2Info.devInfo.fullCap": 12288,
            "pd303_mc.backupIncreInfo.Energy3Info.batteryPercentage": 0, "pd303_mc.backupIncreInfo.Energy3Info.devInfo.fullCap": 0,
            "pd303_mc.backupIncreInfo.Energy3Info.isConnect": 1, "ch1EnableSet": 1, "ch2EnableSet": 1, "ch3EnableSet": 0,
            "wattInfo.gridWatt": 713.0, "wattInfo.allHallWatt": 713.0, "pd303_mc.masterIncreInfo.gridSta": 1,
            "foceChargeHight": 95, "loadInfo.hall1Watt": [231.6, 156.8],
            "pd303_mc.loadIncreInfo.hall1IncreInfo.ch1Info.chName": "Office/Bedroom 4",
        }
        s = server.summarize(raw)
        self.assertEqual([b["n"] for b in s["batteries"]], [1, 2])   # empty slot 3 skipped
        self.assertEqual((s["soc"], s["fullCapWh"], s["maxChargeSoc"], s["gridOnline"]), (12, 30720, 95, 1))
        self.assertEqual(s["circuits"][0]["name"], "Office/Bedroom 4")


if __name__ == "__main__":
    unittest.main()
