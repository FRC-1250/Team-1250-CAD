"""Validate an issue's part number the moment it's opened.

Runs from .github/workflows/validate-part-number.yml. Costs no Onshape quota.

WHY: opening an issue is how a part number gets claimed and how work gets
assigned. Nothing enforced that the number was well-formed, unused, or in the
right subsystem. Those mistakes surface at EXPORT time -- weeks later, after the
wrong number is already in someone's CAD tab and on the board. This catches them
where the mistake happens.

It imports identity.py from the exporter rather than re-implementing the regex.
Two copies of the convention would drift, and the drift would be silent.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "drawing-export"))
import identity  # noqa: E402

API = "https://api.github.com"
TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
ISSUE = int(os.environ["ISSUE_NUMBER"])
TITLE = os.environ.get("ISSUE_TITLE", "")

MARKER = "<!-- part-number-check -->"   # lets us find and replace our own comment
LABEL = "needs-part-number"

# The legacy scheme, where the GitHub issue number WAS the part number:
#   FRC1250-26-0091 Chassis Tubes
# 91 issues use it. This workflow fires on `edited` too, so without this the very
# first edit to any old issue would flag it as malformed and comment on it. They
# are not wrong -- they are finished. Leave them alone.
LEGACY = re.compile(r"^\s*FRC\d{3,5}-\d{2}-(\d{4}|00xx)\b", re.I)


def api(method, path, body=None):
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode() if body else None, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "frc1250-part-number-check")
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        print("HTTP {} {} {}".format(e.code, path, e.read()[:200].decode()))
        return None


def all_issues():
    out, page = [], 1
    while True:
        b = api("GET", "/repos/{}/issues?state=all&per_page=100&page={}".format(REPO, page))
        if not b:
            break
        out.extend(i for i in b if "pull_request" not in i)
        if len(b) < 100:
            break
        page += 1
    return out


def load_config():
    p = os.path.join(REPO_ROOT, "tools", "drawing-export", "config.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def upsert_comment(text):
    """One comment, edited in place. On 'edited' this re-runs -- appending would
    bury the issue in near-identical bot comments."""
    body = MARKER + "\n" + text
    for c in api("GET", "/repos/{}/issues/{}/comments".format(REPO, ISSUE)) or []:
        if MARKER in (c.get("body") or ""):
            api("PATCH", "/repos/{}/issues/comments/{}".format(REPO, c["id"]), {"body": body})
            return
    api("POST", "/repos/{}/issues/{}/comments".format(REPO, ISSUE), {"body": body})


def delete_comment():
    for c in api("GET", "/repos/{}/issues/{}/comments".format(REPO, ISSUE)) or []:
        if MARKER in (c.get("body") or ""):
            api("DELETE", "/repos/{}/issues/comments/{}".format(REPO, c["id"]))


def set_label(on):
    if on:
        api("POST", "/repos/{}/issues/{}/labels".format(REPO, ISSUE), {"labels": [LABEL]})
    else:
        api("DELETE", "/repos/{}/issues/{}/labels/{}".format(REPO, ISSUE, LABEL))


def next_free(ident, issues):
    """Next free number for THIS part's team/year/bot.

    Takes the prefix from the issue being validated, not from config: someone
    opening a `1250-26A-...` issue must be offered another A-bot number, not
    whatever bot config happens to name.
    """
    taken = set()
    for i in issues:
        it = identity.parse(i["title"] or "")
        if it and it.subsystem == ident.subsystem and it.is_subassembly == ident.is_subassembly \
           and it.team == ident.team and it.yy == ident.yy and it.bot == ident.bot:
            taken.add(int(it.part))
    n = 1
    while n in taken:
        n += 1
    return "{}-{}{}-{}{}{:02d}".format(
        ident.team, ident.yy, ident.bot,
        "A" if ident.is_subassembly else "", ident.subsystem, n)


def main():
    cfg = load_config()
    subs = {k: v for k, v in (cfg.get("subsystems") or {}).items() if not k.startswith("_")}

    # --- legacy: not our business ----------------------------------------
    if LEGACY.match(TITLE):
        print("LEGACY, ignoring: {!r}".format(TITLE))
        return 0

    ident = identity.parse(TITLE)

    # --- malformed --------------------------------------------------------
    if ident is None:
        table = "\n".join("| `{}` | {} |".format(k, v) for k, v in sorted(subs.items()))
        upsert_comment(
            "**This issue has no valid part number in its title.**\n\n"
            "Title must contain `<team>-<YY><bot>-[A]<subsystem><part>`, e.g. "
            "`1250-26B-101 Gearbox Plate`.\n\n"
            "| Subsystem | |\n|---|---|\n" + table + "\n\n"
            "Prefix the number with `A` for a **subassembly** drawing: `1250-26B-A503`.\n\n"
            "Run `py tools/drawing-export/next_number.py` for the next free number.\n\n"
            "> Until the title is fixed, the drawing exporter cannot link this part's "
            "PDF here, and the part will not appear on the project board."
        )
        set_label(True)
        print("INVALID: {!r}".format(TITLE))
        return 0

    issues = all_issues()

    # --- duplicate --------------------------------------------------------
    dupes = []
    for i in issues:
        if i["number"] == ISSUE:
            continue
        other = identity.parse(i["title"] or "")
        if other and other.id == ident.id:
            dupes.append(i)
    if dupes:
        lst = ", ".join("#{}".format(i["number"]) for i in dupes)
        upsert_comment(
            "**Part number `{}` is already taken** by {}.\n\n"
            "Two issues sharing a number means the exporter cannot tell which one a "
            "drawing belongs to -- it will refuse to comment on either rather than "
            "guess.\n\n"
            "Next free in subsystem {}: **`{}`**".format(
                ident.id, lst, ident.subsystem, next_free(ident, issues))
        )
        set_label(True)
        print("DUPLICATE: {} also in {}".format(ident.id, lst))
        return 0

    # --- unknown subsystem ------------------------------------------------
    if str(ident.subsystem) not in subs:
        upsert_comment(
            "**Subsystem `{}` is not configured.**\n\n"
            "`{}` parses as subsystem {}, but the known subsystems are: {}.\n\n"
            "Either the number is wrong, or `tools/drawing-export/config.json` needs "
            "a new subsystem.".format(
                ident.subsystem, ident.id, ident.subsystem,
                ", ".join("`{}` {}".format(k, v) for k, v in sorted(subs.items())))
        )
        set_label(True)
        print("UNKNOWN SUBSYSTEM: {}".format(ident.subsystem))
        return 0

    # --- valid ------------------------------------------------------------
    delete_comment()
    set_label(False)
    print("OK: {} ({} - {}){}".format(
        ident.id, ident.subsystem, subs[str(ident.subsystem)],
        " [subassembly]" if ident.is_subassembly else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
