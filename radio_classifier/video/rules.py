"""Small pure decisions for the video pipeline. Cases live in tests/fixtures/video_rules.json, so a port can
run the same ones. Evidence for every threshold here is in ant note racl-cwm6b."""
import os
import re

VIDEO_EXTS = {".mp4", ".m4v", ".mkv", ".avi", ".divx", ".flv", ".webm", ".mov", ".mpg", ".mpeg", ".wmv", ".ts"}
# Bitmap subtitle codecs (hdmv_pgs_subtitle, dvd_subtitle) are images, so they are left out.
TEXT_SUBTITLE_CODECS = {"subrip", "mov_text", "ass", "ssa", "webvtt", "text"}

SIDECAR_SUFFIXES = re.compile(r"((\.[a-z]{2,3}(-[a-z]{2})?)|(\.(forced|sdh|hi|cc)))+")
ENGLISH_SUFFIXES = (".en", ".eng")

# Real dialogue subtitles run 10 to 22 cues a minute; a sound-effects-only track ("PHONE RINGS") about 1.
DIALOGUE_CUES_PER_MINUTE = 4

# Function words per language, from the probe tested on 11 files (racl-cwm6b).
WORDS = {
    "en": "the and of to is that it you was for with have this but not what are they we he she",
    "nl": "de het een en van is dat niet ik je zijn wat er maar ook met op voor hij ze",
    "da": "og det er at en jeg ikke du har på til med som vi han hun der men så",
    "sv": "och det är att en jag inte du har på till med som vi han hon och men så",
    "de": "der die und das ist nicht ich du sie es ein eine mit auf zu den wir aber was",
    "fr": "le la les et est de un une je pas vous que il elle on ce qui avec pour mais",
    "it": "il la le di che non è un una io sono per con si mi ma cosa questo lo gli",
    "es": "el la los las y es de que no un una yo por con se me pero qué lo para",
}
WORDS = {lang: set(words.split()) for lang, words in WORDS.items()}

CUE_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d+)\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d+)")


def match_sidecar(video_path, srt_paths):
    """The .srt beside a video: same stem, or same stem plus language/flag suffixes (.en, .da, .forced).
    Prefers a plain match, then English, then anything else."""
    stem = os.path.splitext(os.path.basename(video_path))[0].lower()
    plain, english, other = [], [], []
    for srt in srt_paths:
        srt_stem = os.path.splitext(os.path.basename(srt))[0].lower()
        if srt_stem == stem:
            plain.append(srt)
        elif srt_stem.startswith(stem) and SIDECAR_SUFFIXES.fullmatch(srt_stem[len(stem):]):
            (english if srt_stem[len(stem):] in ENGLISH_SUFFIXES else other).append(srt)
    for found in (plain, english, other):
        if found:
            return found[0]
    return None


def parse_srt(text):
    """[(start, end, text)] per cue, with tags stripped and lines joined."""
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            m = CUE_TIME.search(line)
            if not m:
                continue
            g = [int(x) for x in m.groups()]
            start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 10 ** len(m.group(4))
            end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 10 ** len(m.group(8))
            words = re.sub(r"<[^>]+>", "", " ".join(lines[i + 1:]))
            words = re.sub(r"\s+", " ", words).strip()
            if words:
                cues.append((start, end, words))
            break
    return cues


def is_dialogue(cue_count, duration_seconds):
    if not duration_seconds:
        return False
    return cue_count / (duration_seconds / 60) >= DIALOGUE_CUES_PER_MINUTE


def vote(text):
    tokens = re.findall(r"[^\W\d_]+", text.lower())
    return {lang: sum(tok in words for tok in tokens) for lang, words in WORDS.items()}


def ranked(scores):
    return sorted(scores.items(), key=lambda kv: -kv[1])


def is_close(scores):
    """Top score under twice the runner-up (ties and all-zero count as close)."""
    top = ranked(scores)
    first = top[0][1] if top else 0
    second = top[1][1] if len(top) > 1 else 0
    return first == 0 or first < 2 * second


def language_from_tag(tag):
    """'en' for an English audio tag, the tag itself for any other language, None when it says nothing."""
    tag = (tag or "").strip().lower()
    if tag in ("", "und"):
        return None
    if tag in ("en", "eng"):
        return "en"
    return tag


def decide_language(window_scores):
    """English unless some probe window's winner is another language (then that window's winner)."""
    for scores in window_scores:
        winner = ranked(scores)[0][0]
        if winner != "en":
            return winner
    return "en"


def coerce_summary(value):
    """The model once returned `summary` as a list of sentences despite the prompt."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(str(v).strip() for v in value)
    return str(value)


def check_vocab(genres, subgenres, vocab):
    """(kept genres, kept subgenres, rejected values). vocab is genres.json: group -> subgenres."""
    groups = set(vocab)
    subs = {s for values in vocab.values() for s in values} - groups
    kept_genres = [g for g in genres if g in groups]
    kept_subs = [s for s in subgenres if s in subs]
    off_list = [g for g in genres if g not in groups] + [s for s in subgenres if s not in subs]
    return kept_genres, kept_subs, off_list


REMOUNT_SAMPLE = 20  # files looked at when a scan is given a root it has never seen
REMOUNT_MIN_MATCHES = 3


def guess_old_root(root, matches, sampled):
    """Where an apparently new share was scanned before. matches: (path under root, existing path) pairs for
    files whose name, size and mtime match an existing row; sampled: how many files were looked at.
    Returns (old root, files voting for it), or None unless at least half the sample (and 3) agree."""
    votes = {}
    for path, existing in matches:
        rel = os.path.relpath(path, root)
        suffix = os.sep + rel
        if existing.endswith(suffix) and existing[: -len(suffix)] != root:
            votes.setdefault(existing[: -len(suffix)], set()).add(path)
    if not votes:
        return None
    old_root, voters = max(votes.items(), key=lambda kv: len(kv[1]))
    if len(voters) >= REMOUNT_MIN_MATCHES and 2 * len(voters) >= sampled:
        return old_root, len(voters)
    return None
