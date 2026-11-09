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


def evening_target(target, deadline, hourly, kw_by_hour, fallback_kw, load_by_hour, load_now, cap, floor_soc, max_soc):
    """Lower the deadline target to what the house needs from the deadline to midnight (load minus solar), so
    no grid-charged energy is left at midnight to run the house off-peak, where its ~20% round-trip loss
    costs more than grid power directly. Returns (target, need %).
    shortcut: solar surplus counts in full even past max_soc; room_target already caps the target for that."""
    run = need = 0.0
    midnight = deadline.replace(hour=0, minute=0) + timedelta(days=1)
    for s, l in _hours(deadline, midnight, hourly, kw_by_hour, fallback_kw, load_by_hour, load_now):
        run += l - s
        need = max(need, run)  # deepest drain below the 3 PM level before midnight
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




def price_at(dt):
    return tou_period(dt)[2]


def day_report(rows, cap_wh, max_gap_s=300):
    """One day's energy and money from the minute readings.

    rows: [(datetime, soc %, load_w, grid_w, solar_w[, battery_wh])] in time order; each reading holds until the next,
    gaps over max_gap_s (app down) are skipped. Each minute the house takes solar first, then grid, then
    battery; whatever solar and grid is left over goes into the battery. Grid kWh and $ are split by PG&E
    period. Saved = what the house would have cost all from the grid minus what the grid actually cost, plus the
    battery's change over the day at the off-peak price (what refilling it from the grid would cost). Without that,
    a grid charge counts as a cost the day it's bought and solar stored in the battery counts for nothing until it's
    used; across days the credits cancel (stored today = drawn tomorrow), so totals don't drift.
    The battery's change is added up over the same counted intervals, from the panel's Wh reading when both ends
    have it (the whole-% level moves in ~1% steps), so app downtime adds nothing to either side. Energy balance:
    energy in (grid + solar + drawn from earlier charge) =
    home + stored + losses; balance_kwh is the raw losses figure (negative = the meters disagree a little).
    """
    periods = ("off-peak", "part-peak", "peak")
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
        off_price = EV2A[season]["off-peak"]
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
        return True, "off-peak: house on cheap grid power, battery saved for 3 PM"
    return False, "a little solar would overflow: the battery keeps running the house"


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
