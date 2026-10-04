import os
import re
import subprocess
import tempfile
import time

import mlx.core as mx

from . import db, excludes

DEFAULT_MODEL = "mlx-community/parakeet-tdt-0.6b-v2"

# --workday: be gentle while people are awake, brisk-but-quiet overnight.
WORKDAY_START, WORKDAY_END = 7, 23  # local hours
NIGHT_PAUSE = 8.0  # seconds between files overnight, keeps fans civil

# First match decides: is this a part/episode beyond the first? Those get
# transcribed last, because announcer intros and opening scenes live in part 1.
LATER_PART_PATTERNS = [
    re.compile(r"(\d+)\s*of\s*\d+"),
    re.compile(r"\bs\d{1,2}[ ._-]*e0*(\d+)", re.I),
    re.compile(r"\b(?:ep|episode)[ ._-]*0*(\d+)", re.I),
    re.compile(r"\bpart[ ._-]*0*(\d+)", re.I),
    re.compile(r"^0*(\d{1,3})[ ._-]"),
]


def is_later_part(basename):
    s = basename.lower()
    for pat in LATER_PART_PATTERNS:
        m = pat.search(s)
        if m:
            return int(m.group(1)) > 1
    return False


def clip_to_wav(src, seconds, wav_path, force_format=None):
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    if force_format:
        cmd += ["-f", force_format]
    cmd += ["-i", src]
    if seconds > 0:
        cmd += ["-t", str(seconds)]
    cmd += ["-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", wav_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # Some MP3s carry a RIFF header ("invalid start code ID3 in RIFF header"); naming the format works.
        if not force_format and src.lower().endswith(".mp3"):
            return clip_to_wav(src, seconds, wav_path, force_format="mp3")
        raise RuntimeError(f"ffmpeg: {proc.stderr.strip()[:300]}")


def pick_candidates(con, verdicts, seconds, deepen, exclude=()):
    excluded = excludes.matcher(con, exclude)
    rows = con.execute(
        """
        SELECT f.id, f.path, f.duration, f.score, f.artist, f.album, f.title,
               t.seconds AS done_seconds
        FROM files f
        LEFT JOIN transcripts t ON t.file_id = f.id
        WHERE f.scan_error IS NULL
          AND f.verdict IN (%s)
        """
        % ",".join("?" * len(verdicts)),
        verdicts,
    ).fetchall()

    def wanted(row):
        if excluded(row):
            return False
        if row["done_seconds"] is None:
            return True
        if not deepen:
            return False
        done = row["done_seconds"]
        want = seconds if seconds > 0 else (row["duration"] or 1e9)
        # A second's slack for rounding; a wider margin left 181-210s files stuck at 180s.
        return done > 0 and done < want and (row["duration"] or 0) > done + 1

    rows = [r for r in rows if wanted(r)]
    rows.sort(
        key=lambda r: (
            is_later_part(os.path.basename(r["path"])),
            -(r["score"] or 0),
            -(r["duration"] or 0),
        )
    )
    return rows


def make_asr(model):
    """Returns wav -> (text, [(start, end, sentence), ...])."""
    from parakeet_mlx import DecodingConfig, SentenceConfig, from_pretrained

    pk = from_pretrained(model)
    # Unpunctuated monologues otherwise become one 145-second "sentence". max_words (~10-12s of
    # speech) rather than max_duration, which splits mid-word ("untut" / "ored").
    decoding = DecodingConfig(sentence=SentenceConfig(max_words=30))

    def run(wav):
        # Unchunked, a 28-minute file asks Metal for 29 GB and fails; 180s chunks peak under 4 GB.
        result = pk.transcribe(wav, chunk_duration=180, decoding_config=decoding)
        return result.text.strip(), [(s.start, s.end, s.text.strip()) for s in result.sentences]

    return run


def in_workday_hours():
    return WORKDAY_START <= time.localtime().tm_hour < WORKDAY_END


def transcribe(con, seconds=180, limit=None, model=DEFAULT_MODEL,
               verdicts=("likely",), deepen=False, exclude=(),
               pause=0.0, gentle=False, workday=False):
    if gentle and not workday:
        mx.set_default_device(mx.cpu)

    candidates = pick_candidates(con, list(verdicts), seconds, deepen, exclude)
    if limit:
        candidates = candidates[:limit]
    if not candidates:
        print("Nothing to transcribe.")
        return 0

    window = "whole file" if seconds <= 0 else f"first {seconds}s"
    if workday:
        mode = f" (workday: CPU {WORKDAY_START:02d}:00-{WORKDAY_END:02d}:00, GPU + {pause or NIGHT_PAUSE:.0f}s pause overnight)"
    elif gentle:
        mode = " (gentle: CPU-only)"
    else:
        mode = ""
    print(f"Transcribing {window} of {len(candidates)} files with {model}{mode}", flush=True)
    asr = make_asr(model)
    daytime = None

    done = failed = 0
    started = time.time()
    for i, row in enumerate(candidates, 1):
        if workday:
            now_day = in_workday_hours()
            if now_day != daytime:
                mx.set_default_device(mx.cpu if now_day else mx.gpu)
                label = "daytime CPU mode" if now_day else "overnight GPU mode"
                print(f"-- {time.strftime('%H:%M')}: {label}", flush=True)
                daytime = now_day
        name = os.path.basename(row["path"])
        t0 = time.time()
        fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            clip_to_wav(row["path"], seconds, wav_path)
            text, segments = asr(wav_path)
            actual = min(seconds, row["duration"]) if (seconds > 0 and row["duration"]) else (row["duration"] or seconds)
            con.execute(
                "INSERT OR REPLACE INTO transcripts (file_id, model, seconds, text, error) VALUES (?, ?, ?, ?, NULL)",
                (row["id"], model, actual, text),
            )
            db.index_transcript(con, row["id"], text, row["path"],
                                row["artist"], row["album"], row["title"])
            # Chunk joins can leave a sentence a fraction of a second out of order.
            db.save_segments(con, row["id"], sorted(segments))
            done += 1
            status = f"{time.time() - t0:5.1f}s"
        except Exception as exc:
            con.execute(
                "INSERT OR REPLACE INTO transcripts (file_id, model, seconds, text, error) VALUES (?, ?, 0, '', ?)",
                (row["id"], model, str(exc)[:500]),
            )
            db.save_segments(con, row["id"], [])
            failed += 1
            status = f"FAILED ({str(exc)[:80]})"
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass
            # MLX keeps freed buffers for reuse; varied clip lengths stop them matching, so without
            # this the cache grew to 11.6 GB over 80 files on a 24 GB Mac (2026-09-24).
            mx.clear_cache()
        con.commit()
        print(f"[{i}/{len(candidates)}] {status}  {name}", flush=True)
        pause_now = pause
        if workday:
            pause_now = 0.0 if daytime else (pause or NIGHT_PAUSE)
        if pause_now > 0 and i < len(candidates):
            time.sleep(pause_now)

    elapsed = time.time() - started
    print(
        f"Done: {done} transcribed, {failed} failed, "
        f"{elapsed / 60:.1f} min ({elapsed / max(1, done + failed):.1f}s/file).",
        flush=True,
    )
    return 0
