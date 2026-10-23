#!/usr/bin/env python3
"""ctoroad: draw a roadmap from a JSON file, as ASCII on the terminal, as a PNG, or as an SVG."""

import json
import os
import sys
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any, TextIO, TypedDict, cast

import click
import svgwrite
from PIL import Image, ImageDraw, ImageFont


class _RequiredProject(TypedDict):
    name: str
    endDate: str


class Project(_RequiredProject, total=False):
    startDate: str
    startAfter: str


Point = tuple[float, float]
Segment = tuple[Point, Point]
Schedule = dict[str, tuple[date, date]]
Paint = Callable[[int, str], str]

DEFAULT_DATA = "roadmap.json"
FORMATS = ("ascii", "png", "svg")
DEFAULT_OUTPUT = {"png": "roadmap.png", "svg": "roadmap.svg"}

LABEL_W = 220
CHART_W = 700
ROW_H = 48
BAR_H = 22
HEADER_H = 40
MARGIN = 30
PADDING = 10
EDGE = "#555555"
GRID = "#dddddd"
PALETTE = ["#cfe8ff", "#d8f5d0", "#ffe3c2", "#f3d1f4", "#fff4b8", "#ffd1d1", "#d2f4f0", "#e0e0ff"]
ANSI_PALETTE = [36, 32, 33, 35, 94, 31, 96, 92]
ANSI_RESET = "\033[0m"

ASCII_LABEL_W = 24
ASCII_BAR_W = 60


class DataError(Exception):
    pass


def parse_date(value: Any, name: str, field: str) -> date:
    if not isinstance(value, str):
        raise DataError(f"'{name}': '{field}' must be a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DataError(f"'{name}': '{field}' must be a YYYY-MM-DD date") from None


def load(path: Path | str) -> list[Project]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise DataError("expected a JSON array of projects")

    projects: list[Project] = []
    by_name: dict[str, Project] = {}
    for index, project in enumerate(data):
        if not isinstance(project, dict):
            raise DataError(f"entry {index} is not an object")
        if not isinstance(project.get("name"), str) or not project["name"].strip():
            raise DataError(f"entry {index}: 'name' is required")
        project = cast(Project, project)
        name = project["name"]
        if name in by_name:
            raise DataError(f"duplicate name '{name}'")
        if "endDate" not in project:
            raise DataError(f"'{name}': 'endDate' is required")
        has_start, has_after = "startDate" in project, "startAfter" in project
        if has_start == has_after:
            raise DataError(f"'{name}': set exactly one of 'startDate' and 'startAfter'")
        by_name[name] = project
        projects.append(project)

    for project in projects:
        after = project.get("startAfter")
        if after is not None and after not in by_name:
            raise DataError(f"'{project['name']}': unknown project '{after}' in 'startAfter'")

    schedule_of(projects)
    return projects


def schedule_of(projects: list[Project]) -> Schedule:
    """Resolve every project to an inclusive (start, end) range. A project that starts after another begins the day after it ends."""
    by_name = {project["name"]: project for project in projects}
    schedule: Schedule = {}

    def resolve(name: str, chain: tuple[str, ...]) -> tuple[date, date]:
        if name in schedule:
            return schedule[name]
        if name in chain:
            raise DataError(f"'{name}': startAfter chain contains a cycle")
        project = by_name[name]
        if "startDate" in project:
            start = parse_date(project["startDate"], name, "startDate")
        else:
            start = resolve(project["startAfter"], chain + (name,))[1] + timedelta(days=1)
        end = parse_date(project["endDate"], name, "endDate")
        if end < start:
            raise DataError(f"'{name}': endDate {end} is before start {start}")
        schedule[name] = (start, end)
        return schedule[name]

    for project in projects:
        resolve(project["name"], ())
    return {project["name"]: schedule[project["name"]] for project in projects}


def span(schedule: Schedule) -> tuple[date, int]:
    """The first day of the roadmap and its length in days."""
    first = min(start for start, _ in schedule.values())
    last = max(end for _, end in schedule.values())
    return first, (last - first).days + 1


def date_range(start: date, end: date) -> str:
    return f"{start} → {end}"


def month_starts(first: date, days: int) -> list[date]:
    last = first + timedelta(days=days)
    months: list[date] = []
    month = first.replace(day=1)
    while month < last:
        if month >= first:
            months.append(month)
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def ascii_chart(projects: list[Project], schedule: Schedule, paint: Paint | None = None) -> list[str]:
    first, days = span(schedule)

    def column(day: date) -> int:
        return (day - first).days * ASCII_BAR_W // days

    lines: list[str] = []
    for index, project in enumerate(projects):
        name = project["name"]
        start, end = schedule[name]
        left = column(start)
        right = max(column(end + timedelta(days=1)), left + 1)
        bar = "█" * (right - left)
        if paint is not None:
            bar = paint(index, bar)
        title = name if len(name) <= ASCII_LABEL_W else name[: ASCII_LABEL_W - 1] + "…"
        lines.append(f"{title:<{ASCII_LABEL_W}} │{' ' * left}{bar}{' ' * (ASCII_BAR_W - right)}│ {date_range(start, end)}")
    return lines


def project_painter() -> Paint:
    def paint(index: int, text: str) -> str:
        return f"\033[{ANSI_PALETTE[index % len(ANSI_PALETTE)]}m{text}{ANSI_RESET}"

    return paint


def use_color(stream: TextIO) -> bool:
    return stream.isatty() and "NO_COLOR" not in os.environ


class Geometry(TypedDict):
    width: int
    height: int
    bars: dict[str, tuple[float, float, float]]  # name -> (left, right, top)
    months: list[tuple[float, str]]
    links: list[Segment]


def place(projects: list[Project], schedule: Schedule) -> Geometry:
    first, days = span(schedule)

    def x_of(day: date) -> float:
        return MARGIN + LABEL_W + (day - first).days * CHART_W / days

    bars: dict[str, tuple[float, float, float]] = {}
    for row, project in enumerate(projects):
        start, end = schedule[project["name"]]
        top = MARGIN + HEADER_H + row * ROW_H + (ROW_H - BAR_H) / 2
        bars[project["name"]] = (x_of(start), x_of(end + timedelta(days=1)), top)

    links: list[Segment] = []
    for project in projects:
        after = project.get("startAfter")
        if after is None:
            continue
        _, _, from_top = bars[after]
        left, _, to_top = bars[project["name"]]
        links.append(((left, from_top + BAR_H / 2), (left, to_top + BAR_H / 2)))

    months = [(x_of(month), month.strftime("%Y-%m")) for month in month_starts(first, days)]
    width = MARGIN * 2 + LABEL_W + CHART_W
    height = MARGIN * 2 + HEADER_H + ROW_H * len(projects)
    return {"width": width, "height": height, "bars": bars, "months": months, "links": links}


def load_fonts() -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, ImageFont.FreeTypeFont | ImageFont.ImageFont]:
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", 14), ImageFont.truetype("DejaVuSans.ttf", 12)
    except OSError:
        fallback = ImageFont.load_default()
        return fallback, fallback


