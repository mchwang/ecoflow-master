"""EcoFlow Smart Home Panel 2 dashboard + PG&E EV2-A grid-charge automation.

Run:  python3 server.py   then open http://127.0.0.1:8787
Without ECOFLOW_ACCESS_KEY in .env it runs in demo mode with demo_quota.json.
Data (SQLite history + settings) lives in DATA_DIR (default ./data; /data in Docker).
Set DASHBOARD_PASSWORD (and optionally DASHBOARD_USER) to require a login; required unless HOST is 127.0.0.1.
"""
import base64
import hmac
import json
import os
import threading
import time
import traceback
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from db import DB
from ecoflow_api import SSL_CTX, EcoFlow
from planner import deadline_of, decide, fit_array_kw, solar_wh, tou_period

HERE = Path(__file__).parent
TZ = ZoneInfo("America/Los_Angeles")
HISTORY_DAYS = 14
MIN_SAMPLES = 60  # ~1 hour of daylight polls before trusting the learned array size
POLL_S = 60
DPU_WH = 6144  # one DPU battery pack; fallback only if the panel omits backupFullCap

for line in (HERE / ".env").read_text().splitlines() if (HERE / ".env").exists() else []:
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
db = DB(DATA_DIR / "homepower.db")
HOST = os.environ.get("HOST", "127.0.0.1")
USER = os.environ.get("DASHBOARD_USER", "admin")
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

DEFAULTS = {
    "mode": "dry-run",           # off | dry-run | auto
    "offpeak_start": "00:00",
    "offpeak_end": "15:00",      # EV2-A part-peak starts 3pm: deadline for min_soc
    "min_soc": 60,               # battery % wanted by the deadline (solar + grid)
    "lat": 0.0, "lon": 0.0,      # location for the weather forecast; 0,0 = no forecast
    "pv_kw": 0.0,                # fallback until history is learned: array kW DC; 0 = ignore solar
    "pv_pr": 0.8,                # fallback performance ratio (losses, tilt, shading)
    "capacity_wh": 0,            # 0 = panel-reported backupFullCap
    "charge_w": 0,               # 0 = use the panel's chargeWattPower setting
    "margin_min": 30,
    "charging_by_us": False,     # persisted so a restart can still turn off what we turned on
}
NUMERIC = {"min_soc", "lat", "lon", "pv_kw", "pv_pr", "capacity_wh", "charge_w", "margin_min"}

_saved = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
cfg = {k: _saved.get(k, v) for k, v in DEFAULTS.items()}  # drops settings from older versions
lock = threading.Lock()
state = {"status": None, "decision": None, "error": None, "updated": None,
         "log": deque((f"{datetime.fromtimestamp(t, TZ):%m-%d %H:%M} {m}" for t, m in db.recent_events()), maxlen=50)}
weather = {"at": 0, "key": None, "data": None, "error": None}


def learned_kw(now, max_soc):
    return fit_array_kw(db.solar_samples(now.timestamp() - HISTORY_DAYS * 86400), max_soc)

AK, SK, SN = (os.environ.get(k, "") for k in ("ECOFLOW_ACCESS_KEY", "ECOFLOW_SECRET_KEY", "ECOFLOW_SN"))
DEMO = not (AK and SK)
api = None if DEMO else EcoFlow(AK, SK, os.environ.get("ECOFLOW_HOST", "https://api.ecoflow.com"))


def find_panel():
    """No ECOFLOW_SN set: use the account's Smart Home Panel 2 (serials start with HD31)."""
    devs = api.devices() or []
    panels = [d["sn"] for d in devs if d.get("sn", "").startswith("HD31")]
    if len(panels) != 1:
        raise RuntimeError(f"Found {len(panels)} Smart Home Panel 2 units in {[d.get('sn') for d in devs]}; set ECOFLOW_SN in .env")
    return panels[0]


def save_cfg():
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def log(msg):
    now = datetime.now(TZ)
    db.add_event(now.timestamp(), msg)
    state["log"].appendleft(f"{now:%m-%d %H:%M} {msg}")


def q(data, key, default=None):
    """Read a quota value; the API may return flat dotted keys, nested dicts, and/or a pd303_mc. prefix."""
    for k in (key, "pd303_mc." + key, key.removeprefix("pd303_mc.")):
        if k in data:
            return data[k]
        node = data
        for part in k.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node is not None:
            return node
    return default


