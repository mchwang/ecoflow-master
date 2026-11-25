"""Decide whether to force-charge from grid so the battery reaches a minimum level before the price goes up.

The utility rate plan (prices, hours, time zone) comes from a JSON file: rates/pge-ev2a.json by default, or the
file named by RATE_PLAN (see load_plan). The cheap window is the plan's longest run of off-peak hours; its end is
the deadline. Solar expected before the deadline (from the weather forecast) counts toward the minimum.
"""
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

def load_plan(plan):
    """Check a rate plan (dict from JSON) and add the derived fields; raises ValueError naming what's wrong.

    {"name": str, "timezone": IANA name, "summer_months": [1-12...] (may be empty),
     "hours": {period: [[start_hour, end_hour), ...]}  # whole hours 0-24; every hour not listed is "off-peak"
     "weekend_hours": {...}  # optional, same shape, for Saturday and Sunday; without it weekends use "hours"
     "holidays": ["2026-12-25", "07-04", ...]  # optional, days that use weekend_hours ("MM-DD" = every year)
     "prices": {"summer": {period: $/kWh}, "winter": {...}}}  # every period plus "off-peak"; summer only if used
    shortcut: no seasonal hours, and holidays are listed by date (no 4th-Thursday rules); upgrade when a user needs it.
    """
    def bad(msg):
        raise ValueError(f"rate plan {plan.get('name', '?') if isinstance(plan, dict) else '?'}: {msg}")
    if not isinstance(plan, dict) or not isinstance(plan.get("name"), str):
        bad('needs a "name"')
    try:
        tz = ZoneInfo(plan.get("timezone", ""))
    except Exception:
        bad(f'"timezone" {plan.get("timezone")!r} is not an IANA time zone like "America/New_York"')
    months = plan.get("summer_months", [])
    if not isinstance(months, list) or any(m not in range(1, 13) for m in months):
        bad('"summer_months" must be a list of month numbers 1-12')
    def iso_day(x):  # a real "YYYY-MM-DD" or "MM-DD" (2000: a leap year, so "02-29" is fine), not a week date like "2026-W52-5"
        y = x if len(x) == 10 else f"2000-{x}"
        return date.fromisoformat(y).isoformat() == y
    hol = plan.get("holidays", [])
    try:
        ok = isinstance(hol, list) and all(iso_day(x) for x in hol)
    except Exception:
        ok = False
    if not ok:
        bad('"holidays" must be a list of dates like "2026-12-25", or "12-25" for every year')
    week = _day_hours(plan.get("hours"), bad)
    weekend = _day_hours(plan["weekend_hours"], bad, "weekend_hours") if "weekend_hours" in plan else week
    for season in ("summer", "winter") if months else ("winter",):
        for period in set(week[0] + weekend[0]):
            price = (plan.get("prices") or {}).get(season, {}).get(period)
            if not isinstance(price, (int, float)) or price < 0:
                bad(f'"prices" needs a $/kWh number for {season} {period}')
    return {**plan, "tz": tz, "by_hour": week[0], "window": week[1], "by_hour_weekend": weekend[0], "window_weekend": weekend[1]}


DERIVED = ("tz", "by_hour", "window", "by_hour_weekend", "window_weekend")  # added by load_plan, not in the file


def _day_hours(hours, bad, key="hours"):
    """One day's {period: [[start, end), ...]} -> (period of each hour 0-23, cheap window)."""
    if hours is not None and not isinstance(hours, dict):
        bad(f'"{key}" must be an object like {{"peak": [[16, 21]]}}')
    by_hour = ["off-peak"] * 24
    for name, ranges in (hours or {}).items():
        if name == "off-peak":
            bad(f'"{key}" lists only the expensive periods; every hour not listed is off-peak')
        for r in ranges if isinstance(ranges, list) else [None]:
            if not (isinstance(r, list) and len(r) == 2 and all(isinstance(x, int) for x in r) and 0 <= r[0] < r[1] <= 24):
                bad(f'"{key}" for {name}: {r!r} is not [start_hour, end_hour] with 0 <= start < end <= 24')
            for h in range(*r):
                if by_hour[h] != "off-peak":
                    bad(f'"{key}": hour {h} is in both {by_hour[h]} and {name}')
                by_hour[h] = name
    off = [by_hour[h] == "off-peak" for h in range(24)]
    if all(off) or not any(off):
        bad(f'"{key}" needs some off-peak hours and some more expensive ones (a flat rate has nothing to time)'
            + ('; weekends with no expensive hours are not supported yet, so leave "weekend_hours" out' if key == "weekend_hours" else ""))
    # cheap window = longest run of off-peak hours, wrapping past midnight (e.g. 21:00-16:00)
    best = (0, 0)
    for start in range(24):
        if off[start] and not off[start - 1]:
            n = next(k for k in range(1, 25) if not off[(start + k) % 24])
            best = max(best, (n, -start))
    n, start = best[0], -best[1]
    return by_hour, {"offpeak_start": f"{start:02d}:00", "offpeak_end": f"{(start + n) % 24:02d}:00"}


