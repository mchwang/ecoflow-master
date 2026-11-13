# Home Power: EcoFlow SHP2 + DPU dashboard

A local dashboard for an EcoFlow Smart Home Panel 2 with Delta Pro Ultra batteries. It also does automatic grid charging for **PG&E EV2-A**.

The automation works like this: it reads the hourly sunlight forecast from [Open-Meteo](https://open-meteo.com), which is free and needs no key. The forecast already reflects cloud cover and drops to zero after sunset. From it, the planner estimates how much solar will reach the batteries before 3 pm, when part-peak starts. If the predicted level at 3 pm is below the minimum (**60%**, or **80%** when the forecast shows little sun after 3 pm, under 300 Wh/m²; both adjustable), it turns on SHP2 **force charge** (`chNForceCharge`) just long enough to cover the shortfall. It never grid-charges during part-peak or peak.

The panel runs the house from the battery at any hour (down to its backup reserve), so the prediction is an hour-by-hour simulation: solar goes in, the house's usual load for that hour (learned from the last 14 days) comes out, and solar above the panel's max charge level counts as wasted. The grid charge starts as late as possible and pauses if the sun moves that time later. If the spare sun after 3 pm (beyond what the house uses) would overfill the battery, the 3 pm target is lowered so that sun fits.

**Discharge control.** The panel never runs the house from the battery below its backup reserve, so the app sets that reserve (`backupReserveSoc`, sent with `PD303_APP_SET`; tested on the panel 2026-11-06). Off-peak it **holds** the battery (reserve = current level, raised in 2% steps as solar fills it), because running the house from the battery and refilling it from the grid costs ~10% more than grid power directly. It **releases** it (reserve = the Outage reserve setting, default 20%) from 3 pm, in a grid outage, or when holding would waste solar before 3 pm (forecast overflow above 0.2 kWh; back to holding below 0.05 kWh). Only the copy in control on Auto sends; any other copy releases a hold it left. Set the panel's operating mode to Self-powered. **Caution:** the panel tops the battery up from the grid (at its 4 kW AC charge rate) whenever the battery is below the reserve, so a reserve at the current level started an unplanned grid charge (2026-11-06). Battery hold therefore has its own mode (Settings, default Dry-run) and stays log-only until a safe hold method is verified. Set your latitude/longitude and array size on the dashboard; until then solar counts as 0.

**Unplanned grid charging.** The panel can also charge from the grid on its own, for example up to its solar reserve (on 2026-11-09 it kept charging to 96% after the app turned force charge off, and the afternoon sun was wasted). Whenever more than 500 W goes from the grid into the battery without the app's force charge, Activity shows a warning with the panel's backup and solar reserves, and a second line when it ends.


**DPU balancing.** The app keeps the two DPUs within 10% of each other (setting: Max difference between DPUs). When one is 10% fuller, it pauses the other with the panel's battery enable switch (`ch1EnableSet` / `ch2EnableSet`), so the house drains the fuller one until they are within 3%. A paused DPU still charges from its own solar. Separately, while DPU 1 (the larger battery, which gets the afternoon sun) is nearly full (90%), the app pauses DPU 2 so the house drains DPU 1 first and makes room for its afternoon solar. DPU 2 resumes when DPU 1 drops to 75% or falls 5% below DPU 2, or when little sunlight is left today. Both rules let go when the grid goes down or grid charging starts, only one DPU is ever paused, and the app only resumes a battery it paused itself. Balancing has its own Off / Dry-run / Auto setting and starts in Dry-run. It only matters while the batteries are running the house.

```bash
cp .env.example .env      # add access key + secret key (panel SN is auto-detected)
python3 server.py         # http://127.0.0.1:8787  (demo data if .env is empty)
python3 -m unittest       # planner, signing, store and login tests
```

Modes:
- **Off:** does nothing.
- **Dry-run** (default): logs what it would do.
- **Auto:** sends the commands to the panel.

The "Force charge ON/OFF" buttons send the command once; in Auto the plan stays in control and sets it back to what it wants at the next poll (within a minute). They are for testing, not an override.
Settings are saved to `config.json`. The EV2-A prices in `planner.py` are the 2026-03-01 tariff; update them when PG&E changes rates.

## Data

`DATA_DIR` (default `./data`, `/data` in Docker) holds:
- `homepower.db` (SQLite):
  - `readings`: one row per minute with battery %, solar W, load W, grid W, forecast sunlight, and whether grid charging is on
  - `events`: the activity log
  - `readings` also stores each DPU's solar watts and battery % (`solar1_w`, `solar2_w`, `soc1`, `soc2`). The two arrays face different ways, so each DPU's solar is learned **per hour of day**. Once both DPUs have 20+ sunny minutes in an hour (over 14 days), that hour's profile replaces the combined estimate in the 3 PM forecast, and balancing uses DPU 1's own expected solar.
  - `weather_hourly`: Open-Meteo per hour (total/direct/diffuse sunlight, cloud %, temperature). Past hours are refreshed with Open-Meteo's after-the-fact estimate, and the learning matches readings to these.
  - `daily_savings`: one row per day: what the house would have cost all from the grid (`cost_without`), what the grid actually cost (`grid_cost`), `saved` = the difference plus the battery's change over the day at the off-peak price (so a grid charge or stored solar isn't counted as a loss before it's used; the credits cancel across days), home kWh, and solar kWh per DPU. Both costs use the PG&E price at each minute, so solar, the battery and its charging losses all show up in the difference. Today is recalculated every minute from `readings`; past days are stored once. `GET /api/savings` returns the history; `GET /api/day?date=YYYY-MM-DD` returns one day's hourly kWh, energy flow (grid by PG&E period with cost, solar, battery, home, losses) and savings.
- `config.json`: dashboard settings and automation state.

The solar learning reads the last 14 days of `readings`. Growth is about 0.5M rows (~30 MB) a year.

## Deploy on Coolify

1. Push this folder to a **private** Git repo and add it in Coolify as an Application. Choose **Dockerfile** as the build pack and set the exposed port to **8787**.
2. Environment variables:
   - `ECOFLOW_ACCESS_KEY` and `ECOFLOW_SECRET_KEY`
   - `ECOFLOW_SN` (optional)
   - `DASHBOARD_PASSWORD`: a long random string. **Required**: the container refuses to start without it.
   - `DASHBOARD_USER` (optional, default `admin`)
   - `ECOFLOW_HOST` (optional): defaults to `https://api-a.ecoflow.com`, the Americas region. Use `https://api-e.ecoflow.com` for a Europe account. Don't use the global `api.ecoflow.com`: it routes by the server's location, so a VPS outside the US gets the Europe server, which rejects US keys with `8513 accessKey is invalid`.
3. Persistent storage: add a volume mounted at **`/data`**. Use a volume, not a host bind mount: the app runs as a non-root user and needs to write there.
4. Health check path: `/healthz`. It's the only URL that works without a login.
5. Set a domain so Coolify's proxy serves it over HTTPS. Basic auth over plain HTTP would expose the password.
6. Run **one** replica only: SQLite and the charge controller both assume a single instance.
7. A new deployment starts in **Dry-run** with empty settings. Re-enter the location (and array kW if known), then switch to Auto.

### Running more than one copy (control lease)

Only the copy holding the **control lease** sends automatic charge commands. Every other copy runs on standby: it shows everything and logs what it would do.

- **Latest start wins:** a copy claims control when it starts, or when you click **Take control** on its dashboard.
- **Renewal and failover:** the holder renews every minute. If it goes quiet for 5 minutes (laptop asleep), a standby copy takes over.
- **Handover:** a copy that takes over adopts the panel's current force-charge state, so it can turn off a charge another copy started.
- **The referee:** one copy (the VPS) stores the lease. Other copies must point at it:

| Where | Variable | Value |
|---|---|---|
| VPS (Coolify) | `INSTANCE_NAME` | `vps` |
| Laptop `.env` | `INSTANCE_NAME` | `laptop` |
| Laptop `.env` | `LEADER_URL` | `https://<your VPS domain>` |
| Laptop `.env` | `LEADER_PASSWORD` | the VPS `DASHBOARD_PASSWORD` (`LEADER_USER` defaults to `admin`) |

A copy that can't reach the referee stays on standby: it never acts on a lease it can't confirm.

**Caution:** a copy *without* `LEADER_URL` acts as its own referee. Two copies without it will **both** take control. Only the VPS should leave `LEADER_URL` unset.

**Backups:** use Coolify's volume backup, or run `sqlite3 /data/homepower.db ".backup /data/backup.db"` inside the container.
