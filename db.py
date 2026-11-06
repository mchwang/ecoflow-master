"""SQLite history store: one row per poll (readings) plus the activity log (events).

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

    def _write(self, sql, args):
        with self.lock, self.conn:  # `with conn` commits, or rolls back on error
            self.conn.execute(sql, args)

    def add_reading(self, ts, soc, solar_w, load_w, grid_w, forecast_ghi, charging):
        self._write("INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?,?)",
                    (int(ts), soc, solar_w, load_w, grid_w, forecast_ghi, int(bool(charging))))

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
