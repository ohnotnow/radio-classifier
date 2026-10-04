from .. import db

# Video-only tables, created by video commands only, so radio commands never add them to radio.db.
# `files` rows for video use path, size, mtime, duration and scan_error; the audio-tag columns stay NULL.
VIDEO_SCHEMA = """
CREATE TABLE IF NOT EXISTS video_files (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    audio_language TEXT,      -- tag of the first audio stream as ffprobe reports it (e.g. 'fre', 'eng'), or NULL
    sidecar_srt TEXT,         -- path of the chosen sidecar .srt, or NULL
    subtitle_stream INTEGER,  -- N for ffmpeg -map 0:s:N: the first text-based subtitle stream, or NULL
    language TEXT,            -- 'en' or another code, decided by video transcribe, or NULL until then
    language_basis TEXT,      -- 'subtitles' | 'tag' | 'probe', or NULL
    probe_error TEXT          -- ffprobe failure text, or NULL
);

CREATE TABLE IF NOT EXISTS video_summaries (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    model TEXT,
    source TEXT,              -- copied from transcripts.model when summarised
    title TEXT,
    series TEXT,
    made_year INTEGER,
    made_year_basis TEXT,     -- 'path' | 'transcript' | 'guess', or NULL
    summary TEXT,
    genres TEXT,              -- JSON array
    subgenres TEXT,           -- JSON array
    places TEXT,              -- JSON array
    setting_era TEXT,
    characters TEXT,          -- JSON array of {name, description}
    set_pieces TEXT,          -- JSON array
    tone TEXT,
    ignored TEXT,
    off_list TEXT,            -- JSON array of genres/subgenres the model returned that are not in the vocabulary
    error TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


def connect_video(db_path):
    """db.connect, refusing a radio database: radio `classify` sets verdicts, video commands never do."""
    con = db.connect(db_path)
    if con.execute("SELECT COUNT(*) FROM files WHERE verdict IS NOT NULL").fetchone()[0]:
        con.close()
        raise SystemExit(f"{db_path} looks like a radio database (it has classified files); "
                         "video commands need their own, e.g. --db video.db")
    con.executescript(VIDEO_SCHEMA)
    return con
