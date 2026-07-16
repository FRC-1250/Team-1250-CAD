"""Configure a GitHub Project (v2) for a season: subsystem options + link fields.

    py project_setup.py --project 19                 report only (default)
    py project_setup.py --project 19 --apply         make the changes

Run once per year, on the new season's project. Costs ZERO Onshape quota.

WHAT IT DOES
  1. Replaces the `Subsystem` single-select options with the subsystems from
     config.json, so the board matches the part-number scheme.
  2. Adds a `Link to PDF` text field if missing.

WHY THE SAFETY CHECK MATTERS
  Replacing single-select options DESTROYS the value on every item using an option
  that goes away. Project #15 ('CAD 2026 Rebuilt') has 90 items filed under the old
  taxonomy (CHASSIS/INTAKE/SHOOTER/CLIMB/FUEL-MGMT). Running this on it would blank
  the Subsystem column for most of them, with no undo. So: this refuses to drop an
  option that items are using, unless you pass --force and mean it.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
SECRETS = os.path.join(HERE, "secrets.json")

LINK_FIELDS = ["Link to PDF"]  # 'Link to Drawing' / 'Link to Component' already exist


def gql(tok, query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request("https://api.github.com/graphql", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("User-Agent", "frc1250-drawing-export")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as f:
            d = json.loads(f.read())
            for e in (d.get("errors") or []):
                print("  GQL ERROR:", e.get("message"))
            return d.get("data")
    except urllib.error.HTTPError as e:
        print("  HTTP", e.code, e.read()[:300].decode())
        return None


def fetch_project(tok, org, number):
    d = gql(tok, """
    query($org: String!, $num: Int!) {
      organization(login: $org) {
        projectV2(number: $num) {
          id title number
          items(first: 100) { totalCount nodes {
            fieldValues(first: 25) { nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name field { ... on ProjectV2FieldCommon { name } } } } } } }
          fields(first: 40) { nodes {
            ... on ProjectV2FieldCommon { id name dataType }
            ... on ProjectV2SingleSelectField { id name dataType options { id name } } } }
        } } }""", {"org": org, "num": number})
    return ((d or {}).get("organization") or {}).get("projectV2")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=int, required=True, help="project number, e.g. 19")
    ap.add_argument("--org", default="FRC-1250")
    ap.add_argument("--apply", action="store_true", help="actually make changes")
    ap.add_argument("--force", action="store_true",
                    help="drop in-use Subsystem options anyway (DESTROYS those values)")
    ap.add_argument("--config", default=CONFIG)
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    tok = (json.load(open(SECRETS, encoding="utf-8")).get("github_token") or "").strip()
    tok = os.environ.get("GITHUB_TOKEN", tok)
    if not tok:
        sys.exit("No github_token in secrets.json (or GITHUB_TOKEN).")

    # Project options are UPPERCASE, matching the board's existing convention
    # (CHASSIS/INTAKE/SHOOTER/...). Case matters: 'Chassis' is a DIFFERENT option
    # from 'CHASSIS', so title-casing would destroy every existing value rather
    # than preserve the ones that already line up.
    want = [v.upper() for k, v in sorted((cfg.get("subsystems") or {}).items())
            if not k.startswith("_")]
    if not want:
        sys.exit("config.subsystems is empty -- nothing to apply.")

    p = fetch_project(tok, args.org, args.project)
    if not p:
        sys.exit("project #{} not found in {}".format(args.project, args.org))

    print("project #{}  {!r}  ({} items)".format(p["number"], p["title"],
                                                 p["items"]["totalCount"]))
    fields = {f["name"]: f for f in p["fields"]["nodes"] if f}

    # --- Subsystem options ------------------------------------------------
    sub = fields.get("Subsystem")
    if not sub:
        print("  no `Subsystem` field on this project")
    else:
        have = [o["name"] for o in sub.get("options", [])]
        print("\n  Subsystem now  :", have)
        print("  Subsystem want :", want)

        in_use = {}
        for it in p["items"]["nodes"]:
            for fv in it["fieldValues"]["nodes"]:
                if fv and (fv.get("field") or {}).get("name") == "Subsystem" and fv.get("name"):
                    in_use[fv["name"]] = in_use.get(fv["name"], 0) + 1
        dropped = {o: n for o, n in in_use.items() if o not in want}

        if dropped:
            print("\n  !! DESTRUCTIVE: {} item(s) use options that would be REMOVED:".format(
                sum(dropped.values())))
            for o, n in sorted(dropped.items(), key=lambda x: -x[1]):
                print("       {:<14} {} item(s) would lose their Subsystem".format(o, n))
            if not args.force:
                print("\n  refusing. Re-run with --force if you really mean it.")
                print("  (Never do this to a finished season's board -- there is no undo.)")
                return 1

        if args.apply:
            d = gql(tok, """
            mutation($f: ID!, $opts: [ProjectV2SingleSelectFieldOptionInput!]!) {
              updateProjectV2Field(input: {fieldId: $f, singleSelectOptions: $opts}) {
                projectV2Field { ... on ProjectV2SingleSelectField { options { name } } } } }""",
                     {"f": sub["id"],
                      "opts": [{"name": w, "color": "GRAY", "description": ""} for w in want]})
            res = ((d or {}).get("updateProjectV2Field") or {}).get("projectV2Field")
            print("\n  applied ->", [o["name"] for o in res["options"]] if res else "FAILED")
        else:
            print("\n  (report only -- pass --apply to change)")

    # --- link fields ------------------------------------------------------
    print()
    for name in LINK_FIELDS:
        if name in fields:
            print("  field {!r} already exists".format(name))
            continue
        if not args.apply:
            print("  field {!r} MISSING (would create)".format(name))
            continue
        d = gql(tok, """
        mutation($p: ID!, $n: String!) {
          createProjectV2Field(input: {projectId: $p, dataType: TEXT, name: $n}) {
            projectV2Field { ... on ProjectV2FieldCommon { name } } } }""",
                 {"p": p["id"], "n": name})
        ok = ((d or {}).get("createProjectV2Field") or {}).get("projectV2Field")
        print("  created field {!r}".format(name) if ok else "  FAILED to create {!r}".format(name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
