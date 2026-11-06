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