PLAN = load_plan(json.loads((Path(__file__).parent / "rates" / "pge-ev2a.json").read_text()))


def use_plan(path):
    """Switch to the rate plan in a JSON file (RATE_PLAN); bad files stop the app with the reason."""
    global PLAN
    PLAN = load_plan(json.loads(Path(path).read_text()))


def _weekend(d):
    """Weekend hours apply on Saturday, Sunday and the plan's holidays."""
    hol = PLAN.get("holidays", [])
    return d.weekday() >= 5 or f"{d:%Y-%m-%d}" in hol or f"{d:%m-%d}" in hol


def _off(t):
    """Is the hour t is in off-peak, by that day's own hours?"""
    return PLAN["by_hour_weekend" if _weekend(t) else "by_hour"][t.hour] == "off-peak"


def _run(last):
    """[start, end) of the off-peak hours around the hour starting at `last`, walking the real calendar
    (so Friday evening uses Friday's hours and Saturday morning Saturday's); at most 23 h long.
    shortcut: a run longer than 23 h loses its earliest hours (they stay off-peak, but closed to grid charging); upgrade
    if a plan with a day of nearly all off-peak next to a mostly off-peak day needs them."""
    hour, start = timedelta(hours=1), last
    end = last + hour
    while _off(end) and end - start < 23 * hour:
        end += hour
    while _off(start - hour) and end - start < 23 * hour:
        start -= hour
    return start, end


def _runs(now):
    """The cheap windows of yesterday through 2 days ahead. Each day's window is the one its own hours give
    (the longest off-peak run), anchored on the hour it ends; its start can be the evening before."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    runs = []
    for k in range(-1, 3):
        d = day + timedelta(days=k)
        end = int(PLAN["window_weekend" if _weekend(d) else "window"]["offpeak_end"][:2])
        runs.append(_run(d.replace(hour=(end - 1) % 24)))  # its last cheap hour (end 00:00 = 11 PM)
    return runs


def _window_at(now):
    """(start, end) of the cheap window now is in, else of the next one."""
    runs = _runs(now)
    return next((r for r in runs if r[0] <= now < r[1]), None) or min(r for r in runs if r[0] > now)


def charge_window(now=None):
    """{"offpeak_start", "offpeak_end"} "HH:MM" of the cheap window in effect at `now` (end = the deadline),
    or of the next one, and "open": whether now is inside it; without `now`, the plain weekday window."""
    if now is None:
        return dict(PLAN["window"])
    start, end = _window_at(now)
    return {"offpeak_start": f"{start:%H:%M}", "offpeak_end": f"{end:%H:%M}", "open": start <= now < end}


def next_window(t):
    """Start of the first cheap window that begins after t."""
    start, end = _window_at(t)
    return start if start > t else _window_at(end)[0]


def offpeak_end(now):
    """End of the off-peak hours now is in (any off-peak block, e.g. SDG&E's 10 AM-2 PM, not only the cheap
    window); now itself when it isn't off-peak."""
    return _run(now.replace(minute=0, second=0, microsecond=0))[1] if _off(now) else now


def in_window(t, cfg):
    start, end = time.fromisoformat(cfg["offpeak_start"]), time.fromisoformat(cfg["offpeak_end"])
    return start <= t < end if start < end else t >= start or t < end  # second form: window crosses midnight


