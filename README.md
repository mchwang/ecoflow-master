# Home Power: EcoFlow SHP2 + DPU dashboard

A local dashboard for an EcoFlow Smart Home Panel 2 with Delta Pro Ultra batteries. It also does automatic grid charging for **PG&E EV2-A**.

The automation works like this: it reads the hourly sunlight forecast from [Open-Meteo](https://open-meteo.com), which is free and needs no key. The forecast already reflects cloud cover and drops to zero after sunset. From it, the planner estimates how much solar will reach the batteries before 3 pm, when part-peak starts. If the predicted level at 3 pm is below **min %** (default 60), it turns on SHP2 **force charge** (`chNForceCharge`) just long enough to cover the shortfall. It never grid-charges during part-peak or peak. Set your latitude/longitude and array size on the dashboard; until then solar counts as 0.

```bash
cp .env.example .env      # add access key + secret key (panel SN is auto-detected)
python3 server.py         # http://127.0.0.1:8787  (demo data if .env is empty)
python3 -m unittest       # planner, signing, store and login tests
```

Modes:
- **Off:** does nothing.
- **Dry-run** (default): logs what it would do.
- **Auto:** sends the commands to the panel.

The "Force charge ON/OFF" buttons are manual overrides, and the automation won't undo them.
Settings are saved to `config.json`. The EV2-A prices in `planner.py` are the 2026-03-01 tariff; update them when PG&E changes rates.

## Data

`DATA_DIR` (default `./data`, `/data` in Docker) holds:
- `homepower.db` (SQLite):
  - `readings`: one row per minute with battery %, solar W, load W, grid W, forecast sunlight, and whether grid charging is on
  - `events`: the activity log
  - `daily_savings`: one row per day with dollars saved and kWh the system delivered to the house. This month is recalculated every minute from `readings`; earlier months stay as stored. `GET /api/savings` returns the history.
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