def fill_of(index: int) -> str:
    return PALETTE[index % len(PALETTE)]


def render_png(projects: list[Project], schedule: Schedule, path: Path | str) -> None:
    geo = place(projects, schedule)
    image = Image.new("RGB", (geo["width"], geo["height"]), "white")
    draw = ImageDraw.Draw(image)
    bold, regular = load_fonts()
    body_bottom = geo["height"] - MARGIN

    for x, text in geo["months"]:
        draw.line([(x, MARGIN + HEADER_H - 6), (x, body_bottom)], fill=GRID, width=1)
        draw.text((x + 3, MARGIN + HEADER_H - 22), text, fill="#333333", font=regular)

    for start, end in geo["links"]:
        draw.line([start, end], fill=EDGE, width=2)

    for row, project in enumerate(projects):
        name = project["name"]
        left, right, top = geo["bars"][name]
        start, end = schedule[name]
        label_top = MARGIN + HEADER_H + row * ROW_H + PADDING / 2
        draw.text((MARGIN, label_top), name, fill="black", font=bold)
        draw.text((MARGIN, label_top + 18), date_range(start, end), fill="#333333", font=regular)
        draw.rounded_rectangle([left, top, right, top + BAR_H], radius=6, fill=fill_of(row), outline=EDGE, width=1)

    image.save(path)


def render_svg(projects: list[Project], schedule: Schedule, path: Path | str) -> None:
    geo = place(projects, schedule)
    width, height = geo["width"], geo["height"]
    drawing = svgwrite.Drawing(size=(width, height), profile="full")
    drawing.viewbox(0, 0, width, height)
    drawing["font-family"] = "DejaVu Sans, Verdana, sans-serif"
    drawing.add(drawing.rect(insert=(0, 0), size=(width, height), fill="white"))
    body_bottom = height - MARGIN

    for x, text in geo["months"]:
        drawing.add(drawing.line(start=(x, MARGIN + HEADER_H - 6), end=(x, body_bottom), stroke=GRID, stroke_width=1))
        drawing.add(drawing.text(text, insert=(x + 3, MARGIN + HEADER_H - 10), font_size=12, fill="#333333"))

    for start, end in geo["links"]:
        drawing.add(drawing.line(start=start, end=end, stroke=EDGE, stroke_width=2))

    for row, project in enumerate(projects):
        name = project["name"]
        left, right, top = geo["bars"][name]
        start, end = schedule[name]
        label_top = MARGIN + HEADER_H + row * ROW_H + PADDING / 2
        drawing.add(drawing.text(name, insert=(MARGIN, label_top + 14), font_size=14, font_weight="bold"))
        drawing.add(drawing.text(date_range(start, end), insert=(MARGIN, label_top + 32), font_size=12, fill="#333333"))
        drawing.add(
            drawing.rect(
                insert=(left, top), size=(right - left, BAR_H), rx=6, ry=6, fill=fill_of(row), stroke=EDGE, stroke_width=1
            )
        )

    drawing.saveas(path, pretty=True)


def load_schedule(path: str) -> tuple[list[Project], Schedule]:
    try:
        projects = load(path)
    except (OSError, json.JSONDecodeError, DataError) as err:
        raise click.ClickException(str(err)) from None
    return projects, schedule_of(projects)


@click.command()
@click.argument("data", default=DEFAULT_DATA)
@click.option("--format", "fmt", type=click.Choice(FORMATS), default="ascii", show_default=True, help="Export format (ascii is printed to the terminal).")
@click.option("-o", "--output", help="Output path (default: roadmap.png or roadmap.svg; with ascii, writes the text there instead of printing).")
def cli(data: str, fmt: str, output: str | None) -> None:
    """Draw a roadmap from a JSON file."""
    projects, schedule = load_schedule(data)

    if fmt == "ascii":
        if output is None:
            paint = project_painter() if use_color(sys.stdout) else None
            click.echo("\n".join(ascii_chart(projects, schedule, paint)))
        else:
            with open(output, "w", encoding="utf-8") as f:
                f.write("\n".join(ascii_chart(projects, schedule)) + "\n")
            click.echo(f"Wrote {output}")
        return

    output = output or DEFAULT_OUTPUT[fmt]
    renderer = render_png if fmt == "png" else render_svg
    renderer(projects, schedule, output)
    click.echo(f"Wrote {output}")


if __name__ == "__main__":
    cli()
