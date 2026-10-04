"""Runs the cases in fixtures/video_rules.json against radio_classifier.video.rules. No case data lives here."""
import json
from pathlib import Path

import pytest

from radio_classifier.video import rules

CASES = json.loads((Path(__file__).parent / "fixtures" / "video_rules.json").read_text())
SIMPLE = ["match_sidecar", "parse_srt", "is_dialogue", "is_close", "language_from_tag", "decide_language",
          "coerce_summary", "check_vocab", "guess_old_root"]


def plain(value):
    """Tuples to lists, so results compare equal to JSON."""
    return json.loads(json.dumps(value))


@pytest.mark.parametrize("name,case", [(n, c) for n in SIMPLE for c in CASES[n]])
def test_simple(name, case):
    assert plain(getattr(rules, name)(*case["args"])) == case["expected"]


@pytest.mark.parametrize("case", CASES["vote"])
def test_vote(case):
    scores = rules.vote(*case["args"])
    assert rules.ranked(scores)[0][0] == case["winner"]
    assert {lang: scores[lang] for lang in case["scores"]} == case["scores"]
    if "close" in case:
        assert rules.is_close(scores) == case["close"]
