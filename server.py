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
import socket
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
from planner import ROUND_TRIP, afternoon_sun, balance, deadline_of, decide, target_soc, hm12, price_at, savings, fit_array_kw, solar_wh, tou_period

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


def stamp(dt):
    return f"{dt:%b} {dt.day}, {hm12(dt)}"  # "Nov 6, 4:20 AM"

USER = os.environ.get("DASHBOARD_USER", "admin")
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
# Control lease: only the copy holding it sends automatic commands. The referee copy (no LEADER_URL,
# normally the VPS) stores the lease; other copies ask it over HTTP. Latest start / "Take control" wins;
# if the holder stops renewing for LEASE_TTL s, a standby copy takes over.
INSTANCE = os.environ.get("INSTANCE_NAME") or socket.gethostname()
LEADER_URL = os.environ.get("LEADER_URL", "").rstrip("/")
LEADER_AUTH = "Basic " + base64.b64encode(
    f"{os.environ.get('LEADER_USER', 'admin')}:{os.environ.get('LEADER_PASSWORD', '')}".encode()).decode()
LEASE_TTL = 300

DEFAULTS = {
    "mode": "dry-run",           # off | dry-run | auto
    "offpeak_start": "00:00",
    "offpeak_end": "15:00",      # EV2-A part-peak starts 3pm: deadline for min_soc
    "min_soc": 60,               # battery % wanted by the deadline when the afternoon is sunny
    "min_soc_cloudy": 80,        # ...when little/no sun is forecast after the deadline (or no forecast)
    "sunny_after_wh": 300,       # forecast sunlight after the deadline (Wh/m2) that counts as "sunny"
    "lat": 0.0, "lon": 0.0,      # location for the weather forecast; 0,0 = no forecast
    "pv_kw": 0.0,                # fallback until history is learned: array kW DC; 0 = ignore solar
    "pv_pr": 0.8,                # fallback performance ratio (losses, tilt, shading)
    "capacity_wh": 0,            # 0 = panel-reported backupFullCap
    "charge_w": 0,               # 0 = use the panel's chargeWattPower setting
    "margin_min": 30,
    "charging_by_us": False,     # persisted so a restart can still turn off what we turned on
    # DPU balancing: pause DPU 2 while DPU 1 is nearly full, so DPU 1 drains and makes room for its afternoon sun.
    "bal_mode": "dry-run",       # off | dry-run | auto, separate from the charging mode until verified on the panel
    "bal_full_soc": 90,          # pause DPU 2 when DPU 1 reaches this %
    "bal_release_soc": 75,       # resume DPU 2 when DPU 1 drops to this %
    "bal_sun_left_wh": 100,      # resume when less forecast sunlight than this (Wh/m2) is left today
    "bal_held": False,           # we paused DPU 2 (persisted, so we only ever resume what we paused)
}
NUMERIC = {"bal_full_soc", "bal_release_soc", "bal_sun_left_wh", "min_soc", "min_soc_cloudy", "sunny_after_wh", "lat", "lon", "pv_kw", "pv_pr", "capacity_wh", "charge_w", "margin_min"}

_saved = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
cfg = {k: _saved.get(k, v) for k, v in DEFAULTS.items()}  # drops settings from older versions
lock = threading.Lock()
state = {"status": None, "decision": None, "error": None, "updated": None,
         "log": deque((f"{stamp(datetime.fromtimestamp(t, TZ))} {m}" for t, m in db.recent_events()), maxlen=50)}
weather = {"at": 0, "key": None, "data": None, "error": None}


