"""Pure parsers for Devpost pages: HTML/JSON in, dicts out. No network, no DB."""

import re
from datetime import date, datetime
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ...money import find_amounts, to_usd

SOURCE = "devpost"
_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def _int(s) -> int | None:
    m = re.search(r"\d[\d,]*", str(s or ""))
    return int(m.group().replace(",", "")) if m else None


def hackathon_key(url: str) -> str:
    """Normalise a hackathon URL to its host, e.g. 'aws-agent-hackathon.devpost.com'."""
    return urlsplit(url if "//" in url else f"https://{url}").netloc.lower()


def base_url(url: str) -> str:
    return f"https://{hackathon_key(url)}/"


# ---------------------------------------------------------------- listing JSON

def parse_period(s: str) -> tuple[date | None, date | None]:
    """'Apr 05 - 06, 2024' | 'Dec 16, 2024 - Jan 12, 2025' | 'May 30, 2025' | 'Sep 29 - Oct 15, 2026'."""
    s = (s or "").replace("–", "-").strip()
    left, _, right = s.partition(" - ")
    right = right or left
    years = re.findall(r"\d{4}", s)
    if not years:
        return None, None
    end_year = years[-1]
    try:
        if not re.match(rf"({_MONTHS})", right):  # '06, 2024' -> borrow month from left
            right = f"{left.split()[0]} {right}"
        if not re.search(r"\d{4}", right):
            right = f"{right}, {end_year}"
        end = datetime.strptime(right.strip(), "%b %d, %Y").date()
        if not re.search(r"\d{4}", left):
            left = f"{left}, {end.year}"
        start = datetime.strptime(left.strip(), "%b %d, %Y").date()
        if start > end:  # 'Dec 28 - Jan 03, 2025' style
            start = start.replace(year=start.year - 1)
        return start, end
    except ValueError:
        return None, datetime(int(end_year), 12, 31).date()


def parse_listing_item(h: dict) -> dict:
    start, end = parse_period(h.get("submission_period_dates", ""))
    prize_text = BeautifulSoup(h.get("prize_amount") or "", "html.parser").get_text().strip()
    amounts = find_amounts(prize_text)
    amount, currency = (amounts[0][0], amounts[0][1]) if amounts else (None, None)
    loc = (h.get("displayed_location") or {})
    counts = h.get("prizes_counts") or {}
    return {
        "source": SOURCE,
        "source_id": str(h["id"]),
        "url": base_url(h["url"]),
        "title": h.get("title"),
        "organizer": h.get("organization_name"),
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
        "location": loc.get("location"),
        "is_online": int(loc.get("icon") == "globe"),
        "invite_only": int(bool(h.get("invite_only"))),
        "managed_by_source": int(bool(h.get("managed_by_devpost_badge"))),
        "winners_announced": int(bool(h.get("winners_announced"))),
        "themes": [t.get("name") for t in h.get("themes") or []],
        "registrants": h.get("registrations_count"),
        "headline_prize_text": prize_text,
        "headline_currency": currency,
        "headline_prize_usd": to_usd(amount, currency),
        "cash_prizes_count": counts.get("cash"),
        "other_prizes_count": counts.get("other"),
    }


# ---------------------------------------------------------------- overview page

