"""Decide whether to force-charge from grid so the battery reaches a minimum level before the price goes up.

Default plan: PG&E EV2-A. Off-peak 00:00-15:00; part-peak 15-16 and 21-24; peak 16-21 (every day).
Solar expected before the deadline (from the weather forecast) counts toward the minimum.
"""
from datetime import datetime, time, timedelta

# $/kWh, PG&E EV2-A bundled, effective 2026-03-01. Update from pge.com/tariffs when rates change.
EV2A = {
    "summer": {"off-peak": 0.22558, "part-peak": 0.42760, "peak": 0.53809},
    "winter": {"off-peak": 0.22558, "part-peak": 0.39428, "peak": 0.41099},
}


def hm12(t):
    """"15:00" / time / datetime -> "3 PM"; "10:24" -> "10:24 AM"."""
    t = time.fromisoformat(t) if isinstance(t, str) else t
    return f"{t.hour % 12 or 12}{f':{t.minute:02d}' if t.minute else ''} {'AM' if t.hour < 12 else 'PM'}"


def tou_period(now):
    h = now.hour
    period = "peak" if 16 <= h < 21 else "part-peak" if h == 15 or h >= 21 else "off-peak"
    season = "summer" if 6 <= now.month <= 9 else "winter"
    return period, season, EV2A[season][period]


def deadline_of(now, cfg):
    return datetime.combine(now.date(), time.fromisoformat(cfg["offpeak_end"]), now.tzinfo)


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


def decide(now, soc, cfg, pv_wh=0.0, charging_by_us=False):
    """Return dict(kind, charge, reason, start_at, projected_soc). kind: outside | ok | planned | charging.

    cfg keys: capacity_wh, charge_w, min_soc (% wanted at the deadline), max_soc,
              offpeak_start/offpeak_end "HH:MM" (offpeak_end = deadline), margin_min.
    Assumes the house runs from grid (not battery) before the deadline, so only solar and grid move the level.
    """
    cap, min_soc = cfg["capacity_wh"], cfg["min_soc"]
    projected = min(cfg["max_soc"], soc + pv_wh / cap * 100)
    out = {"charge": False, "start_at": None, "projected_soc": round(projected, 1)}
    if not (time.fromisoformat(cfg["offpeak_start"]) <= now.time() < time.fromisoformat(cfg["offpeak_end"])):
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
    if charging_by_us:
        # Re-plan while charging: if the sun now covers enough to start later, pause until then. The 20 min
        # gap keeps it from switching off and on every poll; inside the 2% buffer (missing 0) keep going.
        if missing_wh and start_at > now + timedelta(minutes=20):
            return {**out, "kind": "planned", "start_at": start_at, "reason": f"Short: {short}; grid charge paused for the sun, resumes {hm12(start_at)}"}
        return {**out, "kind": "charging", "charge": True, "start_at": start_at, "reason": f"Charging: {solar}, ~{projected:.0f}% by {hm12(cfg['offpeak_end'])} without grid; need {min_soc:.0f}%"}

    if now >= start_at:
        return {**out, "kind": "charging", "charge": True, "start_at": start_at, "reason": f"Short: {short}; grid charging now"}
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


ROUND_TRIP = 0.8  # battery charge + discharge efficiency: 1 kWh in -> 0.8 kWh back out


def price_at(dt):
    return tou_period(dt)[2]


def savings(rows, day_start, initial_kwh=0.0, initial_cost_per_kwh=None, max_gap_s=300):
    """Money saved by the DPU system powering the house instead of the grid.

    rows: [(datetime, load_w, grid_w, solar_w)] in time order (one per poll). Each reading
    holds until the next one; gaps longer than max_gap_s (app was down) are not counted.
    Power the system delivers to the house = load - grid. Solar covering it directly is free;
    the rest comes out of the battery at the average cost of what's stored in it. Energy going
    in costs the grid price at that time (solar: free) and only ROUND_TRIP of it comes back out.
    Battery contents before the first row are assumed grid-charged off-peak (initial_cost_per_kwh).
    Returns dollars (today, total), kWh delivered to the house (today_kwh, total_kwh), and
    by_day {"YYYY-MM-DD": [dollars, kwh]}.
    """
    if initial_cost_per_kwh is None:
        initial_cost_per_kwh = EV2A["winter"]["off-peak"] / ROUND_TRIP
    pool_kwh, pool_cost = initial_kwh, initial_kwh * initial_cost_per_kwh
    out = {"today": 0.0, "total": 0.0, "today_kwh": 0.0, "total_kwh": 0.0, "by_day": {}}
    for (t, load, grid, solar), (t_next, *_) in zip(rows, rows[1:]):
        dt_s = (t_next - t).total_seconds()
        if dt_s <= 0 or dt_s > max_gap_s or load is None or grid is None:
            continue
        h, price, solar = dt_s / 3600, price_at(t), solar or 0
        to_house = max(0.0, load - grid)                 # W the system supplies to the house
        direct = min(solar, to_house)                    # solar straight to the house: free
        from_batt_kwh = (to_house - direct) * h / 1000
        grid_in_kwh = max(0.0, grid - load) * h / 1000   # grid draw beyond the house = charging
        solar_in_kwh = max(0.0, solar - direct) * h / 1000
        pool_kwh += (grid_in_kwh + solar_in_kwh) * ROUND_TRIP
        pool_cost += grid_in_kwh * price
        avg = pool_cost / pool_kwh if pool_kwh > 0 else initial_cost_per_kwh
        used_cost = from_batt_kwh * avg
        pool_kwh, pool_cost = max(0.0, pool_kwh - from_batt_kwh), max(0.0, pool_cost - used_cost)
        delivered = direct * h / 1000 + from_batt_kwh
        saved = delivered * price - used_cost
        out["total"] += saved; out["total_kwh"] += delivered
        day = out["by_day"].setdefault(t.date().isoformat(), [0.0, 0.0])
        day[0] += saved; day[1] += delivered
        if t >= day_start:
            out["today"] += saved; out["today_kwh"] += delivered
    return out


def balance(dpu1_soc, grid_online, grid_charging, sun_left, sun_min, held, cfg):
    """Should DPU 2 be paused so the house drains DPU 1 first?

    DPU 1 (bigger, sunnier in the afternoon) nearly full -> pause DPU 2 so DPU 1 makes room for its
    afternoon solar instead of wasting it. Safety releases win over everything: grid down (need full
    backup), our grid charge running (charge both), DPU 1 drained to the release level, or no
    meaningful sun left today (sun_left below sun_min; None = unknown, rule runs on levels).
    Returns (hold, reason).
    """
    if not grid_online:
        return False, "grid is down: both batteries available for backup"
    if grid_charging:
        return False, "grid charging: both batteries charge"
    if sun_left is not None and sun_left < sun_min:
        return False, "little sun left today: nothing to make room for"
    if held:
        if dpu1_soc <= cfg["bal_release_soc"]:
            return False, f"DPU 1 down to {dpu1_soc:.0f}%: room for its solar again"
        return True, f"DPU 1 at {dpu1_soc:.0f}%: house runs on DPU 1 until it reaches {cfg['bal_release_soc']:.0f}%"
    if dpu1_soc >= cfg["bal_full_soc"]:
        return True, f"DPU 1 nearly full ({dpu1_soc:.0f}%): pausing DPU 2 so DPU 1 makes room for afternoon sun"
    return False, f"DPU 1 at {dpu1_soc:.0f}%: pauses DPU 2 at {cfg['bal_full_soc']:.0f}%"
