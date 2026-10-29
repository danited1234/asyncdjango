"""Draw the README charts from a results file (benchmarks/run.py or server.py).

    python -m benchmarks.plot benchmarks/results/latest.json [--out docs/images]
                              [--machine "Intel Core i5-8350U, 4 cores"]

Writes a light and a dark SVG per chart; the README picks one per theme.
Needs matplotlib (pip install -e ".[bench]").
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

from benchmarks.stacks import STACKS

SCENARIO_TITLES = {
    "read": "Read: fetch, count, serialize 20 rows",
    "write": "Write: validate (unique check) and insert",
    "slow": "I/O-bound: one 50 ms query",
}

# Rows top to bottom: DRF-style APIs, then plain Django async views. The
# connection-per-request row comes first so that its color (slot 5) only
# borders slot 1, the pairing that passes the palette validator in both themes.
ORDER = ["asyncdjango-drf-req", "asyncdjango-drf", "adrf", "drf", "asyncdjango", "django"]
POSITIONS = {"asyncdjango-drf-req": 0, "asyncdjango-drf": 1, "adrf": 2, "drf": 3,
             "asyncdjango": 4.6, "django": 5.6}

# Categorical slots 1-5 of the validated default palette, per theme. Color
# follows the stack family; both default asyncdjango stacks share slot 1.
THEMES = {
    "light": {
        "surface": "#fcfcfb", "text": "#0b0b0b", "text2": "#52514e",
        "grid": "#e6e5e1", "axis": "#c9c8c2",
        "series": {"asyncdjango-drf": "#2a78d6", "asyncdjango": "#2a78d6",
                   "adrf": "#eb6834", "drf": "#1baf7a", "django": "#eda100",
                   "asyncdjango-drf-req": "#e87ba4"},
    },
    "dark": {
        "surface": "#1a1a19", "text": "#ffffff", "text2": "#c3c2b7",
        "grid": "#33332f", "axis": "#55554f",
        "series": {"asyncdjango-drf": "#3987e5", "asyncdjango": "#3987e5",
                   "adrf": "#d95926", "drf": "#199e70", "django": "#c98500",
                   "asyncdjango-drf-req": "#d55181"},
    },
}
# Secondary encoding for the line chart: plain Django views are dashed, and
# the connection-per-request variant is dotted with square markers (lines can
# cross any series there, not just their neighbors in the bar charts).
DASHED = {"asyncdjango", "django"}
DOTTED = {"asyncdjango-drf-req"}


def line_style(stack: str) -> object:
    if stack in DOTTED:
        return (0, (1, 1.5))
    return (0, (4, 2)) if stack in DASHED else "solid"


def marker(stack: str) -> str:
    return "s" if stack in DOTTED else "o"

METRICS = {
    "throughput": ("rps", "Requests per second (higher is better)", "{:,.0f}"),
    "latency": ("p99_ms", "99th percentile latency, ms (lower is better)", "{:,.0f}"),
    "cpu": ("cpu_ms_per_request", "CPU time per request, ms (lower is better)", "{:.1f}"),
}


def style(theme: dict) -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10,
        "text.color": theme["text"], "axes.labelcolor": theme["text2"],
        "xtick.color": theme["text2"], "ytick.color": theme["text"],
        "axes.edgecolor": theme["axis"], "figure.facecolor": theme["surface"],
        "axes.facecolor": theme["surface"], "savefig.facecolor": theme["surface"],
        "svg.hashsalt": "asyncdjango",
    })


def clean_axes(ax, theme: dict, *, horizontal_grid: bool) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left" if not horizontal_grid else "bottom"].set_color(theme["axis"])
    ax.grid(axis="y" if horizontal_grid else "x", color=theme["grid"], linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)


def footer(fig, data: dict, machine: str | None) -> str:
    environment, packages = data["environment"], data["environment"]["packages"]
    repeat = data.get("repeat", 1)
    if environment.get("mode") == "server":
        harness = (f"uvicorn / gunicorn, {environment.get('workers', 1)} worker(s) on"
                   f" {len(environment['server_cpus'].split(','))} vCPU, wrk")
    else:
        harness = "in-process, no HTTP server"
    text = (f"{machine or environment['processor']} · {environment['platform'].split('-')[0]}"
            f" · Python {environment['python']} · PostgreSQL {environment['postgresql']}"
            f" · Django {packages['Django']} · DRF {packages['djangorestframework']}"
            f" · ADRF {packages['adrf']} · {data['pool_size']}-connection pool for every stack"
            + (f" · median of {repeat} rounds" if repeat > 1 else "") +
            f" · {harness} · {environment['date']}")
    fig.text(0.01, 0.015, text, fontsize=7.5, color=plt.rcParams["axes.labelcolor"])
    return text


def bars(data: dict, metric: str, concurrency: int, theme: dict, meta: dict) -> plt.Figure:
    key, title, fmt = METRICS[metric]
    scenarios = [s for s in SCENARIO_TITLES if any(r["scenario"] == s for r in data["results"])]
    fig, axes = plt.subplots(1, len(scenarios), figsize=(4.1 * len(scenarios), 3.5),
                             sharey=True, squeeze=False)
    for ax, scenario in zip(axes[0], scenarios, strict=True):
        rows = {r["stack"]: r for r in data["results"]
                if r["scenario"] == scenario and r["concurrency"] == concurrency}
        stacks = [s for s in ORDER if s in rows]
        values = [rows[s][key] for s in stacks]
        order = [s for s in ORDER if any(r["stack"] == s for r in data["results"])]
        ax.barh([POSITIONS[s] for s in stacks], values, height=0.72,
                color=[theme["series"][s] for s in stacks])
        top = max(values)
        for stack, value in zip(stacks, values, strict=True):
            ax.text(value + top * 0.02, POSITIONS[stack], fmt.format(value),
                    va="center", fontsize=9, color=theme["text"])
        ax.set_xlim(0, top * 1.22)
        ax.set_title(SCENARIO_TITLES[scenario], fontsize=9.5, loc="left",
                     color=theme["text2"])
        clean_axes(ax, theme, horizontal_grid=False)
        ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(
            lambda value, _: f"{value:,.0f}"))
    axes[0][0].set_yticks([POSITIONS[s] for s in order])
    axes[0][0].set_yticklabels([STACKS[s].label for s in order])
    axes[0][0].invert_yaxis()
    fig.suptitle(f"{title}, {concurrency} concurrent clients", x=0.01, ha="left",
                 fontsize=12, fontweight="bold", color=theme["text"])
    footer(fig, data, meta["machine"])
    fig.tight_layout(rect=(0, 0.04, 1, 0.97))
    return fig


def scaling(data: dict, theme: dict, meta: dict) -> plt.Figure:
    scenarios = [s for s in SCENARIO_TITLES if any(r["scenario"] == s for r in data["results"])]
    fig, axes = plt.subplots(1, len(scenarios), figsize=(4.1 * len(scenarios), 3.6),
                             squeeze=False)
    for ax, scenario in zip(axes[0], scenarios, strict=True):
        for stack in ORDER:
            rows = sorted((r for r in data["results"]
                           if r["scenario"] == scenario and r["stack"] == stack),
                          key=lambda r: r["concurrency"])
            if not rows:
                continue
            # With --repeat, a band shows the slowest and fastest round.
            rounds = [[r["rps"] for r in data.get("runs", [])
                       if (r["scenario"], r["stack"], r["concurrency"])
                       == (scenario, stack, row["concurrency"])] or [row["rps"]]
                      for row in rows]
            ax.fill_between([r["concurrency"] for r in rows], [min(v) for v in rounds],
                            [max(v) for v in rounds], color=theme["series"][stack],
                            alpha=0.12, linewidth=0)
            ax.plot([r["concurrency"] for r in rows], [r["rps"] for r in rows],
                    color=theme["series"][stack], linewidth=2,
                    linestyle=line_style(stack), marker=marker(stack), markersize=5,
                    markeredgecolor=theme["surface"], markeredgewidth=1.5)
        concurrencies = sorted({r["concurrency"] for r in data["results"]})
        ax.set_xticks(concurrencies)
        ax.set_ylim(bottom=0)
        ax.set_xlabel("concurrent clients")
        ax.set_title(SCENARIO_TITLES[scenario], fontsize=9.5, loc="left",
                     color=theme["text2"])
        clean_axes(ax, theme, horizontal_grid=True)
    axes[0][0].set_ylabel("requests per second")
    present = [s for s in ORDER if any(r["stack"] == s for r in data["results"])]
    handles = [Line2D([], [], color=theme["series"][s], linewidth=2, marker=marker(s),
                      markersize=5, linestyle=line_style(s), label=STACKS[s].label)
               for s in present]
    fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.005, 0.95),
               ncols=3, frameon=False, fontsize=9, handlelength=2.6,
               labelcolor=theme["text"])
    title = "Throughput as concurrency grows (higher is better)"
    if data.get("repeat", 1) > 1:
        title += f"; median of {data['repeat']} rounds, band = slowest to fastest"
    fig.suptitle(title, x=0.01, ha="left",
                 fontsize=12, fontweight="bold", color=theme["text"])
    footer(fig, data, meta["machine"])
    fig.tight_layout(rect=(0, 0.04, 1, 0.88))
    return fig


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("--out", type=Path, default=Path("docs/images"))
    parser.add_argument("--concurrency", type=int, default=50,
                        help="concurrency level for the bar charts")
    parser.add_argument("--machine", help="readable CPU description for the footer")
    args = parser.parse_args()
    data = json.loads(args.results.read_text())
    meta = {"machine": args.machine}
    args.out.mkdir(parents=True, exist_ok=True)
    for name, theme in THEMES.items():
        style(theme)
        figures = {metric: bars(data, metric, args.concurrency, theme, meta)
                   for metric in METRICS}
        figures["scaling"] = scaling(data, theme, meta)
        for chart, fig in figures.items():
            path = args.out / f"bench-{chart}-{name}.svg"
            fig.savefig(path, format="svg", metadata={"Date": None})
            plt.close(fig)
            print(path)


if __name__ == "__main__":
    main()
