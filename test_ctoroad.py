import json
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

import ctoroad

SAMPLE = Path(__file__).with_name("roadmap.json")


def run(*args):
    return CliRunner().invoke(ctoroad.cli, [str(a) for a in args])


def write_projects(tmp_path, projects):
    path = tmp_path / "roadmap.json"
    path.write_text(json.dumps(projects), encoding="utf-8")
    return path


def test_load_sample_data():
    projects = ctoroad.load(SAMPLE)
    assert len(projects) == 5


def test_start_after_begins_the_day_after_the_predecessor_ends():
    projects = [
        {"name": "A", "startDate": "2026-01-01", "endDate": "2026-01-31"},
        {"name": "B", "startAfter": "A", "endDate": "2026-02-28"},
    ]
    schedule = ctoroad.schedule_of(projects)
    assert schedule["B"] == (date(2026, 2, 1), date(2026, 2, 28))


def test_start_after_chains_in_any_file_order():
    projects = [
        {"name": "C", "startAfter": "B", "endDate": "2026-03-31"},
        {"name": "B", "startAfter": "A", "endDate": "2026-02-28"},
        {"name": "A", "startDate": "2026-01-01", "endDate": "2026-01-31"},
    ]
    schedule = ctoroad.schedule_of(projects)
    assert schedule["C"][0] == date(2026, 3, 1)
    assert list(schedule) == ["C", "B", "A"]


@pytest.mark.parametrize(
    "projects, message",
    [
        ([{"name": "A", "startDate": "2026-01-01", "endDate": "2026-01-02"}] * 2, "duplicate name"),
        ([{"name": "A", "startDate": "2026-01-01"}], "'endDate' is required"),
        ([{"name": "A", "endDate": "2026-01-02"}], "exactly one of"),
        (
            [{"name": "A", "startDate": "2026-01-01", "startAfter": "A", "endDate": "2026-01-02"}],
            "exactly one of",
        ),
        ([{"name": "A", "startAfter": "Z", "endDate": "2026-01-02"}], "unknown project 'Z'"),
        ([{"name": "A", "startDate": "01/02/2026", "endDate": "2026-01-02"}], "YYYY-MM-DD"),
        ([{"name": "A", "startDate": "2026-02-01", "endDate": "2026-01-02"}], "before start"),
        (
            [
                {"name": "A", "startAfter": "B", "endDate": "2026-01-02"},
                {"name": "B", "startAfter": "A", "endDate": "2026-01-02"},
            ],
            "cycle",
        ),
        ({"name": "A"}, "JSON array"),
        (["A"], "not an object"),
    ],
)
def test_load_rejects_bad_data(tmp_path, projects, message):
    with pytest.raises(ctoroad.DataError, match=message):
        ctoroad.load(write_projects(tmp_path, projects))


def test_ascii_chart_output():
    projects = [
        {"name": "A", "startDate": "2026-01-01", "endDate": "2026-01-30"},
        {"name": "B", "startAfter": "A", "endDate": "2026-02-28"},
    ]
    lines = ctoroad.ascii_chart(projects, ctoroad.schedule_of(projects))
    half = ctoroad.ASCII_BAR_W // 2
    assert lines[0] == f"{'A':<24} │{'█' * half}{' ' * half}│ 2026-01-01 → 2026-01-30"
    assert lines[1] == f"{'B':<24} │{' ' * half}{'█' * half}│ 2026-01-31 → 2026-02-28"


def test_ascii_chart_colours_bars():
    projects = [{"name": "A", "startDate": "2026-01-01", "endDate": "2026-01-02"}]
    lines = ctoroad.ascii_chart(projects, ctoroad.schedule_of(projects), ctoroad.project_painter())
    assert "\033[36m" in lines[0] and ctoroad.ANSI_RESET in lines[0]


def test_single_day_project_still_draws_a_bar():
    projects = [
        {"name": "Long", "startDate": "2026-01-01", "endDate": "2026-12-31"},
        {"name": "Day", "startDate": "2026-06-01", "endDate": "2026-06-01"},
    ]
    lines = ctoroad.ascii_chart(projects, ctoroad.schedule_of(projects))
    assert "█" in lines[1]


def test_cli_ascii_to_stdout():
    result = run(SAMPLE)
    assert result.exit_code == 0
    assert "Discovery" in result.output and "█" in result.output


def test_cli_ascii_to_file(tmp_path):
    out = tmp_path / "roadmap.txt"
    assert run(SAMPLE, "-o", out).exit_code == 0
    assert "Launch" in out.read_text(encoding="utf-8")


def test_cli_png(tmp_path):
    out = tmp_path / "roadmap.png"
    assert run(SAMPLE, "--format", "png", "-o", out).exit_code == 0
    with Image.open(out) as image:
        assert image.size[0] > 0 and image.size[1] > 0


def test_cli_svg(tmp_path):
    out = tmp_path / "roadmap.svg"
    assert run(SAMPLE, "--format", "svg", "-o", out).exit_code == 0
    root = ET.parse(out).getroot()
    assert root.tag.endswith("svg")
    assert "Platform Migration" in out.read_text(encoding="utf-8")


def test_cli_reports_bad_data(tmp_path):
    result = run(write_projects(tmp_path, [{"name": "A"}]))
    assert result.exit_code == 1
    assert "'endDate' is required" in result.output


def test_cli_reports_missing_file():
    result = run("nope.json")
    assert result.exit_code == 1
    assert "nope.json" in result.output
