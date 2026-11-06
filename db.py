"""SQLite history store: readings (one per poll), events (activity log), daily_savings, weather_hourly, lease.

Timestamps are unix seconds (UTC), so ranges sort correctly across daylight-saving changes.
"""
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts INTEGER PRIMARY KEY,      -- unix seconds
    soc REAL, solar_w REAL, load_w REAL, grid_w REAL,
    forecast_ghi REAL,           -- forecast irradiance W/m2 for that hour (for learning the array size)
    charging INTEGER             -- 1 while our grid force charge is on
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    msg TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_savings (   -- grid cost avoided per local day (see planner.savings)
    day TEXT PRIMARY KEY,        -- YYYY-MM-DD, Pacific time
    saved REAL NOT NULL,         -- dollars
    kwh REAL NOT NULL,           -- energy the house used that day
    updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS weather_hourly (   -- Open-Meteo, one row per hour; past hours hold the after-the-fact estimate
    hour_end INTEGER PRIMARY KEY, -- unix seconds at the end of the hour the values cover
    ghi REAL,                     -- total sunlight on flat ground, W/m2 (mean over the hour)
    direct REAL, diffuse REAL,    -- its direct-beam and diffuse (sky) parts, W/m2
    cloud REAL,                   -- cloud cover %
    temp REAL,                    -- air temperature C (hot panels make less power)
    fetched INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lease (   -- which copy of the app may send automatic commands (one row)
    id INTEGER PRIMARY KEY CHECK (id = 1),
    holder TEXT NOT NULL,
    renewed INTEGER NOT NULL
);
"""


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()  # poll thread and HTTP threads share one connection
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            # Columns added after the first release: add them to existing databases (old rows keep NULL).
            for table, cols in {"readings": ["solar1_w REAL", "solar2_w REAL", "soc1 REAL", "soc2 REAL", "remain_wh REAL"],
                                "daily_savings": ["solar1_kwh REAL", "solar2_kwh REAL", "cost_without REAL", "grid_cost REAL"]}.items():
                have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for col in cols:
                    if col.split()[0] not in have:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
            if self.conn.execute("PRAGMA user_version").fetchone()[0] < 1:
                # Solar before version 1 was read from the wrong panel field (~3 W): drop it so learning starts clean.
                self.conn.execute("UPDATE readings SET solar_w = NULL, solar1_w = NULL, solar2_w = NULL")
                self.conn.execute("PRAGMA user_version = 1")
            self.conn.commit()

    def _write(self, sql, args):
        with self.lock, self.conn:  # `with conn` commits, or rolls back on error
            self.conn.execute(sql, args)

    def add_reading(self, ts, soc, solar_w, load_w, grid_w, forecast_ghi, charging,
                    solar1_w=None, solar2_w=None, soc1=None, soc2=None, remain_wh=None):
        self._write("INSERT OR REPLACE INTO readings (ts, soc, solar_w, load_w, grid_w, forecast_ghi, charging,"
                    " solar1_w, solar2_w, soc1, soc2, remain_wh) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(ts), soc, solar_w, load_w, grid_w, forecast_ghi, int(bool(charging)), solar1_w, solar2_w, soc1, soc2, remain_wh))

    def stored_wh(self, since_ts, until_ts):
        """((first_ts, wh), (last_ts, wh)) of battery energy recorded in [since, until), or None if fewer than two
        readings have it. The panel reports it to the Wh; the % level is whole numbers only (1% = ~300 Wh here)."""
        with self.lock:
            rows = self.conn.execute("SELECT ts, remain_wh FROM readings WHERE ts >= ? AND ts < ? AND remain_wh IS NOT NULL ORDER BY ts",
                                     (int(since_ts), int(until_ts))).fetchall()
        return (rows[0], rows[-1]) if len(rows) >= 2 else None

    def save_weather(self, rows, now):
        """rows: [(hour_end_ts, ghi, direct, diffuse, cloud, temp)]; newer fetches replace older ones."""
        with self.lock, self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO weather_hourly VALUES (?,?,?,?,?,?,?)",
                                  [(int(r[0]), *r[1:], int(now)) for r in rows])

    def dpu_samples(self, since_ts):
        """[(ts, solar1_w, solar2_w, soc1, soc2, ghi)]: per-DPU solar matched to that hour's stored weather
        (falls back to the forecast captured with the reading)."""
        with self.lock:
            return self.conn.execute(
                "SELECT r.ts, r.solar1_w, r.solar2_w, r.soc1, r.soc2, COALESCE(w.ghi, r.forecast_ghi) FROM readings r"
                " LEFT JOIN weather_hourly w ON w.hour_end = r.ts - (r.ts % 3600) + 3600"
                " WHERE r.ts >= ? AND (r.solar1_w IS NOT NULL OR r.solar2_w IS NOT NULL)", (int(since_ts),)).fetchall()

    def solar_samples(self, since_ts):
        """[(solar_w, forecast_ghi, soc)] for daylight readings since `since_ts`."""
        with self.lock:
            return self.conn.execute(
                "SELECT solar_w, forecast_ghi, soc FROM readings WHERE ts >= ? AND forecast_ghi > 0 AND solar_w IS NOT NULL",
                (int(since_ts),)).fetchall()

    def add_event(self, ts, msg):
        self._write("INSERT INTO events (ts, msg) VALUES (?, ?)", (int(ts), msg))

    def recent_events(self, n=50):
        """[(ts, msg)] newest first."""
        with self.lock:
            return self.conn.execute("SELECT ts, msg FROM events ORDER BY id DESC LIMIT ?", (n,)).fetchall()

    def claim_lease(self, holder, now, ttl, force=False):
        """Claim, renew or check control. Returns the holder after this call.

        force=True takes control unconditionally (latest claim wins). Otherwise the caller
        gets it only if it already holds it or the current holder stopped renewing for `ttl` s.
        """
        with self.lock, self.conn:
            row = self.conn.execute("SELECT holder, renewed FROM lease WHERE id = 1").fetchone()
            if force or row is None or row[0] == holder or row[1] < now - ttl:
                self.conn.execute("INSERT OR REPLACE INTO lease VALUES (1, ?, ?)", (holder, int(now)))
                return holder
            return row[0]

    def peek_lease(self, now, ttl):
        """Current holder without claiming, or None if nobody renewed within `ttl` s."""
        with self.lock:
            row = self.conn.execute("SELECT holder, renewed FROM lease WHERE id = 1").fetchone()
        return row[0] if row and row[1] >= now - ttl else None

    def readings_since(self, since_ts, until_ts=2**62):
        """[(ts, soc, load_w, grid_w, solar_w, solar1_w, solar2_w)] in [since, until), oldest first."""
        with self.lock:
            return self.conn.execute("SELECT ts, soc, load_w, grid_w, solar_w, solar1_w, solar2_w FROM readings"
                                     " WHERE ts >= ? AND ts < ? ORDER BY ts", (int(since_ts), int(until_ts))).fetchall()

    def first_reading_ts(self):
        with self.lock:
            return self.conn.execute("SELECT MIN(ts) FROM readings").fetchone()[0]

    def save_daily_savings(self, by_day, now):
        """by_day: {day: [saved, home_kwh, solar1_kwh, solar2_kwh, cost_without, grid_cost]} (later values optional)."""
        with self.lock, self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO daily_savings (day, saved, kwh, updated, solar1_kwh, solar2_kwh, cost_without, grid_cost)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [(d, round(v[0], 4), round(v[1], 4), int(now), *(round(x, 4) for x in (list(v[2:6]) + [0] * 4)[:4]))
                 for d, v in by_day.items()])

    def daily_savings(self, since_day="0000-00-00"):
        """[(day, saved, home_kwh, solar1_kwh, solar2_kwh, cost_without, grid_cost)] oldest first."""
        with self.lock:
            return self.conn.execute("SELECT day, saved, kwh, solar1_kwh, solar2_kwh, cost_without, grid_cost FROM daily_savings"
                                     " WHERE day >= ? ORDER BY day", (since_day,)).fetchall()
