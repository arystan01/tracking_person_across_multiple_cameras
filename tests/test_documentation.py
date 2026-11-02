from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def test_all_local_markdown_links_resolve() -> None:
    broken: list[str] = []
    markdown_files = sorted(
        path for path in ROOT.rglob("*.md") if ".git" not in path.parts
    )
    for document in markdown_files:
        for raw_target in MARKDOWN_LINK.findall(
            document.read_text(encoding="utf-8")
        ):
            target = raw_target.strip().strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or not parsed.path:
                continue
            candidate = (document.parent / unquote(parsed.path)).resolve()
            if not candidate.exists():
                broken.append(
                    f"{document.relative_to(ROOT)} -> {target}"
                )
    assert not broken, "broken local Markdown links:\n" + "\n".join(broken)

