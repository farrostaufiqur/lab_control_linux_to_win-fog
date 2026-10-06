import json
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS pcs (
    id TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    ip TEXT, user TEXT, app TEXT, title TEXT,
    idle INTEGER, uptime INTEGER, mem INTEGER,
    locked INTEGER, version TEXT, exe_sha TEXT,
    override TEXT,
    alias TEXT,
    shot_ts REAL,                 -- waktu screenshot terakhir diterima
    shared_mode TEXT              -- aturan folder bersama: off|receive|send|both; NULL = ikut bawaan
);
CREATE TABLE IF NOT EXISTS shared_files (
    path TEXT PRIMARY KEY,        -- relatif, gaya POSIX
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,          -- waktu ubah file asli (untuk memutus konflik: yang terbaru menang)
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pc TEXT NOT NULL,
    eid TEXT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    detail TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS events_dedupe ON events(pc, eid) WHERE eid IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_pc_ts ON events(pc, ts);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pc TEXT NOT NULL,
    action TEXT NOT NULL,
    created REAL NOT NULL,
    delivered REAL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS renames (
    old TEXT PRIMARY KEY,         -- hostname Windows sekarang
    new TEXT NOT NULL,            -- hostname Windows yang diminta admin (huruf besar)
    restart INTEGER NOT NULL,     -- 1 = restart PC begitu nama berhasil diganti
    state TEXT NOT NULL,          -- pending | applied (menunggu restart) | failed
    error TEXT,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notices (
    pc TEXT PRIMARY KEY,          -- PC id, or '*' for a message to every PC
    text TEXT NOT NULL,
    created REAL NOT NULL,
    until REAL                    -- NULL = shown until the admin clears it
);
"""

PC_FIELDS = ("ip", "user", "app", "title", "idle", "uptime", "mem", "locked", "version", "exe_sha")


class Store:
    def __init__(self, path: str):
        self.path = path
        with self.tx() as db:
            db.executescript(SCHEMA)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(pcs)")}
            for column, kind in (("exe_sha", "TEXT"), ("alias", "TEXT"), ("shot_ts", "REAL"), ("shared_mode", "TEXT")):  # database lama
                if column not in columns:
                    db.execute(f"ALTER TABLE pcs ADD COLUMN {column} {kind}")
        self._last_prune = 0.0

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # settings -----------------------------------------------------------
    def get_setting(self, key, default=None):
        with self.tx() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        with self.tx() as db:
            db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    # pcs ----------------------------------------------------------------
    def record_report(self, pc_id: str, report: dict, events: list[dict], now: float):
        """Stores a heartbeat and its events. Returns (pc row before update or None)."""
        with self.tx() as db:
            before = db.execute("SELECT * FROM pcs WHERE id=?", (pc_id,)).fetchone()
            values = [report.get(field) for field in PC_FIELDS]
            if before is None:
                db.execute(
                    "INSERT INTO pcs(id, first_seen, last_seen, %s) VALUES(?, ?, ?, %s)"
                    % (", ".join(PC_FIELDS), ", ".join("?" * len(PC_FIELDS))),
                    [pc_id, now, now, *values],
                )
            else:
                assignments = ", ".join(f"{field}=?" for field in PC_FIELDS)
                db.execute(
                    f"UPDATE pcs SET last_seen=?, {assignments} WHERE id=?",
                    [now, *values, pc_id],
                )
            for event in events:
                db.execute(
                    "INSERT OR IGNORE INTO events(pc, eid, ts, type, detail) VALUES(?, ?, ?, ?, ?)",
                    (pc_id, event["eid"], event["ts"], event["type"], event["detail"]),
                )
        return before

    def set_build(self, pc_id: str, version, exe_sha) -> None:
        """Helper melapor build-nya sendiri (berguna saat belum ada user login); hanya untuk PC yang sudah dikenal."""
        with self.tx() as db:
            db.execute("UPDATE pcs SET version=?, exe_sha=? WHERE id=?", (version, exe_sha, pc_id))

    def set_alias(self, pc_id: str, alias: str | None) -> bool:
        with self.tx() as db:
            return db.execute("UPDATE pcs SET alias=? WHERE id=?", (alias, pc_id)).rowcount > 0

    def set_shot(self, pc_id: str, ts: float | None) -> bool:
        with self.tx() as db:
            return db.execute("UPDATE pcs SET shot_ts=? WHERE id=?", (ts, pc_id)).rowcount > 0

    def set_shared_mode(self, pc_id: str, mode: str | None) -> bool:
        with self.tx() as db:
            return db.execute("UPDATE pcs SET shared_mode=? WHERE id=?", (mode, pc_id)).rowcount > 0

    # folder bersama ---------------------------------------------------
    def shared_index(self) -> dict:
        with self.tx() as db:
            return {r["path"]: {"sha256": r["sha256"], "size": r["size"], "mtime": r["mtime"]}
                    for r in db.execute("SELECT * FROM shared_files ORDER BY path")}

    def shared_set(self, path: str, sha: str, size: int, mtime: float, now: float) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO shared_files(path, sha256, size, mtime, updated) VALUES(?, ?, ?, ?, ?) "
                       "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, size=excluded.size, "
                       "mtime=excluded.mtime, updated=excluded.updated", (path, sha, size, mtime, now))

    def shared_remove(self, path: str) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM shared_files WHERE path=?", (path,))

    def clear_shots(self) -> None:
        with self.tx() as db:
            db.execute("UPDATE pcs SET shot_ts=NULL")

    # ganti nama Windows -------------------------------------------------
    def request_rename(self, old: str, new: str, restart: bool, now: float):
        with self.tx() as db:
            db.execute(
                "INSERT INTO renames(old, new, restart, state, error, created) VALUES(?, ?, ?, 'pending', NULL, ?) "
                "ON CONFLICT(old) DO UPDATE SET new=excluded.new, restart=excluded.restart, state='pending', "
                "error=NULL, created=excluded.created",
                (old, new, int(restart), now))

    def cancel_rename(self, old: str):
        with self.tx() as db:
            db.execute("DELETE FROM renames WHERE old=?", (old,))

    def list_renames(self) -> dict:
        with self.tx() as db:
            return {row["old"]: dict(row) for row in db.execute("SELECT * FROM renames")}

    def rename_result(self, old: str, new: str, ok: bool, error: str) -> dict | None:
        """Hasil dari helper. Mengembalikan permintaan yang cocok (sebelum diubah) atau None."""
        with self.tx() as db:
            row = db.execute("SELECT * FROM renames WHERE old=? AND new=? AND state='pending'", (old, new)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE renames SET state=?, error=? WHERE old=?",
                       ("applied" if ok else "failed", None if ok else error, old))
            return dict(row)

    def migrate_renamed(self, new: str) -> str | None:
        """PC baru muncul dengan hostname hasil ganti nama: pindahkan nama tampilan, override, dan log dari yang lama."""
        with self.tx() as db:
            req = db.execute("SELECT * FROM renames WHERE lower(new)=lower(?) AND state='applied'", (new,)).fetchone()
            if req is None or req["old"] == new:
                return None
            old = req["old"]
            prev = db.execute("SELECT alias, override, shared_mode FROM pcs WHERE id=?", (old,)).fetchone()
            if prev is not None:
                db.execute("UPDATE pcs SET alias=?, override=?, shared_mode=? WHERE id=?",
                           (prev["alias"], prev["override"], prev["shared_mode"], new))
            db.execute("UPDATE OR IGNORE events SET pc=? WHERE pc=?", (new, old))
            db.execute("UPDATE OR IGNORE notices SET pc=? WHERE pc=?", (new, old))
            db.execute("DELETE FROM pcs WHERE id=?", (old,))
            db.execute("DELETE FROM commands WHERE pc=?", (old,))
            db.execute("DELETE FROM notices WHERE pc=?", (old,))
            db.execute("DELETE FROM renames WHERE old=?", (old,))
            return old

    def add_event(self, pc_id: str, type_: str, detail: str, ts: float | None = None):
        with self.tx() as db:
            db.execute(
                "INSERT INTO events(pc, ts, type, detail) VALUES(?, ?, ?, ?)",
                (pc_id, ts or time.time(), type_, detail),
            )

    def get_pc(self, pc_id: str):
        with self.tx() as db:
            return db.execute("SELECT * FROM pcs WHERE id=?", (pc_id,)).fetchone()

    def list_pcs(self):
        with self.tx() as db:
            return db.execute("SELECT * FROM pcs ORDER BY id COLLATE NOCASE").fetchall()

    def set_override(self, pc_id: str, override: dict | None) -> bool:
        with self.tx() as db:
            cur = db.execute(
                "UPDATE pcs SET override=? WHERE id=?",
                (json.dumps(override) if override else None, pc_id),
            )
            return cur.rowcount > 0

    def delete_pc(self, pc_id: str):
        with self.tx() as db:
            db.execute("DELETE FROM pcs WHERE id=?", (pc_id,))
            db.execute("DELETE FROM commands WHERE pc=?", (pc_id,))
            db.execute("DELETE FROM notices WHERE pc=?", (pc_id,))
            db.execute("DELETE FROM renames WHERE old=?", (pc_id,))

    # commands -----------------------------------------------------------
    def queue_command(self, pc_id: str, action: str, now: float):
        with self.tx() as db:
            db.execute(
                "INSERT INTO commands(pc, action, created) VALUES(?, ?, ?)", (pc_id, action, now)
            )

    def take_commands(self, pc_id: str, oldest_allowed: float, now: float) -> list[str]:
        """Each command is handed out at most once, so a reboot can never replay it."""
        with self.tx() as db:
            rows = db.execute(
                "SELECT id, action FROM commands WHERE pc=? AND delivered IS NULL AND created>=? "
                "ORDER BY id",
                (pc_id, oldest_allowed),
            ).fetchall()
            for row in rows:
                db.execute("UPDATE commands SET delivered=? WHERE id=?", (now, row["id"]))
            db.execute("DELETE FROM commands WHERE created<?", (now - 86400,))
        return [row["action"] for row in rows]

    # notices ------------------------------------------------------------
    def set_notice(self, pc_id: str, text: str, until: float | None, now: float):
        with self.tx() as db:
            db.execute(
                "INSERT INTO notices(pc, text, created, until) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(pc) DO UPDATE SET text=excluded.text, created=excluded.created, until=excluded.until",
                (pc_id, text, now, until),
            )

    def clear_notice(self, pc_id: str):
        with self.tx() as db:
            db.execute("DELETE FROM notices WHERE pc=?", (pc_id,))

    def get_notice(self, pc_id: str, now: float) -> str:
        """The message for this PC: its own if still active, otherwise the one for every PC."""
        with self.tx() as db:
            rows = db.execute(
                "SELECT pc, text FROM notices WHERE pc IN (?, '*') AND (until IS NULL OR until>?)",
                (pc_id, now),
            ).fetchall()
        by_pc = {row["pc"]: row["text"] for row in rows}
        return by_pc.get(pc_id) or by_pc.get("*") or ""

    # events -------------------------------------------------------------
    def list_events(self, pc_id: str | None, limit: int, before_id: int | None):
        query, args = "SELECT * FROM events WHERE 1=1", []
        if pc_id:
            query += " AND pc=?"
            args.append(pc_id)
        if before_id:
            query += " AND id<?"
            args.append(before_id)
        query += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        with self.tx() as db:
            return db.execute(query, args).fetchall()

    def prune(self, retention_days: int, now: float):
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        with self.tx() as db:
            db.execute("DELETE FROM events WHERE ts<?", (now - retention_days * 86400,))