def hm12(t):
    """"15:00" / time / datetime -> "3 PM"; "10:24" -> "10:24 AM"."""
    t = time.fromisoformat(t) if isinstance(t, str) else t
    return f"{t.hour % 12 or 12}{f':{t.minute:02d}' if t.minute else ''} {'AM' if t.hour < 12 else 'PM'}"


def tou_period(now):
    period = PLAN["by_hour_weekend" if _weekend(now) else "by_hour"][now.hour]
    season = "summer" if now.month in PLAN.get("summer_months", []) else "winter"
    return period, season, PLAN["prices"][season][period]


def deadline_of(now, cfg):
    """End of the cheap window now is in: today's, or tomorrow's when the window crosses midnight and it's evening.
    Outside the window ("open" False), the end of the last one, as the weekday-only code gave (not Friday's
    hours pinned on Saturday's window)."""
    if cfg.get("open") is False:
        return max(end for _, end in _runs(now) if end <= now)
    end = datetime.combine(now.date(), time.fromisoformat(cfg["offpeak_end"]), now.tzinfo)
    start = time.fromisoformat(cfg["offpeak_start"])
    return end + timedelta(days=1) if start > end.time() and now.time() >= start else end


def solar_wh(now, until, hourly, pv_kw, pr):
    """Expected PV energy (Wh) between now and `until` for one array size (kW at 1000 W/m2) x performance ratio.

    hourly: [(end_of_hour datetime, irradiance W/m2)], Open-Meteo style (value = mean of the preceding hour).
    Irradiance already reflects clouds and is 0 after sunset, so this covers "daylight left" too.
    shortcut: horizontal irradiance x array kW x performance ratio; tilt/orientation is folded into pr.
    """
    return solar_wh_hourly(now, until, hourly, {}, pv_kw * pr)


def solar_wh_hourly(now, until, hourly, kw_by_hour, fallback_kw):
    """Like solar_wh, but with a learned effective kW per hour of day (captures which way panels face:
    a morning array has high kW at 9 AM and low at 4 PM). Hours not in kw_by_hour use fallback_kw."""
    total = 0.0
    for end, ghi in hourly:
        start = end - timedelta(hours=1)
        overlap = (min(end, until) - max(start, now)).total_seconds() / 3600
        if overlap > 0:
            total += ghi * kw_by_hour.get(start.hour, fallback_kw) * overlap
    return total


def _hours(now, until, hourly, kw_by_hour, fallback_kw, load_by_hour, load_now):
    """Yield (solar_wh, home_wh) for each clock-hour slice of [now, until)."""
    ghi = {end - timedelta(hours=1): g for end, g in hourly}
    t = now
    while t < until:
        start = t.replace(minute=0, second=0, microsecond=0)
        nxt = min(start + timedelta(hours=1), until)
        h = (nxt - t).total_seconds() / 3600
        yield (ghi.get(start, 0) or 0) * kw_by_hour.get(start.hour, fallback_kw) * h, load_by_hour.get(start.hour, load_now) * h
        t = nxt


def simulate(now, until, soc, hourly, kw_by_hour, fallback_kw, load_by_hour, load_now, cap, floor_soc, max_soc):
    """Battery level at `until` with the house running from it: each hour solar goes in and the home load
    comes out. Below the panel's reserve (floor_soc) the grid powers the house; solar above max_soc is wasted.
    Returns dict(soc, solar_wh, home_wh, wasted_wh)."""
    level, lo, hi = soc / 100 * cap, floor_soc / 100 * cap, max_soc / 100 * cap
    solar = home = wasted = 0.0
    for s, l in _hours(now, until, hourly, kw_by_hour, fallback_kw, load_by_hour, load_now):
        solar += s; home += l
        level = max(min(level, lo), level + s - l)  # the house never pulls it below the reserve
        if level > hi:
            wasted += level - hi; level = hi
    return {"soc": level / cap * 100, "solar_wh": solar, "home_wh": home, "wasted_wh": wasted}