def lease(force=False, peek=False):
    """Claim/renew control (or with peek, only look). Returns the holder's name, or None if
    nobody holds it / the referee is unreachable (= standby)."""
    if not LEADER_URL:
        return db.peek_lease(time.time(), LEASE_TTL) if peek else db.claim_lease(INSTANCE, time.time(), LEASE_TTL, force)
    req = urllib.request.Request(LEADER_URL + "/api/lease", method="POST",
                                 data=json.dumps({"holder": INSTANCE, "force": force, "peek": peek}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": LEADER_AUTH})
    try:
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
            return json.load(r)["holder"]
    except Exception as e:  # can't confirm control -> never act (fail safe)
        state["leaseError"] = f"Can't reach control referee {LEADER_URL}: {type(e).__name__}: {e}"
        return None


def update_control(s, force=False):
    # Only a copy in Auto competes for control; Off/Dry-run copies just look, so they never
    # take control away from the copy that is actually automating.
    auto = cfg["mode"] == "auto"
    state["leaseError"] = None
    holder = lease(force=force) if auto else lease(peek=True)
    leader = auto and holder == INSTANCE
    if not auto:
        state.update(leader=False, holder=holder)
        return False
    if leader and not state.get("leader"):
        # Adopt the panel's current force-charge state, so we can turn off a charge the previous controller started.
        cfg["charging_by_us"] = any(b["forceCharge"] == "FORCE_CHARGE_ON" for b in s["batteries"] if b["enabled"] != 0)
        save_cfg()
        log(f"{INSTANCE} is now in control")
    elif not leader and state.get("leader", True):
        log(f"{INSTANCE} on standby: {holder or 'control referee unreachable'} is in control")
    state.update(leader=leader, holder=holder)
    return leader


def month_savings(now, capacity_wh):
    """Savings today and this month, replayed from this month's readings (~45k rows max, fast enough per poll)."""
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = db.readings_since(month_start.timestamp())
    if not rows:
        return None
    first_soc = rows[0][1] or 0
    r = savings([(datetime.fromtimestamp(ts, TZ), load, grid, solar) for ts, _, load, grid, solar in rows],
                now.replace(hour=0, minute=0, second=0, microsecond=0),
                initial_kwh=first_soc / 100 * capacity_wh / 1000,
                initial_cost_per_kwh=price_at(month_start.replace(hour=1)) / ROUND_TRIP)  # unknown origin: assume off-peak grid
    db.save_daily_savings(r.pop("by_day"), now.timestamp())  # this month's days; earlier months stay as stored
    history = db.daily_savings()
    return {k: round(v, 2) for k, v in r.items()} | {
        "allTime": round(sum(d[1] for d in history), 2), "days": len(history), "since": history[0][0] if history else None}


def learned_kw(now, max_soc):
    return fit_array_kw(db.solar_samples(now.timestamp() - HISTORY_DAYS * 86400), max_soc)

# strip: hosting dashboards (e.g. Coolify) pass pasted quotes/whitespace through literally -> "accessKey is invalid"
AK, SK, SN = (os.environ.get(k, "").strip().strip("'\"").strip() for k in ("ECOFLOW_ACCESS_KEY", "ECOFLOW_SECRET_KEY", "ECOFLOW_SN"))
DEMO = not (AK and SK)
# api-a (Americas), not the global api.ecoflow.com: that routes by caller location, so a VPS in Europe
# lands on the EU server, which rejects US keys with 8513 "accessKey is invalid".
api = None if DEMO else EcoFlow(AK, SK, os.environ.get("ECOFLOW_HOST") or "https://api-a.ecoflow.com")


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
    state["log"].appendleft(f"{stamp(now)} {msg}")


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
    params = {f"ch{b['n']}ForceCharge": "FORCE_CHARGE_ON" if on else "FORCE_CHARGE_OFF" for b in s["batteries"]
              if b["enabled"] != 0 or (b["n"] == 2 and cfg["bal_held"])}  # include DPU 2 if only we paused it
    if DEMO:
        for b in demo["batteries"]:
            b["forceCharge"] = params[f"ch{b['n']}ForceCharge"]
    else:
        api.set_shp2(SN, params)
    log(f"Sent force charge {'ON' if on else 'OFF'}: {params}")


def set_dpu2(enabled):
    params = {"ch2EnableSet": 1 if enabled else 0}
    if DEMO:
        for b in demo["batteries"]:
            if b["n"] == 2:
                b["enabled"] = params["ch2EnableSet"]
    else:
        api.set_shp2(SN, params)
    log(f"Sent DPU 2 {'resume' if enabled else 'pause'}: {params}")


def run_balance(s, c, now, hourly, leader):
    bats = {b["n"]: b for b in s["batteries"]}
    if 1 not in bats or 2 not in bats:
        return None
    if not cfg["bal_held"] and bats[2]["enabled"] == 0:
        return {"hold": False, "held": False, "mode": cfg["bal_mode"], "why": "DPU 2 is disabled in the EcoFlow app; leaving it alone"}
    sun_left = solar_wh(now, now.replace(hour=23, minute=59), hourly, 1, 1) if hourly else None
    grid_charging = cfg["charging_by_us"] or any(b["forceCharge"] == "FORCE_CHARGE_ON" for b in bats.values())
    hold, why = balance(bats[1]["soc"], bats[2]["soc"], bool(s["gridOnline"]), grid_charging, sun_left, cfg["bal_held"], c)
    if hold != cfg["bal_held"] and cfg["bal_mode"] != "off":
        if cfg["bal_mode"] == "auto" and leader:
            set_dpu2(not hold)
            cfg["bal_held"] = hold
            save_cfg()
            log(why)
        else:
            msg = f"[{'standby' if cfg['bal_mode'] == 'auto' else 'dry-run'}] would {'pause' if hold else 'resume'} DPU 2"
            if state.get("lastBal") != msg:
                log(f"{msg}: {why}")
            state["lastBal"] = msg
    else:
        state["lastBal"] = None
    return {"hold": hold, "held": cfg["bal_held"], "mode": cfg["bal_mode"], "why": why}


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
    leader = update_control(s)
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
        c["sun_after_wh"] = afternoon_sun(now, c, hourly)
        c["min_soc"], c["target_why"] = target_soc(c, c["sun_after_wh"])
        pv_wh = solar_wh(now, deadline, hourly, c["pv_kw"], c["pv_pr"]) if c["pv_kw"] else 0.0
        d = decide(now, s["soc"], c, pv_wh, cfg["charging_by_us"])
        charge, reason, start_at = d["charge"], d["reason"], d["start_at"]
        mode = cfg["mode"]
        if mode != "off" and charge != cfg["charging_by_us"]:
            if mode == "auto" and leader:
                set_force_charge(s, charge)
                cfg["charging_by_us"] = charge
                save_cfg()
            else:
                msg = f"[{'standby' if mode == 'auto' else 'dry-run'}] would turn force charge {'ON' if charge else 'OFF'}"
                if state.get("lastWould") != msg:  # log once per change, not every minute
                    log(f"{msg}: {reason}")
                state["lastWould"] = msg
        else:
            state["lastWould"] = None
        bal = run_balance(s, c, now, hourly, leader)
        period, season, price = tou_period(now)
        saved = month_savings(now, c["capacity_wh"])
        # Open-Meteo values are the mean of the hour ending at t, so label each bar with the hour it covers.
        forecast = [{"hour": f"{t - timedelta(hours=1):%H}", "cloud": cc, "ghi": round(ghi or 0), "kw": round((ghi or 0) / 1000 * c["pv_kw"] * c["pv_pr"], 2)}
                    for t, ghi, cc in (w["hourly"] if w else []) if (t - timedelta(hours=1)).date() == now.date() and (ghi or 0) > 0]
        state.update(status=s, raw=raw, error=weather["error"], updated=now.isoformat(timespec="seconds"), decision={
            "charge": charge, "kind": d["kind"], "reason": reason, "startAt": start_at and start_at.strftime("%H:%M"),
            "projectedSoc": d["projected_soc"], "solarKwh": round(pv_wh / 1000, 1),
            "sunrise": w and w["sunrise"], "sunset": w and w["sunset"], "forecast": forecast,
            "period": period, "season": season, "price": price, "effective": c,
        }, savings=saved, balance=bal, control={"instance": INSTANCE, "holder": state.get("holder"), "leader": leader,
                    "referee": LEADER_URL or "this copy", "error": state.get("leaseError")})


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
        elif self.path == "/api/savings":  # daily history: [{day, saved, kwh}]
            self._json([{"day": d, "saved": round(v, 2), "kwh": round(k, 2)} for d, v, k in db.daily_savings()])
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
                    to_auto = body.get("mode") == "auto" and cfg["mode"] != "auto"
                    for k, v in body.items():
                        if k in ("mode", "bal_mode") and v in ("off", "dry-run", "auto"):
                            cfg[k] = v
                        elif k in ("offpeak_start", "offpeak_end"):
                            cfg[k] = datetime.strptime(v, "%H:%M").strftime("%H:%M")  # "8:00" -> "08:00"
                        elif k in NUMERIC:
                            v = float(v)
                            if (v < 0 and k not in ("lat", "lon")) or (k in ("min_soc", "min_soc_cloudy", "bal_full_soc", "bal_release_soc") and v > 100):
                                raise ValueError(f"{k} out of range")
                            cfg[k] = v
                        else:
                            raise ValueError(f"unknown or invalid setting {k}")
                    if cfg["bal_release_soc"] >= cfg["bal_full_soc"]:
                        raise ValueError("DPU 2 resume level must be below its pause level")
                    save_cfg()
                    log(f"Settings changed: {body}")
                    if to_auto and state["status"]:  # switching to Auto takes control (latest wins)
                        update_control(state["status"], force=True)
            elif self.path == "/api/lease":  # other copies ask this referee for control
                if LEADER_URL:
                    raise ValueError("this copy is not the control referee")
                holder = str(body.get("holder", ""))[:64]
                if not holder:
                    raise ValueError("holder required")
                if body.get("peek"):
                    return self._json({"holder": db.peek_lease(time.time(), LEASE_TTL)})
                return self._json({"holder": db.claim_lease(holder, time.time(), LEASE_TTL, bool(body.get("force")))})
            elif self.path == "/api/take-control":
                if state["status"] is None:
                    raise ValueError("no panel data yet")
                if cfg["mode"] != "auto":
                    raise ValueError("switch this copy to Auto to take control")
                with lock:
                    update_control(state["status"], force=True)
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
    # Starting up in Auto claims control: the most recently started Auto copy wins.
    holder = lease(force=True) if cfg["mode"] == "auto" else lease(peek=True)
    print(f"Control: {INSTANCE} {'took control' if holder == INSTANCE else 'on standby, holder: ' + str(holder)}"
          f" (referee: {LEADER_URL or 'this copy'})", flush=True)
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer((HOST, port), Handler).serve_forever()
