"""Whole-file text for each video: subtitles when they are real dialogue, else parakeet, v2 for English and
v3 for anything else. Evidence for each choice is in ant note racl-cwm6b."""
import os
import subprocess
import tempfile

import mlx.core as mx

from .. import excludes
from .. import transcribe as radio
from . import rules

PROBE_SECONDS = 120


def pick_candidates(con, exclude=()):
    """Scanned videos with no transcript row yet (a failure counts as done, as in radio without --deepen)."""
    excluded = excludes.matcher(con, exclude)
    rows = con.execute(
        """
        SELECT f.id, f.path, f.duration, f.artist, f.album, f.title,
               v.audio_language, v.sidecar_srt, v.subtitle_stream
        FROM files f
        JOIN video_files v ON v.file_id = f.id
        LEFT JOIN transcripts t ON t.file_id = f.id
        WHERE f.scan_error IS NULL AND t.file_id IS NULL
        ORDER BY f.path
        """
    ).fetchall()
    return [r for r in rows if not excluded(r)]


def read_text(path):
    try:
        return open(path, encoding="utf-8-sig").read()
    except UnicodeDecodeError:
        return open(path, encoding="latin-1").read()


def embedded_srt(path, stream):
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", f"0:s:{stream}", "-f", "srt", "pipe:1"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg subtitles: {proc.stderr.decode(errors='replace').strip()[:300]}")
    return proc.stdout.decode("utf-8", errors="replace")


def wav_of(path, seconds, asr, start=0):
    fd, wav = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        radio.clip_to_wav(path, seconds, wav, start=start)
        return asr(wav)
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass


def transcribe(con, limit=None, exclude=(), pause=0.0, gentle=False, workday=False):
    if gentle and not workday:
        mx.set_default_device(mx.cpu)

    candidates = pick_candidates(con, exclude)
    if limit:
        candidates = candidates[:limit]
    reachable = [r for r in candidates if os.path.exists(r["path"])]
    if len(reachable) < len(candidates):
        print(f"{len(candidates) - len(reachable)} files skipped: not reachable (share not mounted?)")
    if not reachable:
        print("Nothing to transcribe.")
        return 0
    print(f"Transcribing {len(reachable)} videos (subtitles where they hold the dialogue, "
          f"else {radio.DEFAULT_MODEL} for English and {radio.MULTILINGUAL_MODEL} for other languages)"
          f"{radio.mode_note(gentle, workday, pause)}", flush=True)

    models = {}

    def asr(model):
        # Loaded on first use, once per run: a run of English files never loads v3 beyond the probes.
        if model not in models:
            models[model] = radio.make_asr(model)
        return models[model]

    def decide(row):
        language = rules.language_from_tag(row["audio_language"])
        if language:
            return language, "tag"
        duration = row["duration"]
        # Mid-file, not the start: titles, continuity and silent openings (130s in Night Conspirators)
        # would fool a probe of the opening minutes.
        first = max(duration / 2 - PROBE_SECONDS / 2, 0) if duration else 300
        windows = [rules.vote(wav_of(row["path"], PROBE_SECONDS, asr(radio.MULTILINGUAL_MODEL), start=first)[0])]
        if rules.ranked(windows[0])[0][0] == "en" and rules.is_close(windows[0]) and duration:
            windows.append(rules.vote(
                wav_of(row["path"], PROBE_SECONDS, asr(radio.MULTILINGUAL_MODEL), start=duration / 3)[0]))
        return rules.decide_language(windows), "probe"

    def subtitles(row, text, label):
        cues = rules.parse_srt(text)
        if not rules.is_dialogue(len(cues), row["duration"]):
            return None
        con.execute("UPDATE video_files SET language = NULL, language_basis = 'subtitles' WHERE file_id = ?",
                    (row["id"],))
        return label, row["duration"], " ".join(c[2] for c in cues), cues

    def process(row):
        if row["sidecar_srt"] and os.path.exists(row["sidecar_srt"]):
            found = subtitles(row, read_text(row["sidecar_srt"]), "srt:sidecar")
            if found:
                return found
        if row["subtitle_stream"] is not None:
            # Some embedded tracks are only captions for sounds ("PHONE RINGS"); the density check drops those.
            try:
                found = subtitles(row, embedded_srt(row["path"], row["subtitle_stream"]), "srt:embedded")
            except RuntimeError as exc:
                print(f"  (embedded subtitles unreadable, using speech: {str(exc)[:80]})", flush=True)
                found = None
            if found:
                return found
        language, basis = decide(row)
        con.execute("UPDATE video_files SET language = ?, language_basis = ? WHERE file_id = ?",
                    (language, basis, row["id"]))
        model = radio.DEFAULT_MODEL if language == "en" else radio.MULTILINGUAL_MODEL
        text, segments = wav_of(row["path"], 0, asr(model))
        return model, row["duration"], text, segments

    return radio.run_loop(con, reachable, process, "video", pause=pause, workday=workday)
