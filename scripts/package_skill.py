#!/usr/bin/env python3
"""Package the installable skill with SKILL.md at the ZIP root."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "commit-rings-skill"
archive = ROOT / "dist" / "commit-rings-skill.zip"
archive.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
    for path in sorted(SKILL.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc" and path.name != ".DS_Store":
            output.write(path, path.relative_to(SKILL))
    output.write(ROOT / "LICENSE", "LICENSE")
print(archive)
