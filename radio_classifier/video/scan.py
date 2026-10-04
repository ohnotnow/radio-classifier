import json
import os
import shlex
import subprocess
import time

from .. import db
from . import rules


def find_videos(root):
    """(video path, [.srt paths in the same directory]) for every video under root, skipping hidden folders."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        srts = [os.path.join(dirpath, n) for n in filenames if n.lower().endswith(".srt")]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in rules.VIDEO_EXTS:
                yield os.path.join(dirpath, name), srts


def probe(path):
    """Returns (duration, audio_language, subtitle_stream, error)."""
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration:stream=index,codec_type,codec_name:stream_tags=language", "-of", "json", path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return None, None, None, "ffprobe timed out"
    if proc.returncode != 0:
        return None, None, None, (proc.stderr.strip() or f"ffprobe exit {proc.returncode}")[:300]
    info = json.loads(proc.stdout or "{}")
    streams = info.get("streams", [])
    duration = info.get("format", {}).get("duration")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    language = (audio or {}).get("tags", {}).get("language")
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
    # N counts subtitle streams only, so the track can be pulled out with ffmpeg -map 0:s:N.
    stream = next((n for n, s in enumerate(subtitles) if s.get("codec_name") in rules.TEXT_SUBTITLE_CODECS), None)
    return float(duration) if duration else None, language, stream, None


UNDER = "substr(path, 1, length(?)) = ?"  # exact prefix: LIKE would treat _ and % in folder names as wildcards


def rows_under(con, root, columns="id, path, size, mtime"):
    prefix = root + os.sep
    return con.execute(f"SELECT {columns} FROM files WHERE {UNDER}", (prefix, prefix)).fetchall()


def remounted_from(con, root, found):
    """The old root, if these look like videos already scanned elsewhere (same name, size and mtime)."""
    sample = sorted(path for path, _ in found)[:rules.REMOUNT_SAMPLE]
    matches = []
    for path in sample:
        try:
            st = os.stat(path)
        except OSError:
            continue
        for row in con.execute("SELECT path, mtime FROM files WHERE path LIKE ? ESCAPE '\\' AND size = ?",
                               ("%" + os.sep + like_escape(os.path.basename(path)), st.st_size)):
            if abs(row["mtime"] - st.st_mtime) < 1:
                matches.append((path, row["path"]))
    return rules.guess_old_root(root, matches, len(sample))


def like_escape(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def scan(con, root, new_root=False):
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        print(f"Not a directory: {root}")
        return 2
    existing = {row["path"]: (row["size"], row["mtime"], row["id"]) for row in rows_under(con, root)}

    print(f"Listing videos under {root} ...", flush=True)
    found = list(find_videos(root))
    if not found and existing:
        # An unmounted share can leave an empty mountpoint behind: pruning would throw away every transcript.
        print(f"Found no videos, but the database has {len(existing)} under {root} (share not mounted?). "
              "Nothing pruned.")
        return 1
    if found and not existing and not new_root:
        # macOS mounts a share at /Volumes/ssd-1 when a stale /Volumes/ssd is still there: without this, the
        # whole collection would be added again with no transcripts.
        guess = remounted_from(con, root, found)
        if guess:
            old_root, n = guess
            sampled = min(len(found), rules.REMOUNT_SAMPLE)
            print(f"These look like videos already scanned from {old_root} ({n} of the first {sampled} match).\n"
                  "If your share has come back under a new name, keep everything already done with:\n"
                  f"    video remount {shlex.quote(old_root)} {shlex.quote(root)}\n"
                  "To scan this as a separate collection anyway, add --new-root.")
            return 1
    print(f"Found {len(found)} videos; probing new and changed files ...", flush=True)

    added = updated = unchanged = errors = 0
    seen = set()
    started = time.time()
    for i, (path, srts) in enumerate(found, 1):
        seen.add(path)
        try:
            st = os.stat(path)
        except OSError:
            continue
        prior = existing.get(path)
        if prior and prior[0] == st.st_size and abs(prior[1] - st.st_mtime) < 1:
            unchanged += 1
            file_id = prior[2]
        else:
            duration, language, stream, error = probe(path)
            if error:
                errors += 1
            con.execute(
                """
                INSERT INTO files (path, size, mtime, duration, scan_error) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size = excluded.size, mtime = excluded.mtime,
                    duration = excluded.duration, scan_error = excluded.scan_error
                """,
                (path, st.st_size, st.st_mtime, duration, error),
            )
            file_id = con.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()[0]
            if prior:
                updated += 1
                db.forget_file(con, file_id)  # file changed: transcript and summary are stale
                con.execute("DELETE FROM video_summaries WHERE file_id = ?", (file_id,))
            else:
                added += 1
            con.execute(
                """
                INSERT INTO video_files (file_id, audio_language, subtitle_stream, probe_error) VALUES (?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    audio_language = excluded.audio_language, subtitle_stream = excluded.subtitle_stream,
                    language = NULL, language_basis = NULL, probe_error = excluded.probe_error
                """,
                (file_id, language, stream, error),
            )
        # Every time: a sidecar can turn up after the video was first scanned.
        con.execute("UPDATE video_files SET sidecar_srt = ? WHERE file_id = ?",
                    (rules.match_sidecar(path, srts), file_id))
        if i % 2000 == 0:
            rate = i / (time.time() - started)
            print(f"  {i}/{len(found)} ({rate:.0f} files/s)", flush=True)
        if i % 500 == 0:
            con.commit()

    pruned = 0
    for path, (_, _, file_id) in existing.items():
        if path not in seen:
            db.forget_file(con, file_id)
            con.execute("DELETE FROM files WHERE id = ?", (file_id,))  # cascades to video_files, video_summaries
            pruned += 1
    con.commit()

    print(
        f"Scan complete in {time.time() - started:.0f}s: "
        f"{added} added, {updated} updated, {unchanged} unchanged, "
        f"{pruned} pruned, {errors} unreadable.",
        flush=True,
    )
    return 0


def remount(con, old, new):
    """Move stored paths from one mount point to another, keeping transcripts and summaries."""
    old, new = os.path.abspath(old), os.path.abspath(new)
    if not os.path.isdir(new):
        print(f"Not a directory: {new}")
        return 2
    rows = rows_under(con, old, "id, path")
    if not rows:
        print(f"Nothing stored under {old}.")
        return 2
    moved = [(new + row["path"][len(old):], row["id"]) for row in rows]
    for path, _ in moved:
        if con.execute("SELECT 1 FROM files WHERE path = ?", (path,)).fetchone():
            print(f"{path} is already in the database, so {new} has been scanned before. Nothing moved.")
            return 2
    with con:
        con.executemany("UPDATE files SET path = ? WHERE id = ?", moved)
        con.executemany("UPDATE search_index SET path = ? WHERE rowid = ?", moved)
        con.execute(
            "UPDATE video_files SET sidecar_srt = ? || substr(sidecar_srt, length(?) + 1) "
            f"WHERE {UNDER.replace('path', 'sidecar_srt')}",
            (new, old, old + os.sep, old + os.sep),
        )
    print(f"Moved {len(moved)} files from {old} to {new}.")
    return 0
