"""The menu only builds command lines; check the ones it builds for scripted answers."""

from hackathon_index import menu


class Answers:
    """Stands in for questionary: each prompt pops the next scripted answer."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def _next(self, *a, **kw):
        value = self.answers.pop(0)
        return type("Q", (), {"ask": lambda self: value})()

    select = confirm = text = _next


def test_menu_builds_commands(monkeypatch):
    monkeypatch.setattr(menu, "questionary", Answers(True, "Normal (3 workers)"))
    assert menu.update() == ["crawl", "all", *menu.LOSERS]

    monkeypatch.setattr(menu, "questionary", Answers("2021", False, "Careful (1 worker, if Devpost starts pushing back)"))
    assert menu.backfill() == ["crawl", "all", "--since", "2021-01-01", "--workers", "1"]

    monkeypatch.setattr(menu, "questionary", Answers("projects (writeups, built-with, links, videos)"))
    assert menu.reparse() == ["crawl", "projects", "--reparse"]


def test_execute_shows_command_and_respects_no(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(menu, "questionary", Answers(False))
    ran = []
    monkeypatch.setattr(menu.subprocess, "run", lambda *a, **kw: ran.append(a))
    menu.execute(tmp_path, ["crawl", "all", "--since", "2021-01-01"])
    assert "./run.sh crawl all --since 2021-01-01" in capsys.readouterr().out
    assert ran == []