def summarize(d):
    bats = []
    for n in (1, 2, 3):
        soc = q(d, f"backupIncreInfo.Energy{n}Info.batteryPercentage")
        if soc is None or q(d, f"backupIncreInfo.Energy{n}Info.devInfo.fullCap") == 0:
            continue  # empty battery slot (the panel still reports isConnect=1 for it)
        watts = q(d, "wattInfo.chWatt", [])
        bats.append({"n": n, "soc": soc, "watt": watts[n - 1] if len(watts) >= n else None,
                     "fullCapWh": q(d, f"backupIncreInfo.Energy{n}Info.devInfo.fullCap"),
                     "temp": q(d, f"backupIncreInfo.Energy{n}Info.emsBatTemp"),
                     "solarWatt": q(d, f"backupIncreInfo.Energy{n}Info.pvChargeWatts"),
                     "ready": q(d, f"backupIncreInfo.ch{n}Info.backupIsReady"),
                     "enabled": q(d, f"ch{n}EnableSet"), "forceCharge": q(d, f"ch{n}ForceCharge")})
    hall = q(d, "loadInfo.hall1Watt", []) or []
    circuits = []
    for x in range(1, 13):
        name = q(d, f"loadIncreInfo.hall1IncreInfo.ch{x}Info.chName")
        if name is None and x > len(hall):
            continue
        circuits.append({"ch": x, "name": name or f"Circuit {x}",
                         "watt": hall[x - 1] if x <= len(hall) else None,
                         "on": q(d, f"loadIncreInfo.hall1IncreInfo.ch{x}Sta.loadSta")})
    return {
        "gridOnline": q(d, "masterIncreInfo.gridSta"), "gridVolt": q(d, "masterIncreInfo.gridVol"),
        "gridWatt": q(d, "wattInfo.gridWatt"), "loadWatt": q(d, "wattInfo.allHallWatt"),
        "soc": q(d, "backupIncreInfo.backupBatPer"), "fullCapWh": q(d, "backupIncreInfo.backupFullCap"),
        "remainWh": q(d, "backupIncreInfo.backupDischargeRmainBatCap"), "chargeMin": q(d, "backupInfo.backupChargeTime"), "dischargeMin": q(d, "backupInfo.backupDischargeTime"),
        "backupReserveSoc": q(d, "backupReserveSoc"), "chargeWattPower": q(d, "chargeWattPower"),
        "maxChargeSoc": q(d, "foceChargeHight"), "epsMode": q(d, "epsModeInfo"),
        "stormGuard": q(d, "stormIsEnable"), "inStorm": q(d, "inStormMode"),
        "smartBackupMode": q(d, "smartBackupMode"), "batteries": bats, "circuits": circuits,
        "solarWatt": sum(b["solarWatt"] or 0 for b in bats),
    }


def effective(s):
    c = dict(cfg)
    c["capacity_wh"] = c["capacity_wh"] or s["fullCapWh"] or DPU_WH * max(1, len(s["batteries"]))
    c["charge_w"] = c["charge_w"] or s["chargeWattPower"] or 3000
    c["max_soc"] = s["maxChargeSoc"] or 100  # panel stops charging at its max level
    c["min_soc"] = min(c["min_soc"], c["max_soc"])
    return c


def set_force_charge(s, on):
    params = {f"ch{b['n']}ForceCharge": "FORCE_CHARGE_ON" if on else "FORCE_CHARGE_OFF" for b in s["batteries"] if b["enabled"] != 0}
    if DEMO:
        for b in demo["batteries"]:
            b["forceCharge"] = params[f"ch{b['n']}ForceCharge"]
    else:
        api.set_shp2(SN, params)
    log(f"Sent force charge {'ON' if on else 'OFF'}: {params}")


demo = None


def fetch():
    global demo
    if DEMO:
        if demo is None:
            demo = summarize(json.loads((HERE / "demo_quota.json").read_text()))
        return demo, {"demo": True}
    global SN
    SN = SN or find_panel()
    raw = api.quota_all(SN)
    return summarize(raw), raw


def get_weather():
    """Hourly irradiance + cloud cover from Open-Meteo (free, no key), cached 30 min."""
    key = (cfg["lat"], cfg["lon"])
    if key == (0, 0):
        return None
    if weather["key"] != key or time.time() - weather["at"] > 1800:
        url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": cfg["lat"], "longitude": cfg["lon"], "timezone": "America/Los_Angeles", "forecast_days": 2,
            "hourly": "shortwave_radiation,cloud_cover", "daily": "sunrise,sunset"})
        try:
            with urllib.request.urlopen(url, timeout=20, context=SSL_CTX) as r:
                d = json.load(r)
            h = d["hourly"]
            weather.update(at=time.time(), key=key, error=None, data={
                "hourly": [(datetime.fromisoformat(t).replace(tzinfo=TZ), ghi, cc)
                           for t, ghi, cc in zip(h["time"], h["shortwave_radiation"], h["cloud_cover"])],
                "sunrise": d["daily"]["sunrise"][0][11:], "sunset": d["daily"]["sunset"][0][11:]})
        except Exception as e:  # keep the last forecast; with none, solar counts as 0 (charges more, never less)
            weather.update(at=time.time(), error=f"Weather: {type(e).__name__}: {e}")
    return weather["data"]


