"""The reference profile's wake-briefing sections: HANDS, SUTI NOW, ACROSS SURFACES.

Order, caps and pinning are the ones the briefing used before these
sections became the profile's: Hands and Suti now are pinned (never
cut), and Across surfaces is cut after the journal and the project
files, down to ``ACROSS_FLOOR``, before identity is touched.
"""

from __future__ import annotations

from smriti.profiles import WakeContext, WakeSection
from smriti.profiles.reference import presence
from smriti.profiles.reference.presence import WakeSources

ACROSS_FLOOR = 400
ACROSS_LABEL = "Across surfaces: cut for budget (Telegram chat-cycles, brain/notifications.json)"


def wake_sections(ctx: WakeContext) -> list[WakeSection]:
    """The three presence sections, read from ``ctx.sources`` when it is a :class:`WakeSources`."""
    src = ctx.sources if isinstance(ctx.sources, WakeSources) else WakeSources.for_root(
        ctx.memory_root, cwd=ctx.cwd, now=ctx.now, harness=ctx.harness)
    # The builders are looked up when called, so a test can replace one.
    return [
        WakeSection("hands", "HANDS", lambda: presence.hands_section(src), pinned=True),
        WakeSection("suti", "SUTI NOW", lambda: presence.suti_now_section(src), pinned=True),
        WakeSection("across", "ACROSS SURFACES, last 24h",
                    lambda: presence.across_section(src),
                    cap=presence.ACROSS_CAP, floor=ACROSS_FLOOR, label=ACROSS_LABEL),
    ]