def room_target(target, deadline, hourly, kw_by_hour, fallback_kw, load_by_hour, load_now, cap, floor_soc, max_soc):
    """Lower the deadline target so the sun after it (beyond what the house uses) fits under max_soc.
    Returns (target, room %): room is the largest build-up of surplus solar between the deadline and midnight."""
    run = peak = 0.0
    for s, l in _hours(deadline, deadline.replace(hour=23, minute=59), hourly, kw_by_hour, fallback_kw, load_by_hour, load_now):
        run = max(0.0, run + s - l)  # a drained battery can't go below empty, so restart the build-up
        peak = max(peak, run)
    room = peak / cap * 100
    return max(floor_soc, min(target, max_soc - room)), room


def evening_target(target, deadline, hourly, kw_by_hour, fallback_kw, load_by_hour, load_now, cap, floor_soc, max_soc, until=None):
    """Lower the deadline target to what the house needs from the deadline until the next cheap window (`until`,
    default midnight) as load minus solar, so no grid-charged energy is left to run the house off-peak, where its
    ~20% round-trip loss costs more than grid power directly. Returns (target, need %).
    shortcut: solar surplus counts in full even past max_soc; room_target already caps the target for that."""
    run = need = 0.0
    until = until or deadline.replace(hour=0, minute=0) + timedelta(days=1)
    for s, l in _hours(deadline, until, hourly, kw_by_hour, fallback_kw, load_by_hour, load_now):
        run += l - s
        need = max(need, run)  # deepest drain below the deadline level before the cheap window
    # 10% headroom: short at peak costs ~$0.13/kWh, a leftover kWh only ~$0.06, so err on the high side
    pct = need * 1.1 / cap * 100
    return max(floor_soc, min(target, floor_soc + pct)), pct


def afternoon_sun(now, cfg, hourly):
    """Forecast sunlight (Wh/m2) from today's deadline to the end of the day; None without a forecast."""
    if not hourly:
        return None
    start = deadline_of(now, cfg)
    return solar_wh(start, start.replace(hour=23, minute=59), hourly, 1, 1)  # 1 kW at 1000 W/m2, pr 1 -> Wh/m2


def target_soc(cfg, sun_after_wh):
    """Minimum % wanted by the deadline. Little or no sun after the deadline means solar won't help
    through peak, so aim higher. No forecast counts as no sun (charge more, never less).
    Returns (percent, why)."""
    sunny = sun_after_wh is not None and sun_after_wh >= cfg["sunny_after_wh"]
    want = cfg["min_soc"] if sunny else cfg["min_soc_cloudy"]
    why = ("no forecast" if sun_after_wh is None else
           f"{'sunny' if sunny else 'little sun'} after {hm12(cfg['offpeak_end'])}")
    return min(want, cfg["max_soc"]), why


