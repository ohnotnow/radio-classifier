import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    size INTEGER,
    mtime REAL,
    duration REAL,
    bitrate INTEGER,
    channels INTEGER,
    artist TEXT,
    album TEXT,
    title TEXT,
    genre TEXT,
    scan_error TEXT,
    score INTEGER,
    verdict TEXT,
    reasons TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_verdict ON files(verdict);

CREATE TABLE IF NOT EXISTS transcripts (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    model TEXT,
    seconds REAL,
    text TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Sentence-level timings for a transcript, so a hit can say where in the file it is.
CREATE TABLE IF NOT EXISTS segments (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    start REAL NOT NULL,
    end REAL NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_file ON segments(file_id, start);

-- One row per story, written by `summarise`: made when its folder is grouped (title and kind from the
-- grouping call), filled in when described (model stays NULL until then). A folder can hold several
-- stories. cast_list is JSON [{actor, role}].
CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY,
    folder TEXT NOT NULL,
    kind TEXT,
    kind_reason TEXT,
    title TEXT,
    writer TEXT,
    cast_list TEXT,
    synopsis TEXT,
    model TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Copies of the same recording share an episode number.
CREATE TABLE IF NOT EXISTS story_files (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    episode INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_story_files_story ON story_files(story_id, episode);

-- Per-episode summaries: full of spoilers, so keep them out of search results and browsing.
CREATE TABLE IF NOT EXISTS summaries (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    model TEXT,
    text TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Regexes for things never to transcribe or summarise, matched against path and tags (excludes.py).
CREATE TABLE IF NOT EXISTS excludes (
    id INTEGER PRIMARY KEY,
    pattern TEXT UNIQUE NOT NULL,
    note TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    text, path, artist, album, title,
    tokenize = 'porter unicode61'
);
"""


# Columns added after databases already existed: (column, type), added in place by connect().
ADDED_FILE_COLUMNS = [
    ("missing_since", "TEXT"),  # when a scan last found the file gone; NULL while present (racl-9X77J)
    ("changed_at", "TEXT"),     # when a scan saw a transcribed file's size or mtime change
]


def connect(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    have = {r["name"] for r in con.execute("PRAGMA table_info(files)")}
    for column, kind in ADDED_FILE_COLUMNS:
        if column not in have:
            con.execute(f"ALTER TABLE files ADD COLUMN {column} {kind}")
    con.commit()
    return con


UNDER = "substr(path, 1, length(?)) = ?"  # exact prefix: LIKE would treat _ and % in folder names as wildcards


def rows_under(con, root, columns="id, path, size, mtime"):
    prefix = root.rstrip(os.sep) + os.sep
    return con.execute(f"SELECT {columns} FROM files WHERE {UNDER}", (prefix, prefix)).fetchall()


def has_transcript(con, file_id):
    return con.execute("SELECT 1 FROM transcripts WHERE file_id = ?", (file_id,)).fetchone() is not None


def record_presence(con, existing, seen):
    """After a scan: mark rows whose file wasn't found as missing, and clear the mark on ones found again.
    Nothing is ever deleted: transcripts and summaries are archive (ant racl-9X77J). Returns the
    number newly marked missing."""
    gone = [(file_id,) for path, file_id in existing.items() if path not in seen]
    back = [(file_id,) for path, file_id in existing.items() if path in seen]
    newly = sum(
        con.execute("UPDATE files SET missing_since = datetime('now') WHERE id = ? AND missing_since IS NULL",
                    row).rowcount
        for row in gone
    )
    con.executemany("UPDATE files SET missing_since = NULL WHERE id = ? AND missing_since IS NOT NULL", back)
    return newly


def index_transcript(con, file_id, text, path, artist, album, title):
    con.execute("DELETE FROM search_index WHERE rowid = ?", (file_id,))
    con.execute(
        "INSERT INTO search_index (rowid, text, path, artist, album, title) VALUES (?, ?, ?, ?, ?, ?)",
        (file_id, text, path, artist or "", album or "", title or ""),
    )


def save_segments(con, file_id, segments):
    con.execute("DELETE FROM segments WHERE file_id = ?", (file_id,))
    con.executemany(
        "INSERT INTO segments (file_id, start, end, text) VALUES (?, ?, ?, ?)",
        [(file_id, start, end, text) for start, end, text in segments],
    )
