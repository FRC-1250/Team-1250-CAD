"""SQLite state. Schema per drawing-export-spec.md section 5.

Design notes that matter:
  - `export` is keyed separately from `drawing_state` so a translation failure
    retries without losing the observation.
  - Export rows are written PENDING *before* the POST, so an interrupted run is
    recoverable without re-spending detection calls.
  - Cursors advance only on success.
  - call_log exists because Onshape exposes usage only in company settings,
    never via the API, and the 2,500/yr pool is shared company-wide.
"""

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS drawing_state (
    source_id      TEXT NOT NULL,
    source_kind    TEXT NOT NULL,
    element_id     TEXT NOT NULL,
    document_id    TEXT NOT NULL,
    document_key   TEXT,
    document_name  TEXT,
    element_name   TEXT,
    identifier     TEXT,
    version_id     TEXT,
    version_name   TEXT,
    microversion   TEXT,
    configuration  TEXT,
    -- Attribution. Comes from BTVersionInfo.creator, which Stage 1 already
    -- fetches, so it costs nothing. CAVEAT: this is who created the VERSION,
    -- not necessarily who authored the drawing -- the elements response carries
    -- no creator field, and element metadata would cost a call per document.
    -- If Alice draws and Bob versions, this says Bob.
    creator_id     TEXT,
    creator_name   TEXT,
    observed_at    TEXT NOT NULL,
    PRIMARY KEY (element_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_ds_document ON drawing_state(document_id);
CREATE INDEX IF NOT EXISTS idx_ds_ident    ON drawing_state(identifier);

CREATE TABLE IF NOT EXISTS export (
    element_id     TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    format         TEXT NOT NULL DEFAULT 'PDF',
    status         TEXT NOT NULL,
    translation_id TEXT,
    output_path    TEXT,
    sha256         TEXT,
    byte_size      INTEGER,
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    started_at     TEXT,
    completed_at   TEXT,
    PRIMARY KEY (element_id, source_id, format)
);
CREATE INDEX IF NOT EXISTS idx_export_status ON export(status);

CREATE TABLE IF NOT EXISTS publish (
    element_id   TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    format       TEXT NOT NULL DEFAULT 'PDF',
    status       TEXT NOT NULL,
    identifier   TEXT,
    repo_path    TEXT,
    commit_sha   TEXT,
    blob_url     TEXT,
    issue_number INTEGER,
    comment_url  TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    published_at TEXT,
    PRIMARY KEY (element_id, source_id, format)
);
CREATE INDEX IF NOT EXISTS idx_publish_status ON publish(status);

CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS call_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    status   INTEGER NOT NULL,
    counted  INTEGER NOT NULL,
    at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calllog_at ON call_log(at);

CREATE TABLE IF NOT EXISTS skip_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT NOT NULL,
    stage    TEXT NOT NULL,
    subject  TEXT NOT NULL,
    reason   TEXT NOT NULL,
    at       TEXT NOT NULL
);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` silently
# does nothing on an existing database, so new columns must be migrated in
# explicitly or every upgrade crashes on a missing column.
MIGRATIONS = [
    ("drawing_state", "creator_id", "TEXT"),
    ("drawing_state", "creator_name", "TEXT"),
]


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self):
        for table, col, decl in MIGRATIONS:
            have = {r["name"] for r in self.db.execute("PRAGMA table_info({})".format(table))}
            if not have:
                continue  # table doesn't exist yet; SCHEMA just created it fresh
            if col not in have:
                self.db.execute(
                    "ALTER TABLE {} ADD COLUMN {} {}".format(table, col, decl)
                )

    def close(self):
        self.db.close()

    # -- cursors ---------------------------------------------------------

    def get_state(self, key):
        r = self.db.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_state(self, key, value):
        self.db.execute(
            "INSERT INTO sync_state(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), now()),
        )
        self.db.commit()

    # -- change detection ------------------------------------------------

    def last_exported_microversion(self, element_id):
        """Microversion of the last DONE export for this drawing, or None.

        MUST default rather than subscript: a drawing exporting for the first
        time (incl. one just renamed into convention) has no row at all.
        Returning None makes `mv != last` true, so it exports. See spec 9.
        """
        r = self.db.execute(
            "SELECT ds.microversion FROM export e "
            "JOIN drawing_state ds ON ds.element_id=e.element_id AND ds.source_id=e.source_id "
            "WHERE e.element_id=? AND e.status='DONE' "
            "ORDER BY e.completed_at DESC LIMIT 1",
            (element_id,),
        ).fetchone()
        return r["microversion"] if r else None

    def record_drawing_state(self, **kw):
        cols = ",".join(kw)
        marks = ",".join("?" * len(kw))
        self.db.execute(
            "INSERT OR REPLACE INTO drawing_state({}) VALUES({})".format(cols, marks),
            tuple(kw.values()),
        )
        self.db.commit()

    # -- exports ---------------------------------------------------------

    def begin_export(self, element_id, source_id, fmt="PDF"):
        """Write PENDING *before* the POST so an interrupted run is recoverable."""
        self.db.execute(
            "INSERT INTO export(element_id,source_id,format,status,attempts,started_at) "
            "VALUES(?,?,?,'PENDING',1,?) "
            "ON CONFLICT(element_id,source_id,format) DO UPDATE SET "
            "status='PENDING', attempts=export.attempts+1, started_at=excluded.started_at",
            (element_id, source_id, fmt, now()),
        )
        self.db.commit()

    def finish_export(self, element_id, source_id, fmt, status, **kw):
        sets = ", ".join("{}=?".format(k) for k in kw)
        params = list(kw.values()) + [status, now(), element_id, source_id, fmt]
        self.db.execute(
            "UPDATE export SET {}{}status=?, completed_at=? "
            "WHERE element_id=? AND source_id=? AND format=?".format(sets, ", " if sets else ""),
            params,
        )
        self.db.commit()

    def already_exported(self, element_id, source_id, fmt="PDF"):
        r = self.db.execute(
            "SELECT 1 FROM export WHERE element_id=? AND source_id=? AND format=? AND status='DONE'",
            (element_id, source_id, fmt),
        ).fetchone()
        return r is not None

    # -- accounting ------------------------------------------------------

    def log_call(self, run_id, endpoint, status):
        counted = 1 if 200 <= status < 400 else 0
        self.db.execute(
            "INSERT INTO call_log(run_id,endpoint,status,counted,at) VALUES(?,?,?,?,?)",
            (run_id, endpoint, status, counted, now()),
        )
        self.db.commit()
        return counted

    def calls_counted(self, since_iso=None):
        if since_iso:
            r = self.db.execute(
                "SELECT COALESCE(SUM(counted),0) n FROM call_log WHERE at>=?", (since_iso,)
            ).fetchone()
        else:
            r = self.db.execute("SELECT COALESCE(SUM(counted),0) n FROM call_log").fetchone()
        return r["n"]

    def log_skip(self, run_id, stage, subject, reason):
        """Every skip is recorded. A silently skipped drawing is this tool's
        worst failure -- 'nothing exported' must never look like 'nothing changed'."""
        self.db.execute(
            "INSERT INTO skip_log(run_id,stage,subject,reason,at) VALUES(?,?,?,?,?)",
            (run_id, stage, subject, reason, now()),
        )
        self.db.commit()

    def skips_for_run(self, run_id):
        return self.db.execute(
            "SELECT stage,subject,reason FROM skip_log WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
