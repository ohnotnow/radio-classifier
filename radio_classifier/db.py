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

CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    text, path, artist, album, title,
    tokenize = 'porter unicode61'
);
"""


def connect(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def index_transcript(con, file_id, text, path, artist, album, title):
    con.execute("DELETE FROM search_index WHERE rowid = ?", (file_id,))
    con.execute(
        "INSERT INTO search_index (rowid, text, path, artist, album, title) VALUES (?, ?, ?, ?, ?, ?)",
        (file_id, text, path, artist or "", album or "", title or ""),
    )


def forget_file(con, file_id):
    con.execute("DELETE FROM search_index WHERE rowid = ?", (file_id,))
    con.execute("DELETE FROM transcripts WHERE file_id = ?", (file_id,))
