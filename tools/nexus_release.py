"""Text for the release workflows (.github/workflows/release.yml and nexus.yml).

    python tools/nexus_release.py section 1.1.0      # the CHANGELOG.md section of a version, as on GitHub
    python tools/nexus_release.py check 1.1.0        # the release rules for that section
    python tools/nexus_release.py files              # the title and description of the file on Nexus

check: a section is a list, one change per line starting "- New:", "- Changed:" or "- Removed:", with nothing
else in it, and follows the rules for public text: no em or en dashes, and none of the words in WORDS.

files: the upload action always sends a title (the file name, when none is given), so the workflow passes back
what the page has now and the title and description set by hand on Nexus stay. Read from the public v2 API, no
key needed. The Nexus page (Helldivers Remastered) holds two live files, Enhanced Visuals and Upscaled Textures,
so the file is the live (MAIN) one whose name starts with FILE_NAME; anything else stops the run before an upload.
Prints lines for $GITHUB_OUTPUT.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

GAME_ID = 3552   # helldivers
MOD_ID = 48      # nexusmods.com/helldivers/mods/48, Unique Mod ID 15255723835440 (3552 << 32 | 48)
FILE_NAME = "Enhanced Visuals"
CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
WORDS = re.compile(r"\b(its|it'?s|i|me|my|we|our|us|you|your)\b", re.I)  # the possessive, first and second person
KINDS = re.compile(r"^- (New|Changed|Removed): \S")


def section(version):
    """The lines of the CHANGELOG.md section headed "## <version>", blank lines dropped."""
    out, on = [], False
    for line in CHANGELOG.read_text(encoding="utf-8").replace("\r", "").split("\n"):
        if line.startswith("## "):
            on = line.strip() == f"## {version}"
            continue
        if on and line.strip():
            out.append(line.strip())
    return out


def check(entries):
    problems = []
    if not entries:
        problems.append("the section is empty or missing")
    for s in entries:
        if not KINDS.match(s):
            problems.append(f"not a list entry starting \"- New:\", \"- Changed:\" or \"- Removed:\": {s}")
            continue
        body = re.sub(r"`[^`]*`", "", s[2:])  # code, such as an option id, isn't prose
        hit = WORDS.search(body)
        if hit:
            problems.append(f"\"{hit.group(0)}\" in: {s}")
        if "—" in s or "–" in s:
            problems.append(f"a dash in: {s}")
    return problems


def files():
    q = {"query": f"{{ modFiles(modId: {MOD_ID}, gameId: {GAME_ID}) {{ name description category }} }}"}
    req = urllib.request.Request(
        "https://api.nexusmods.com/v2/graphql", data=json.dumps(q).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "EnhancedVisuals-release"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if data.get("errors"):
        sys.exit(f"Nexus refused the file list: {data['errors']}")
    live = [f for f in data["data"]["modFiles"] if f["category"] == "MAIN" and f["name"].startswith(FILE_NAME)]
    if len(live) != 1:
        sys.exit(f"Expected one live Nexus file named {FILE_NAME}, found {len(live)}: {[f['name'] for f in live]}. "
                 "Nothing uploaded.")
    f = live[0]
    desc = (f["description"] or "").replace("\r", "")
    return f"name={f['name']}\ndescription<<DESC_END\n{desc}\nDESC_END"


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "section":
        print("\n".join(section(sys.argv[2])))
    elif len(sys.argv) == 3 and sys.argv[1] == "check":
        problems = check(section(sys.argv[2]))
        for p in problems:
            print(f"::error file=CHANGELOG.md::{p}")
        sys.exit(1 if problems else 0)
    elif len(sys.argv) == 2 and sys.argv[1] == "files":
        print(files())
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
