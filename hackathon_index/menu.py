"""Interactive menu: `./run.sh` with no arguments. Every choice shows the equivalent
command before running it, so the flags are easy to pick up and reuse in scripts."""

import shlex
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import questionary

from . import snapshot

LOSERS = ["--loser-rate", "0.15", "--min-losers", "5", "--max-losers", "40"]
SPEEDS = {
    "Normal (3 workers)": [],
    "Careful (1 worker, if Devpost starts pushing back)": ["--workers", "1"],
    "Fast (5 workers)": ["--workers", "5"],
}


def run(data: Path) -> None:
    while True:
        print()
        choice = questionary.select("What do you want to do?", choices=[
            "Status: what's collected so far",
            "Update: new events and newly announced winners",
            "Go further back: add an earlier year",
            "Sample non-winners: full pages for a balanced sample",
            "Re-parse saved pages (after a parser update, no network)",
            "Grade events with an LLM: winners, stakes, sponsors, competition",
            "Score events: flag junk and swag-only events",
            "Snapshots: list, save, or restore a copy of the database",
            "Quit",
        ]).ask()
        if choice is None or choice == "Quit":
            return
        args = {
            "Status": lambda: ["status"],
            "Update": update,
            "Go further back": backfill,
            "Sample non-winners": sample,
            "Re-parse saved pages": reparse,
            "Grade events with an LLM": label_events,
            "Score events": lambda: ["score"],
            "Snapshots": lambda: snapshots(data),
        }[choice.split(":")[0].split(" (")[0]]()
        if args:
            execute(data, args, ask=args[0] not in ("status", "snapshots"))


def execute(data: Path, args: list[str], ask: bool = True) -> None:
    shown = shlex.join(["./run.sh", *args])
    print(f"\n  equivalent command:  {shown}\n")
    if ask and not questionary.confirm("Run it?", default=True).ask():
        return
    cmd = [sys.executable, "-m", "hackathon_index", "--data", str(data), *args]
    if args[0] == "crawl" and shutil.which("caffeinate"):
        cmd = ["caffeinate", "-i", *cmd]  # keep the Mac awake while crawling
    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        pass  # the crawl prints its own "stopped" message; back to the menu


def speed() -> list[str]:
    return SPEEDS[questionary.select("Speed?", choices=list(SPEEDS)).ask() or "Normal (3 workers)"]


def update() -> list[str]:
    losers = questionary.confirm("Also sample non-winners for the new events?", default=True).ask()
    return ["crawl", "all", *(LOSERS if losers else []), *speed()]


def backfill() -> list[str]:
    this = date.today().year
    years = [str(y) for y in range(this - 1, 2014, -1)]
    year = questionary.select("Add events back to which year?", choices=years, default="2022").ask()
    if not year:
        return []
    print("  Older years take a few hours each. Everything already saved is skipped.")
    losers = questionary.confirm("Also sample non-winners?", default=True).ask()
    return ["crawl", "all", "--since", f"{year}-01-01", *(LOSERS if losers else []), *speed()]


def sample() -> list[str]:
    rate = questionary.text("Share of non-winners per event", default="0.15").ask()
    lo = questionary.text("At least this many per event", default="5").ask()
    hi = questionary.text("At most this many per event", default="40").ask()
    if None in (rate, lo, hi):
        return []
    return ["crawl", "all", "--loser-rate", rate, "--min-losers", lo, "--max-losers", hi, *speed()]


def label_events() -> list[str]:
    mode = questionary.select("Which run?", choices=[
        "Test: 30 events + your anchors with 3 models, writes a review file (a few cents)",
        "Full run: every gallery event with one model (snapshot first, resumable)",
    ]).ask()
    if not mode:
        return []
    if mode.startswith("Test"):
        return ["label-events", "--sample", "30"]
    model = questionary.text("Model", default="deepseek/deepseek-v4.1-flash").ask()
    cap = questionary.text("Stop after spending (USD)", default="3").ask()
    return ["label-events", "--model", model, "--max-cost", cap] if model and cap else []


def reparse() -> list[str]:
    stage = questionary.select("Which pages?", choices=[
        "projects (writeups, built-with, links, videos)",
        "overview (prizes and judges)",
    ]).ask()
    return ["crawl", stage.split()[0], "--reparse"] if stage else []


def snapshots(data: Path) -> list[str]:
    saved = snapshot.listing(data / "index.db")
    action = questionary.select("Snapshots", choices=[
        f"List ({len(saved)} saved)", "Save a copy now", "Restore one", "Back"]).ask()
    if not action or action == "Back":
        return []
    if action.startswith("List"):
        return ["snapshots"]
    if action == "Save a copy now":
        label = questionary.text("Label", default="manual").ask()
        return ["snapshot", label or "manual"]
    if not saved:
        print("  No snapshots yet.")
        return []
    name = questionary.select("Restore which one? (the current state is saved first)",
                              choices=[p.name for p in reversed(saved)]).ask()
    return ["restore", name] if name else []