def decide(now, soc, cfg, pv_wh=0.0, charging_by_us=False, projected=None):
    """Return dict(kind, charge, reason, start_at, projected_soc, done_at). kind: outside | ok | planned | charging.
    done_at (charging only): when the battery is expected to reach the stop level (min + 2%), at the latest the deadline.

    cfg keys: capacity_wh, charge_w, min_soc (% wanted at the deadline), max_soc,
              offpeak_start/offpeak_end "HH:MM" (offpeak_end = deadline), margin_min.
    projected: level expected at the deadline without grid charging (see simulate, which counts the house
    running from the battery). Without it, assumes all solar goes into the battery.
    """
    cap, min_soc = cfg["capacity_wh"], cfg["min_soc"]
    projected = min(cfg["max_soc"], soc + pv_wh / cap * 100) if projected is None else projected
    out = {"charge": False, "start_at": None, "projected_soc": round(projected, 1)}
    if not cfg.get("open", in_window(now.time(), cfg)):  # "open": checked on the real calendar (charge_window)
        return {**out, "kind": "outside", "reason": f"Outside off-peak window ({hm12(cfg['offpeak_start'])}-{hm12(cfg['offpeak_end'])}); never grid-charge at high price"}
    solar = f"solar adds ~{pv_wh / 1000:.1f} kWh"
    # Keep charging 2% past the minimum once started, so a small forecast change doesn't flip it on and off.
    goal = min_soc + (2 if charging_by_us else 0)
    if projected >= goal:
        return {**out, "kind": "ok", "reason": f"OK: {soc:.0f}% now, {solar} -> ~{projected:.0f}% by {hm12(cfg['offpeak_end'])} (min {min_soc:.0f}%)"}
    # Grid-charge as late as possible so the sun gets every chance first: start just in time for the deadline.
    missing_wh = max(0.0, min_soc - projected) / 100 * cap
    minutes = missing_wh / (cfg["charge_w"] * 0.9) * 60 + cfg["margin_min"]  # 0.9: AC charge losses
    start_at = deadline_of(now, cfg) - timedelta(minutes=minutes)
    short = f"{solar}, only ~{projected:.0f}% by {hm12(cfg['offpeak_end'])} (min {min_soc:.0f}%)"
    # Charging stops once projected reaches min + 2%; each grid Wh stored raises projected by the same amount.
    stop_wh = max(0.0, min_soc + 2 - projected) / 100 * cap
    done_at = min(deadline_of(now, cfg), now + timedelta(hours=stop_wh / (cfg["charge_w"] * 0.9)))
    if charging_by_us:
        # Re-plan while charging: if the sun now covers enough to start later, pause until then. The 20 min
        # gap keeps it from switching off and on every poll; inside the 2% buffer (missing 0) keep going.
        if missing_wh and start_at > now + timedelta(minutes=20):
            return {**out, "kind": "planned", "start_at": start_at, "reason": f"Short: {short}; grid charge paused for the sun, resumes {hm12(start_at)}"}
        return {**out, "kind": "charging", "charge": True, "start_at": start_at, "done_at": done_at,
                "reason": f"Charging: {solar}, ~{projected:.0f}% by {hm12(cfg['offpeak_end'])} without grid; need {min_soc:.0f}%; done ~{hm12(done_at)}"}

    if now >= start_at:
        return {**out, "kind": "charging", "charge": True, "start_at": start_at, "done_at": done_at,
                "reason": f"Short: {short}; grid charging now, done ~{hm12(done_at)}"}
    return {**out, "kind": "planned", "start_at": start_at, "reason": f"Short: {short}; grid charge starts {hm12(start_at)}"}


def fit_array_kw(samples, max_soc, min_ghi=50):
    """Effective solar array kW learned from history: measured PV watts per forecast W/m2.

    samples: [(solar_w, ghi, soc)]. Skips dim hours (noisy) and times the battery was near full
    (the charger throttles PV then, so it would understate the array). Includes all losses/tilt,
    so use it with pr=1. Returns (kw, n_samples_used).
    """
    used = [(w, g) for w, g, soc in samples if g >= min_ghi and soc < max_soc - 1]
    total_ghi = sum(g for _, g in used)
    return (sum(w for w, _ in used) / total_ghi if total_ghi else 0.0), len(used)


def fit_hourly_kw(samples, max_soc, min_ghi=50, min_n=20):
    """One DPU's solar array, learned per hour of day: {hour: effective kW at 1000 W/m2}.

    samples: [(hour, solar_w, ghi, soc)]. Same filters as fit_array_kw (dim light, battery near full).
    Only hours with at least min_n usable readings (20 minutes of that hour) are returned.
    """
    acc = {}
    for h, w, g, soc in samples:
        if w is not None and g and g >= min_ghi and soc is not None and soc < max_soc - 1:
            a = acc.setdefault(h, [0.0, 0.0, 0])
            a[0] += w; a[1] += g; a[2] += 1
    return {h: a[0] / a[1] for h, a in acc.items() if a[2] >= min_n}


def daily_solar_kwh(rows, max_gap_s=300):
    """rows: [(datetime, solar1_w, solar2_w)] in time order -> {"YYYY-MM-DD": [kwh1, kwh2]}.
    Each reading holds until the next; gaps longer than max_gap_s are not counted."""
    out = {}
    for (t, w1, w2), (t_next, *_) in zip(rows, rows[1:]):
        dt_s = (t_next - t).total_seconds()
        if 0 < dt_s <= max_gap_s and (w1 is not None or w2 is not None):
            day = out.setdefault(t.date().isoformat(), [0.0, 0.0])
            day[0] += (w1 or 0) * dt_s / 3.6e6
            day[1] += (w2 or 0) * dt_s / 3.6e6
    return out