def parse_overview(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    prizes = []
    for pos, el in enumerate(soup.select("#prizes .prize")):
        value_el = el.select_one(".prize-value")
        value_text = _text(value_el)
        amounts = find_amounts(value_text)
        desc_el = el.select_one(".prize-content p") or el.select_one("p")
        prizes.append({
            "source_prize_id": (el.get("id") or "").removeprefix("prize_") or None,
            "position": pos,
            "title": _text(el.select_one("h6")),
            "value_text": value_text or None,
            "value_amount": amounts[0][0] if amounts else None,
            "value_currency": amounts[0][1] if amounts else None,
            "cash_labeled": int("cash" in value_text.lower()),
            "winners_count": _int(_text(el.select_one(".prize-winners"))),
            "description": desc_el.get_text("\n", strip=True) if desc_el else None,
        })

    judges = []
    for el in soup.select("#judges .challenge_judge"):
        name = _text(el.select_one("strong"))
        if name:
            judges.append({"name": name, "title": _text(el.select_one("i")) or None})

    cash, confidence = estimate_cash(prizes)
    return {"prizes": prizes, "judges": judges, "cash_usd_est": cash, "cash_confidence": confidence,
            "sidebar": _text(soup.select_one("#challenge-information"))}


def estimate_cash(prizes: list[dict]) -> tuple[float, str]:
    """Rough cash total in USD. 'high' confidence only when every prize had an explicit
    '... in cash' value or no money at all; anything parsed from free text is 'low'."""
    total, confidence = 0.0, "high"
    for p in prizes:
        if p["cash_labeled"] and p["value_amount"] is not None:
            total += to_usd(p["value_amount"], p["value_currency"]) or 0
            continue
        found = find_amounts(f"{p['value_text'] or ''}\n{p['description'] or ''}")
        if found:
            confidence = "low"
            total += sum(to_usd(a, c) or 0 for a, c, non_cash in found if not non_cash)
    return round(total, 2), confidence


# ---------------------------------------------------------------- gallery page

def parse_gallery(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    for el in soup.select(".gallery-item"):
        link = el.select_one("a.link-to-software, a.block-wrapper-link")
        if not link or not link.get("href"):
            continue
        entries.append({
            "source_id": el.get("data-software-id"),
            "url": link["href"].split("?")[0],
            "title": _text(el.select_one("h5")),
            "tagline": _text(el.select_one(".tagline")) or None,
            "is_winner": int(el.select_one(".entry-badge .winner, img.winner") is not None),
            "likes": _int(_text(el.select_one(".like-count"))) or 0,
            "comments": _int(_text(el.select_one(".comment-count"))) or 0,
            "members": sorted({m["data-url"].split("?")[0] for m in el.select(".members [data-url]")}),
        })
    nxt = soup.select_one(".pagination .next_page")
    has_next = bool(nxt and "unavailable" not in (nxt.get("class") or []))
    # The gallery's "filter by prize" checkboxes; values match the prize blocks' ids.
    filter_ids = list(dict.fromkeys(i.get("value") for i in soup.select('input[name="prize_filter[prizes][]"]')
                                    if i.get("value")))
    return {"entries": entries, "has_next": has_next, "prize_filter_ids": filter_ids}


# ---------------------------------------------------------------- project page

def video_url(url: str) -> str | None:
    """A YouTube/Vimeo embed or link as a plain watch URL; None for anything else."""
    m = re.search(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=|shorts/)|youtu\.be/)([\w-]{6,})", url or "")
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    m = re.search(r"vimeo\.com/(?:video/)?(\d+)", url or "")
    return f"https://vimeo.com/{m.group(1)}" if m else None


_SKIP_SECTIONS = {"Built With", "Try it out"}


def parse_project(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    submitted = []
    for li in soup.select("#submissions ul.software-list-with-thumbnail > li"):
        a = li.select_one(".software-list-content a[href]") or li.select_one("a[href]")
        if not a:
            continue
        prizes = []
        for w in li.select(".software-list-content li"):
            if w.select_one(".winner"):
                prizes.append(_text(w).removeprefix("Winner").strip() or "Winner")
        submitted.append({"hackathon_url": base_url(a["href"]), "hackathon_title": _text(a),
                          "prizes": prizes})

    writeup, current = {}, None
    details = soup.select_one("#app-details-left")
    if details:
        for el in details.find_all(["h2", "p", "ul", "ol", "pre", "blockquote", "h1", "h3", "h4", "h5", "h6"]):
            if el.find_parent(id=["built-with", "gallery"]) or el.find_parent(class_="app-links") \
                    or el.get("id") == "gallery":
                continue
            if el.name == "h2":
                current = _text(el)
                if current not in _SKIP_SECTIONS:
                    writeup.setdefault(current, "")
            elif current not in _SKIP_SECTIONS and el.find_parent(["ul", "ol"]) is None:
                # Writeups without the template headings go under "Description".
                key = current or "Description"
                writeup[key] = (writeup.get(key, "") + "\n" + el.get_text(" ", strip=True)).strip()

        # Some writeups put every paragraph in a heading: keep long headings as text, and if
        # nothing else was found, use the headings themselves.
        for el in details.find_all("h2"):
            text = _text(el)
            if len(text) > 80 and text in writeup and not writeup[text]:
                del writeup[text]
                writeup["Description"] = (writeup.get("Description", "") + "\n" + text).strip()
        if not any(writeup.values()):
            heads = [h for h in writeup if h]
            writeup = {"Description": "\n".join(heads)} if heads else {}

    links = [a.get("title") or a.get("href") for a in soup.select('[data-role="software-urls"] a')]
    links = [l for l in links if l]
    members = sorted({a["href"].split("?")[0] for a in soup.select("#app-team a.user-profile-link[href]")})
    gallery = soup.select_one("#gallery")
    videos = [video_url(f.get("src", "")) for f in (gallery.select("iframe") if gallery else [])]
    videos += [video_url(l) for l in links]
    return {
        "title": _text(soup.select_one("#app-title")),
        "tagline": _text(soup.select_one("#software-header p.large")) or None,
        "likes": _int(_text(soup.select_one(".software-likes .side-count"))),
        "built_with": [_text(t) for t in soup.select("#built-with .cp-tag")],
        "links": links,
        "has_repo": int(any(re.search(r"github\.com|gitlab\.com|bitbucket\.org", l) for l in links)),
        "has_video": int(bool(gallery and gallery.select("iframe"))),
        "video_urls": list(dict.fromkeys(v for v in videos if v)),
        "image_count": len(gallery.select("img")) if gallery else 0,
        "writeup": writeup,
        "writeup_chars": sum(len(v) for v in writeup.values()),
        "members": members,
        "submitted_to": submitted,
    }
