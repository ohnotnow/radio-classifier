import os
import re
import subprocess
import tempfile
import time

import mlx.core as mx

from . import db

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


def clip_to_wav(src, seconds, wav_path):
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src]
    if seconds > 0:
        cmd += ["-t", str(seconds)]
    cmd += ["-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", wav_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {proc.stderr.strip()[:300]}")


def pick_candidates(con, verdicts, seconds, deepen, exclude=()):
    exclude_rx = [re.compile(pat, re.I) for pat in exclude]
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
        if exclude_rx:
            haystack = " | ".join(
                str(v) for v in (row["path"], row["artist"], row["album"], row["title"]) if v
            )
            if any(rx.search(haystack) for rx in exclude_rx):
                return False
        if row["done_seconds"] is None:
            return True
        if not deepen:
            return False
        done = row["done_seconds"]
        want = seconds if seconds > 0 else (row["duration"] or 1e9)
        return done > 0 and done < want and (row["duration"] or 0) > done + 30

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
    if "parakeet" in model.lower():
        from parakeet_mlx import from_pretrained

        pk = from_pretrained(model)
        return lambda wav: pk.transcribe(wav).text.strip()

    import mlx_whisper

    return lambda wav: mlx_whisper.transcribe(
        wav,
        path_or_hf_repo=model,
        language="en",
        condition_on_previous_text=False,
        verbose=None,
    )["text"].strip()


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
            text = asr(wav_path)
            actual = min(seconds, row["duration"]) if (seconds > 0 and row["duration"]) else (row["duration"] or seconds)
            con.execute(
                "INSERT OR REPLACE INTO transcripts (file_id, model, seconds, text, error) VALUES (?, ?, ?, ?, NULL)",
                (row["id"], model, actual, text),
            )
            db.index_transcript(con, row["id"], text, row["path"],
                                row["artist"], row["album"], row["title"])
            done += 1
            status = f"{time.time() - t0:5.1f}s"
        except Exception as exc:
            con.execute(
                "INSERT OR REPLACE INTO transcripts (file_id, model, seconds, text, error) VALUES (?, ?, 0, '', ?)",
                (row["id"], model, str(exc)[:500]),
            )
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