def hourly_kwh(rows, max_gap_s=300):
    """rows: one day's [(datetime, solar_w, load_w, grid_w)] in time order -> 24 x [solar, home, from-grid] kWh.
    Same counting as daily_solar_kwh; grid export (negative) is not counted as drawn from the grid."""
    out = [[0.0, 0.0, 0.0] for _ in range(24)]
    for (t, solar, load, grid), (t_next, *_) in zip(rows, rows[1:]):
        dt_s = (t_next - t).total_seconds()
        if 0 < dt_s <= max_gap_s:
            h = out[t.hour]
            for i, w in enumerate((solar, load, max(0, grid or 0))):
                h[i] += (w or 0) * dt_s / 3.6e6
    return out


def day_report(rows, cap_wh, max_gap_s=300):
    """One day's energy and money from the minute readings.

    rows: [(datetime, soc %, load_w, grid_w, solar_w[, battery_wh])] in time order; each reading holds until the next,
    gaps over max_gap_s (app down) are skipped. Each minute the house takes solar first, then grid, then
    battery; whatever solar and grid is left over goes into the battery. Grid kWh and $ are split by rate
    period. Saved = what the house would have cost all from the grid minus what the grid actually cost, plus the
    battery's change over the day at the off-peak price (what refilling it from the grid would cost). Without that,
    a grid charge counts as a cost the day it's bought and solar stored in the battery counts for nothing until it's
    used; across days the credits cancel (stored today = drawn tomorrow), so totals don't drift.
    The battery's change is added up over the same counted intervals, from the panel's Wh reading when both ends
    have it (the whole-% level moves in ~1% steps), so app downtime adds nothing to either side. Energy balance:
    energy in (grid + solar + drawn from earlier charge) =
    home + stored + losses; balance_kwh is the raw losses figure (negative = the meters disagree a little).
    """
    periods = sorted(set(PLAN["by_hour"] + PLAN["by_hour_weekend"]), key=lambda p: PLAN["prices"]["winter"][p])  # cheapest first, e.g. off-peak, part-peak, peak
    grid_home, grid_batt, grid_usd = (dict.fromkeys(periods, 0.0) for _ in range(3))
    solar_home = solar_batt = batt_home = home = without = delta = 0.0
    off_price = 0.0
    for (t, soc, load, grid, solar, *wh), (t_next, soc_next, *rest) in zip(rows, rows[1:]):
        dt_s = (t_next - t).total_seconds()
        if not 0 < dt_s <= max_gap_s or load is None or grid is None:
            continue
        wh_next = rest[3:4]
        if wh and wh[0] is not None and wh_next and wh_next[0] is not None:
            delta += (wh_next[0] - wh[0]) / 1000
        elif soc is not None and soc_next is not None:
            delta += (soc_next - soc) / 100 * cap_wh / 1000
        k = dt_s / 3.6e6                                   # W -> kWh over this reading
        load, imp, solar = max(0.0, load), max(0.0, grid), max(0.0, solar or 0)
        period, season, price = tou_period(t)
        off_price = PLAN["prices"][season]["off-peak"]
        sh = min(solar, load); gh = min(imp, load - sh)
        solar_home += sh * k; solar_batt += (solar - sh) * k
        grid_home[period] += gh * k; grid_batt[period] += (imp - gh) * k; grid_usd[period] += imp * k * price
        batt_home += (load - sh - gh) * k; home += load * k; without += load * k * price
    into = solar_batt + sum(grid_batt.values())
    grid_cost = sum(grid_usd.values())
    energy_in = solar_home + solar_batt + sum(grid_home.values()) + sum(grid_batt.values()) + max(0.0, -delta)
    balance = into + max(0.0, -delta) - batt_home - max(0.0, delta)  # = energy_in - home - stored
    return {"grid_home": grid_home, "grid_batt": grid_batt, "grid_usd": grid_usd,
            "grid_kwh": {p: grid_home[p] + grid_batt[p] for p in periods},
            "solar_home": solar_home, "solar_batt": solar_batt, "solar_kwh": solar_home + solar_batt,
            "batt_home": batt_home, "home_kwh": home, "drawn_kwh": max(0.0, -delta), "stored_kwh": max(0.0, delta),
            "losses_kwh": max(0.0, balance), "balance_kwh": balance,  # the flow diagram can't draw a negative
            "energy_in": energy_in, "loss_pct": max(0.0, balance) / energy_in * 100 if energy_in else 0.0,
            # share of everything that passed through the battery (charged today or drawn from earlier) not lost
            "batt_eff": min(100.0, (1 - balance / (into + max(0.0, -delta))) * 100)
                        if into + max(0.0, -delta) > 0.05 and balance >= -0.05 else None,  # unknown if meters disagree
            "cost_without": without, "grid_cost": grid_cost, "battery_value": delta * off_price,
            "saved": without - grid_cost + delta * off_price}


