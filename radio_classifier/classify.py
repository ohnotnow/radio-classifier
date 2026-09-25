import re

STRONG_KEYWORDS = [
    "bbc", "radio 4", "radio4", "radio 7", "radio7", "bbc7",
    "afternoon play", "afternoon drama", "saturday play", "saturday drama",
    "saturday night theatre", "classic serial", "book at bedtime",
    "radio drama", "old time radio", "otr", "radio 4 extra", "radio4extra",
]

MEDIUM_KEYWORDS = [
    "drama", "serial", "thriller", "mystery", "detective", "suspense",
    "sherlock", "agatha", "theatre", "theater", "play for today",
]

EPISODE_PATTERNS = [
    re.compile(r"\d+\s*of\s*\d+"),
    re.compile(r"\bs\d{1,2}\s*e\d{1,3}\b"),
    re.compile(r"\b(?:ep|episode)[ ._-]*\d+\b"),
    re.compile(r"\bpart[ ._-]*\d+\b"),
    re.compile(r"\b(19[2-9]\d|20[0-2]\d)[-._]\d{2}[-._]\d{2}\b"),
    re.compile(r"(?:^|/)\d{6}[ _-]"),
]

SPOKEN_GENRES = [
    "spoken", "speech", "audiobook", "audio book", "book", "radio",
    "drama", "comedy", "podcast", "humour", "humor",
]

MUSIC_GENRES = [
    "rock", "pop", "dance", "electronic", "electronica", "techno", "house",
    "trance", "ambient", "hip hop", "hip-hop", "rap", "metal", "punk",
    "indie", "alternative", "folk", "country", "blues", "jazz", "classical",
    "soundtrack", "new age", "reggae", "soul", "funk", "disco", "r&b", "rnb",
    "garage", "grunge", "psychedelic", "lo-fi", "chillout", "downtempo",
    "dub", "ska", "world", "latin", "opera", "choral",
]


def score_file(row):
    """Returns (score, verdict, reasons) for one files-table row."""
    if row["scan_error"]:
        return None, "error", "unreadable"

    text = " ".join(
        str(v).lower()
        for v in (row["path"], row["artist"], row["album"], row["title"])
        if v
    )
    genre = (row["genre"] or "").lower()
    duration = row["duration"]
    score = 0
    reasons = []

    for kw in STRONG_KEYWORDS:
        if kw in text:
            score += 3
            reasons.append(f"kw:{kw}")
            break
    for kw in MEDIUM_KEYWORDS:
        if kw in text:
            score += 2
            reasons.append(f"kw:{kw}")
            break
    if any(pat.search(text) for pat in EPISODE_PATTERNS):
        score += 2
        reasons.append("episodic")

    if genre:
        if any(g in genre for g in SPOKEN_GENRES):
            score += 2
            reasons.append(f"genre+:{genre[:20]}")
        if any(g in genre for g in MUSIC_GENRES):
            score -= 4
            reasons.append(f"genre-:{genre[:20]}")

    if duration:
        if duration >= 1500:
            score += 2
            reasons.append("dur:25m+")
        elif duration >= 600:
            score += 1
            reasons.append("dur:10m+")
        elif duration < 240:
            score -= 1
            reasons.append("dur:short")
        if row["bitrate"] and row["bitrate"] <= 96000 and duration >= 600:
            score += 1
            reasons.append("lowbit")
    if row["channels"] == 1:
        score += 1
        reasons.append("mono")

    if score >= 4:
        verdict = "likely"
    elif score >= 2:
        verdict = "maybe"
    else:
        verdict = "unlikely"
    return score, verdict, ",".join(reasons)


def classify(con):
    updates = []
    for row in con.execute("SELECT * FROM files"):
        score, verdict, reasons = score_file(row)
        updates.append((score, verdict, reasons, row["id"]))
    con.executemany(
        "UPDATE files SET score = ?, verdict = ?, reasons = ? WHERE id = ?", updates
    )
    con.commit()

    counts = dict(
        con.execute("SELECT verdict, COUNT(*) FROM files GROUP BY verdict").fetchall()
    )
    total = sum(counts.values())
    print(f"Classified {total} files:")
    for verdict in ("likely", "maybe", "unlikely", "error"):
        if verdict in counts:
            print(f"  {verdict:>8}: {counts[verdict]}")
    return 0
