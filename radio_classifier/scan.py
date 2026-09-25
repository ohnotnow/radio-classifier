import os
import sys
import time

from mutagen import File as MutagenFile

from . import db

AUDIO_EXTS = {
    ".mp3", ".m4a", ".m4b", ".m4p", ".ogg", ".opus",
    ".wav", ".aif", ".aiff", ".flac", ".wma",
}


def find_audio(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                yield os.path.join(dirpath, name)


def first_tag(mf, key):
    try:
        vals = mf.get(key)
    except Exception:
        return None
    if not vals:
        return None
    val = str(vals[0]).strip()
    return val or None


def read_metadata(path):
    """Returns (duration, bitrate, channels, artist, album, title, genre, error)."""
    try:
        mf = MutagenFile(path, easy=True)
    except Exception as exc:
        return None, None, None, None, None, None, None, f"{type(exc).__name__}: {exc}"
    if mf is None:
        return None, None, None, None, None, None, None, "unrecognised format"
    info = getattr(mf, "info", None)
    duration = getattr(info, "length", None)
    bitrate = getattr(info, "bitrate", None)
    channels = getattr(info, "channels", None)
    return (
        duration, bitrate, channels,
        first_tag(mf, "artist"), first_tag(mf, "album"),
        first_tag(mf, "title"), first_tag(mf, "genre"),
        None,
    )


def scan(con, root):
    root = os.path.abspath(root)
    existing = {
        row["path"]: (row["size"], row["mtime"], row["id"])
        for row in con.execute(
            "SELECT id, path, size, mtime FROM files WHERE path LIKE ?",
            (root + os.sep + "%",),
        )
    }

    print(f"Listing audio files under {root} ...", flush=True)
    paths = list(find_audio(root))
    print(f"Found {len(paths)} audio files; reading metadata ...", flush=True)

    added = updated = unchanged = errors = 0
    seen = set()
    started = time.time()
    for i, path in enumerate(paths, 1):
        seen.add(path)
        try:
            st = os.stat(path)
        except OSError:
            continue
        prior = existing.get(path)
        if prior and prior[0] == st.st_size and abs(prior[1] - st.st_mtime) < 1:
            unchanged += 1
        else:
            duration, bitrate, channels, artist, album, title, genre, error = read_metadata(path)
            if error:
                errors += 1
            con.execute(
                """
                INSERT INTO files (path, size, mtime, duration, bitrate, channels,
                                   artist, album, title, genre, scan_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size = excluded.size, mtime = excluded.mtime,
                    duration = excluded.duration, bitrate = excluded.bitrate,
                    channels = excluded.channels, artist = excluded.artist,
                    album = excluded.album, title = excluded.title,
                    genre = excluded.genre, scan_error = excluded.scan_error
                """,
                (path, st.st_size, st.st_mtime, duration, bitrate, channels,
                 artist, album, title, genre, error),
            )
            if prior:
                updated += 1
                db.forget_file(con, prior[2])  # file changed, transcript is stale
            else:
                added += 1
        if i % 2000 == 0:
            rate = i / (time.time() - started)
            print(f"  {i}/{len(paths)} ({rate:.0f} files/s)", flush=True)
        if i % 500 == 0:
            con.commit()

    pruned = 0
    for path, (_, _, file_id) in existing.items():
        if path not in seen:
            db.forget_file(con, file_id)
            con.execute("DELETE FROM files WHERE id = ?", (file_id,))
            pruned += 1
    con.commit()

    print(
        f"Scan complete in {time.time() - started:.0f}s: "
        f"{added} added, {updated} updated, {unchanged} unchanged, "
        f"{pruned} pruned, {errors} unreadable.",
        flush=True,
    )
    return 0