def hold_battery(period, grid_online, waste_if_held_kwh, held):
    """Should the battery be held (house on grid) right now? Returns (hold, reason).

    Off-peak, running the house from the battery and refilling it from the grid costs ~10% more than
    using grid power directly, so the battery is saved for part-peak/peak. Exceptions: an outage, and
    solar that would overflow a held battery before the deadline (then the house uses the battery to make
    room). Gap between 0.05 and 0.2 kWh keeps the last state, so it doesn't flip every minute.
    """
    if not grid_online:
        return False, "grid is down: whole battery available for backup"
    if period != "off-peak":
        return False, f"{period}: the battery runs the house"
    if waste_if_held_kwh > 0.2:
        return False, f"holding would waste ~{waste_if_held_kwh:.1f} kWh of solar, so the house uses the battery to make room"
    if held or waste_if_held_kwh < 0.05:
        return True, "off-peak: house on cheap grid power, battery saved for the expensive hours"
    return False, "a little solar would overflow: the battery keeps running the house"


def balance(dpu1_soc, dpu2_soc, grid_online, grid_charging, sun_left, sun_min, paused, cfg):
    """Which DPU should be paused (0 = neither, 1 or 2), so the house draws from the other one?

    Two rules. Keep the DPUs within cfg["bal_max_spread"] % of each other: at that spread the fuller one is
    paused until they're within 3% (pausing only stops a DPU running the house; its own solar still charges it,
    so the gap closes only while the batteries run the house). And DPU 1 (bigger, sunnier in the afternoon) nearly
    full -> pause DPU 2 so DPU 1 makes room for its afternoon solar, until DPU 1 drops to the release level or
    5% below DPU 2. Safety releases win over both: grid down (need full backup) and our grid charge running
    (charge both). No meaningful sun left today (sun_left below sun_min; None = unknown) ends the room rule.
    paused: the DPU we paused now (0 = none). Returns (pause, reason).
    """
    if not grid_online:
        return 0, "grid is down: both batteries available for backup"
    if grid_charging:
        return 0, "grid charging: both batteries charge"
    gap, lim = dpu1_soc - dpu2_soc, cfg["bal_max_spread"]
    levels = f"DPU 1 {dpu1_soc:.0f}%, DPU 2 {dpu2_soc:.0f}%"
    if gap <= -lim or (paused == 1 and gap < -3):
        return 1, f"{levels}: pausing DPU 1 so the house drains DPU 2 until they're within 3%"
    if gap >= lim or (paused == 2 and gap > 3):
        return 2, f"{levels}: pausing DPU 2 so the house drains DPU 1 until they're within 3%"
    if sun_left is not None and sun_left < sun_min:
        return 0, "little sun left today: nothing to make room for"
    if paused == 2:
        if dpu1_soc <= cfg["bal_release_soc"]:
            return 0, f"DPU 1 down to {dpu1_soc:.0f}%: room for its solar again"
        if gap <= -5:
            return 0, f"{levels}: DPU 1 well below DPU 2, so both run the house again"
        return 2, f"DPU 1 at {dpu1_soc:.0f}%: house runs on DPU 1 until it reaches {cfg['bal_release_soc']:.0f}%"
    if dpu1_soc >= cfg["bal_full_soc"] and gap > -5:  # same 5% as the release above, or it flaps
        return 2, f"DPU 1 nearly full ({dpu1_soc:.0f}%): pausing DPU 2 so DPU 1 makes room for afternoon sun"
    return 0, f"{levels}: within {lim:.0f}%; pauses DPU 2 when DPU 1 reaches {cfg['bal_full_soc']:.0f}%"
