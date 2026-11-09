"""Draw where smriti's LongMemEval answer accuracy sits among published figures.

    python scripts/longmemeval_chart.py [--out docs/img]

Writes ``longmemeval-light.svg`` and ``longmemeval-dark.svg`` (the README
shows the one for the reader's theme). Needs matplotlib, which smriti
doesn't depend on. The figures are in :data:`SYSTEMS`, each with its
source; ``docs/BENCHMARKS.md`` says how smriti's was measured and why
none of these is like for like.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import NamedTuple


class Bar(NamedTuple):
    label: str
    model: str
    score: float  # % of questions answered correctly
    upper: float | None  # the top of a reported range
    source: str


SYSTEMS = [
    Bar("Mastra Observational Memory", "gpt-5-mini", 94.87, None,
        "https://mastra.ai/research/observational-memory (2026-02-09)"),
    Bar("Mem0 v3", "", 93.4, 94.4,
        "https://docs.mem0.ai/migration/oss-v2-to-v3 (93.4); "
        "https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm (94.4, platform)"),
    Bar("Hindsight", "Gemini-3", 91.4, None, "https://arxiv.org/abs/2512.12818"),
    Bar("Oblivion", "GPT-4.1-mini", 90.6, None, "https://arxiv.org/abs/2604.00131"),
    Bar("Mastra Observational Memory", "gpt-4o", 84.23, None,
        "https://mastra.ai/research/observational-memory (2026-02-09)"),
    Bar("Nemori", "gpt-4o-mini", 64.2, None, "https://arxiv.org/abs/2508.03341"),
]
# smriti: docs/BENCHMARKS.md, 88 of 100 sampled questions.
SMRITI = Bar("smriti", "Sonnet 5", 88.0, None, "docs/BENCHMARKS.md")
SMRITI_N = 100

THEMES = {
    "light": {"text": "#1f2328", "muted": "#59636e", "bar": "#b6bec7", "range": "#d8dee4",
              "accent": "#1a7f64", "grid": "#d8dee4"},
    "dark": {"text": "#e6edf3", "muted": "#9198a1", "bar": "#4b535d", "range": "#353c44",
             "accent": "#3fb68b", "grid": "#30363d"},
}

CAPTION = (
    "Others: each system's own published figure, mostly reported by its makers. Answering "
    "models, graders and dataset\nreleases differ, so this places smriti; it doesn't rank it. "
    "smriti: a 100-question sample of LongMemEval-S (cleaned),\nSonnet 5 answering through "
    "smriti_read over raw conversations, graded by Codex (gpt-6-astra); the line is a 95% "
    "interval."
)


def interval(p: float, n: int) -> float:
    """Half-width of a 95% normal-approximation interval, in points."""
    return 196 * math.sqrt(p * (1 - p) / n)


def draw(theme: str, out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    c = THEMES[theme]
    plt.rcParams.update({
        "svg.fonttype": "none",  # text stays text: smaller, and in the reader's font
        "svg.hashsalt": "smriti-longmemeval",  # stable ids, so a redraw diffs cleanly
        "font.family": "sans-serif",
        "font.sans-serif": ["Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"],
        "font.size": 10,
    })
    bars = sorted([*SYSTEMS, SMRITI], key=lambda b: b.score)
    fig, ax = plt.subplots(figsize=(8.6, 5.0))
    if out.suffix == ".svg":
        fig.patch.set_alpha(0)  # the page shows through
    else:  # a preview, on the page colour GitHub uses
        fig.patch.set_facecolor("#ffffff" if theme == "light" else "#0d1117")
    ax.set_facecolor("none")
    for i, b in enumerate(bars):
        ours = b is SMRITI
        color = c["accent"] if ours else c["bar"]
        if b.upper is not None:
            ax.barh(i, b.upper, color=c["range"], height=0.62, zorder=2)
        ax.barh(i, b.score, color=color, height=0.62, zorder=3)
        value = f"{b.score:.1f}" if b.upper is None else f"{b.score:.1f}–{b.upper:.1f}"
        end = b.upper or b.score
        if ours:
            half = interval(b.score / 100, SMRITI_N)
            ax.errorbar(b.score, i, xerr=half, fmt="none", ecolor=c["text"], elinewidth=1.2,
                        capsize=4, zorder=4)
            value += f" ± {half:.1f}"
            end = b.score + half
        ax.text(end + 1.2, i, value, va="center", color=c["text"],
                fontweight="bold" if ours else "normal", zorder=5)
    ax.tick_params(colors=c["muted"], length=0)  # before the labels' own colours
    ax.set_yticks(range(len(bars)))
    ax.set_yticklabels([f"{b.label}\n{b.model}" if b.model else b.label for b in bars],
                       linespacing=1.15)
    for tick, b in zip(ax.get_yticklabels(), bars, strict=True):
        tick.set_color(c["accent"] if b is SMRITI else c["text"])
        if b is SMRITI:
            tick.set_fontweight("bold")
    ax.set_xlim(0, 112)
    ax.set_xticks(range(0, 101, 20))
    ax.set_xlabel("% of questions answered correctly", color=c["muted"])
    ax.xaxis.grid(True, color=c["grid"], linewidth=0.8, zorder=0)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.set_title("LongMemEval-S: answer accuracy", loc="left", color=c["text"],
                 fontsize=13, fontweight="bold", pad=12)
    fig.text(0.012, 0.012, CAPTION, color=c["muted"], fontsize=7.6, va="bottom")
    fig.subplots_adjust(left=0.27, right=0.97, top=0.90, bottom=0.24)
    if out.suffix == ".svg":
        fig.savefig(out, format="svg", metadata={"Date": None, "Creator": None})
    else:  # a preview: the format from the name
        fig.savefig(out, dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default="docs/img")
    out = Path(parser.parse_args().out)
    out.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        draw(theme, out / f"longmemeval-{theme}.svg")
        print(out / f"longmemeval-{theme}.svg")


if __name__ == "__main__":
    main()
