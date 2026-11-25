# Changelog

## 0.5 — 2026-11-25

First tagged release. Everything below is new.

### Smart charging
- Plans each day from the Open-Meteo solar forecast and the solar your panels have actually produced.
- Grid-charges only in the cheap off-peak window, and only as much as the evening needs (no fixed 60/80% target).
- Smart charge card: the expected 3 PM level as a gauge beside the headline, hidden once the deadline passes.
- Forecast chart covers every daylight hour until sunset, with measured solar overlaid as a translucent bar; past hours use what was learned before today.
- Per-DPU solar profile, only for the DPUs actually connected; unlearned hours are not treated as "no sun".
- Shows when grid charging finishes; stats refresh faster after a command.

### Balancing two battery stacks
- Keeps DPU 1 and DPU 2 within 10% of each other by pausing the emptier one.
- Does not pause DPU 2 to make room when DPU 1 is only slightly below it.
- Discharge control through the panel's backup reserve; force buttons no longer block it.
- Battery hold has its own Off / Dry-run / Auto mode, starting in Dry-run.

### Catching the panel
- Stops unplanned grid charging by setting the panel's solar reserve back.
- Warns in Activity when the panel grid-charges without our force charge, and names Storm Guard when it charges past the max.
- Force charge: resends an OFF the panel ignored (only after a panel report made after our OFF), and keeps each switch for 15 minutes.
- Pauses automation and shows a plain banner when the panel stops reporting for 20 minutes.
- Logs each stretch of failed polls in Activity: when it started and how many failed.
- Logs when the panel switches the house between grid and battery.

### Storm protection
- Raises the outage reserve to 60% while gusts over 20 mph are forecast.

### Rate plan
- Any time-of-use plan as a JSON file (`RATE_PLAN`), or edited in Settings → Rate plan; the plan is checked before saving.
- Supports weekend hours, holidays, summer/winter prices, and a cheap window that crosses midnight.
- Default plan: PG&E EV2-A.

### Dashboard
- Batteries card: outage reserve, max charge level and grid charge power.
- Day card with a date picker, daily energy flow diagram and meter-based savings.
- Energy flow diagram shows battery charge/discharge power, with direction from net power.
- Energy balance accounts for every kWh, including losses and battery efficiency.
- Savings: today, month and all time in one tile; the battery's change over the day is credited at the off-peak price.
- Settings: button in the top bar opens a modal dialog; Save and Close side by side, Esc closes.
- HUD-style restyle; stat tiles in 2/3/6 columns; header fits in two rows on phones.

### Running it
- Demo mode with no account needed; Docker image with a `/healthz` health check.
- Dashboard password (`DASHBOARD_PASSWORD`); requests through a proxy are refused without one.
- Every automation starts in Dry-run.

### Fixes
- `/api/state` no longer returns 502 while the panel grid-charges on its own.
- Sign-in through a proxy, non-ASCII passwords, and safe config writes.
- Panel downtime is no longer counted as losses.

### Project
- Open-sourced under the GNU AGPL v3, with a simpler README and screenshots.