def tick():
    s, raw = fetch()
    if s["soc"] is None:  # never act on missing data (a 0% default would trigger charging)
        raise RuntimeError("battery level (backupIncreInfo.backupBatPer) missing from panel data")
    now = datetime.now(TZ)
    w = get_weather()
    with lock:
        c = effective(s)
        deadline = deadline_of(now, c)
        hourly = [(t, ghi or 0) for t, ghi, _ in w["hourly"]] if w else []
        ghi_now = next((g for t, g in hourly if t - timedelta(hours=1) <= now < t), 0)
        db.add_reading(now.timestamp(), s["soc"], s["solarWatt"], s["loadWatt"], s["gridWatt"], ghi_now, cfg["charging_by_us"])
        fit_kw, fit_n = learned_kw(now, c["max_soc"])
        if fit_n >= MIN_SAMPLES:
            c["pv_kw"], c["pv_pr"], c["pv_source"] = round(fit_kw, 2), 1.0, f"learned from {fit_n} readings"
        else:
            c["pv_source"] = f"manual setting (learning: {fit_n}/{MIN_SAMPLES} readings)"
        pv_wh = solar_wh(now, deadline, hourly, c["pv_kw"], c["pv_pr"]) if c["pv_kw"] else 0.0
        d = decide(now, s["soc"], c, pv_wh, cfg["charging_by_us"])
        charge, reason, start_at = d["charge"], d["reason"], d["start_at"]
        mode = cfg["mode"]
        if mode != "off" and charge != cfg["charging_by_us"]:
            if mode == "auto":
                set_force_charge(s, charge)
                cfg["charging_by_us"] = charge
                save_cfg()
            else:
                log(f"[dry-run] would turn force charge {'ON' if charge else 'OFF'}: {reason}")
        period, season, price = tou_period(now)
        forecast = [{"hour": f"{t:%H}", "cloud": cc, "ghi": round(ghi or 0), "kw": round((ghi or 0) / 1000 * c["pv_kw"] * c["pv_pr"], 2)}
                    for t, ghi, cc in (w["hourly"] if w else []) if t.date() == now.date() and (ghi or 0) > 0]
        state.update(status=s, raw=raw, error=weather["error"], updated=now.isoformat(timespec="seconds"), decision={
            "charge": charge, "reason": reason, "startAt": start_at and start_at.strftime("%H:%M"),
            "projectedSoc": d["projected_soc"], "solarKwh": round(pv_wh / 1000, 1),
            "sunrise": w and w["sunrise"], "sunset": w and w["sunset"], "forecast": forecast,
            "period": period, "season": season, "price": price, "effective": c,
        })


def loop():
    while True:
        try:
            tick()
        except Exception as e:  # keep polling; show the error on the dashboard
            state["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        time.sleep(POLL_S)


def authorized(header):
    if not PASSWORD:
        return True  # only allowed when bound to localhost (checked at startup)
    try:
        user, _, pw = base64.b64decode(header.removeprefix("Basic ")).decode().partition(":")
    except Exception:
        return False
    return hmac.compare_digest(user, USER) & hmac.compare_digest(pw, PASSWORD)


class Handler(BaseHTTPRequestHandler):
    def _auth(self):
        if authorized(self.headers.get("Authorization", "")):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Home Power", charset="UTF-8"')
        self.end_headers()
        return False

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=list).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":  # unauthenticated, for Coolify's health check
            return self._json({"ok": True})
        if not self._auth():
            return
        if self.path == "/":
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._json({**state, "config": cfg, "demo": DEMO})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._auth():
            return
        # Browsers can't send application/json cross-site without a CORS preflight, so this blocks
        # other sites from riding on your saved login to flip grid charging (CSRF).
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._json({"error": "Content-Type must be application/json"}, 415)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api/config":
                with lock:
                    for k, v in body.items():
                        if k == "mode" and v in ("off", "dry-run", "auto"):
                            cfg[k] = v
                        elif k in ("offpeak_start", "offpeak_end"):
                            cfg[k] = datetime.strptime(v, "%H:%M").strftime("%H:%M")  # "8:00" -> "08:00"
                        elif k in NUMERIC:
                            v = float(v)
                            if (v < 0 and k not in ("lat", "lon")) or (k.endswith("soc") and v > 100):
                                raise ValueError(f"{k} out of range")
                            cfg[k] = v
                        else:
                            raise ValueError(f"unknown or invalid setting {k}")
                    save_cfg()
                    log(f"Settings changed: {body}")
            elif self.path == "/api/force":
                with lock:
                    set_force_charge(state["status"], bool(body.get("on")))
                    cfg["charging_by_us"] = False  # manual override: automation won't undo it
                    save_cfg()
            else:
                return self._json({"error": "not found"}, 404)
            tick()
            self._json({"ok": True})
        except Exception as e:
            self._json({"error": str(e)}, 400)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if HOST != "127.0.0.1" and not PASSWORD:
        raise SystemExit(f"Refusing to listen on {HOST} without DASHBOARD_PASSWORD: this app can switch grid charging.")
    port = int(os.environ.get("PORT", 8787))
    print(f"{'DEMO mode' if DEMO else 'Panel ' + (SN or 'auto-detect')} - http://{HOST}:{port} - data in {DATA_DIR}"
          + (" - login required" if PASSWORD else ""), flush=True)
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer((HOST, port), Handler).serve_forever()
