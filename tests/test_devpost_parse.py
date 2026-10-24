"""Fixtures mirror real Devpost markup (Hypermode KG challenge, AWS AI Agent, AWS MCP Agents,
Cite Smart AI), trimmed to the elements the parsers read."""

from datetime import date
from pathlib import Path

from hackathon_index.money import find_amounts
from hackathon_index.sources.devpost import parse

FIX = Path(__file__).parent / "fixtures"


def test_parse_period():
    assert parse.parse_period("Apr 05 - 06, 2024") == (date(2024, 4, 5), date(2024, 4, 6))
    assert parse.parse_period("Dec 16, 2024 - Jan 12, 2025") == (date(2024, 12, 16), date(2025, 1, 12))
    assert parse.parse_period("May 30, 2025") == (date(2025, 5, 30), date(2025, 5, 30))
    assert parse.parse_period("Sep 29 - Oct 15, 2026") == (date(2026, 9, 29), date(2026, 10, 15))


def test_listing_item():
    row = parse.parse_listing_item({
        "id": 31481, "title": "Multimodal AI Hackathon 2026",
        "url": "https://multimodal-ai-hackathon-2026-7.devpost.com/?ref=x",
        "displayed_location": {"icon": "globe", "location": "Online"},
        "submission_period_dates": "Sep 29 - Oct 15, 2026",
        "themes": [{"id": 6, "name": "Machine Learning/AI"}],
        "prize_amount": '<span data-currency-value>₹100,000</span>',
        "prizes_counts": {"cash": 8, "other": 0}, "registrations_count": 4351,
        "organization_name": "Kamand Prompt, IIT Mandi", "winners_announced": False,
        "invite_only": False, "managed_by_devpost_badge": False,
    })
    assert row["url"] == "https://multimodal-ai-hackathon-2026-7.devpost.com/"
    assert row["headline_currency"] == "INR" and row["headline_prize_usd"] == 1200.0
    assert row["end_date"] == "2026-10-15" and row["is_online"] == 1
    assert row["themes"] == ["Machine Learning/AI"]


def test_amounts_flag_credits():
    found = find_amounts("First: $10k in AWS credits & $2,000 Second place $1,500")
    assert found == [(10000.0, "USD", True), (2000.0, "USD", False), (1500.0, "USD", False)]


def test_overview():
    d = parse.parse_overview((FIX / "overview.html").read_text())
    p = d["prizes"]
    assert [x["title"] for x in p] == ["1st Place", "Best Use of Neo4j", "Top Overall Winners", "Best Use of Auth0"]
    assert p[0]["cash_labeled"] == 1 and p[0]["value_amount"] == 16000 and p[0]["source_prize_id"] == "89797"
    assert p[2]["winners_count"] == 3
    # 16,000 labelled + 1,000 PayPal + (2,000 + 1,500 + 1,000) cash, credits and sunglasses excluded
    assert d["cash_usd_est"] == 21500 and d["cash_confidence"] == "low"
    assert d["judges"][1] == {"name": "Michael Hunger", "title": "Head of Product Innovation, Neo4j"}


def test_gallery():
    d = parse.parse_gallery((FIX / "gallery.html").read_text())
    winner, loser = d["entries"]
    assert winner["source_id"] == "833025" and winner["is_winner"] == 1
    assert winner["likes"] == 8 and winner["comments"] == 2 and len(winner["members"]) == 2
    assert loser["url"] == "https://devpost.com/software/some-loser" and loser["is_winner"] == 0
    assert d["has_next"] is True


def test_project():
    d = parse.parse_project((FIX / "project.html").read_text())
    assert d["title"] == "Cite Smart AI" and d["likes"] == 5
    assert d["built_with"] == ["neo4j", "nextjs"]
    assert d["has_repo"] == 1 and d["has_video"] == 1 and d["image_count"] == 2
    assert list(d["writeup"]) == ["Inspiration", "What it does", "How we built it"]
    assert "Graph view" in d["writeup"]["What it does"]
    assert d["members"] == ["https://devpost.com/m-azzam-azis"]
    hyper, azure = d["submitted_to"]
    assert hyper["hackathon_url"] == "https://hypermode-knowledge-graph-ai.devpost.com/"
    assert hyper["prizes"] == ["Best Use of Neo4j"] and azure["prizes"] == []


def test_project_without_template_headings():
    html = """<div id="app-details-left">
      <div id="gallery"><ul><li><p><i>Splash screen</i></p></li></ul></div>
      <div><p>Reconix is an AI-powered pentesting browser.</p><p>Built in Electron.</p></div>
      <div id="built-with"><h2>Built With</h2><ul><li><span class="cp-tag">electron</span></li></ul></div>
    </div>"""
    d = parse.parse_project(html)
    assert list(d["writeup"]) == ["Description"]
    assert "Splash screen" not in d["writeup"]["Description"]
    assert d["writeup"]["Description"].endswith("Built in Electron.")


def test_project_with_text_in_headings():
    long = "What it does: a 3D trivia map where every question is about the place you are travelling to."
    html = f"""<div id="app-details-left"><div>
      <h2>Inspiration Trivia, quest, risk</h2><h2>{long}</h2></div></div>"""
    d = parse.parse_project(html)
    assert list(d["writeup"]) == ["Inspiration Trivia, quest, risk", "Description"]
    assert d["writeup"]["Description"] == long
    only_short = parse.parse_project('<div id="app-details-left"><h2>Inspiration fun</h2><h2>Built fast</h2></div>')
    assert only_short["writeup"] == {"Description": "Inspiration fun\nBuilt fast"}


def test_video_urls():
    assert parse.video_url("https://www.youtube.com/embed/gz_HUX_mEYY?enablejsapi=1") == \
        "https://www.youtube.com/watch?v=gz_HUX_mEYY"
    assert parse.video_url("https://youtu.be/abc12345") == "https://www.youtube.com/watch?v=abc12345"
    assert parse.video_url("https://player.vimeo.com/video/123456") == "https://vimeo.com/123456"
    assert parse.video_url("https://github.com/x/y") is None
    d = parse.parse_project((FIX / "project.html").read_text())
    assert d["video_urls"] == [] or all("watch" in v or "vimeo" in v for v in d["video_urls"])
