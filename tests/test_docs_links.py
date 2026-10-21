# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Every relative link and anchor in the README, the CHANGELOG and docs/ resolves.

Renames break links silently: two CHANGELOG links pointed at files that had moved, and a
runbook heading change in 0.4.0 would have orphaned a link from testing-gaps.md. And one link
is load-bearing at runtime: the 400 for a ``transport`` field names an upgrade.md anchor, so a
heading edit there would send every refused client to a section that does not exist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / "README.md", ROOT / "CHANGELOG.md", *sorted((ROOT / "docs").rglob("*.md"))]


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower-cased, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[`*]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _outside_fences(text: str):
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            yield number, line


def _anchors(path: Path) -> set[str]:
    return {_slug(line.lstrip("#")) for _, line in _outside_fences(path.read_text()) if line.startswith("#")}


def _links(path: Path):
    for number, line in _outside_fences(path.read_text()):
        for match in re.finditer(r"\]\(([^)\s]+)\)", line):
            target = match.group(1)
            if not target.startswith(("http://", "https://", "mailto:")):
                yield number, target


@pytest.mark.parametrize("doc", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_relative_link_and_anchor_resolves(doc):
    broken = []
    for number, target in _links(doc):
        path, _, fragment = target.partition("#")
        resolved = (doc.parent / path).resolve() if path else doc.resolve()
        if not resolved.exists():
            broken.append(f"{doc.name}:{number} {target} (no such file)")
        elif fragment and resolved.suffix == ".md" and fragment not in _anchors(resolved):
            broken.append(f"{doc.name}:{number} {target} (no such heading)")
    assert not broken, "\n".join(broken)


def test_the_transport_refusal_points_at_a_section_that_exists():
    from device_mcp_gateway.registry.validation import TRANSPORT_REMOVED

    match = re.search(r"docs/upgrade\.md#([\w-]+)", TRANSPORT_REMOVED)
    assert match, TRANSPORT_REMOVED
    assert match.group(1) in _anchors(ROOT / "docs" / "upgrade.md")
