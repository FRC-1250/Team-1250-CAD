"""Suggest the next free part number per subsystem.

    py next_number.py                 all subsystems
    py next_number.py --subsystem 1   just the chassis
    py next_number.py --subsystem 1 --assembly    next subassembly number

Costs ZERO Onshape quota -- it reads GitHub issues only.

WHY THIS EXISTS
  The old scheme used the GitHub issue number AS the part number, so assigning a
  number was automatic: open an issue, you have a number. It just couldn't group
  by subsystem, because issue numbers are one global sequence.

  The new scheme groups by subsystem, but the number is no longer handed to you --
  someone has to know that chassis is up to 102 and the next one is 103. That's
  what this prints.

  The workflow is unchanged in shape, just inverted:
      1. pick the next number here
      2. open an issue titled '1250-26B-103 Gearbox Plate'  <- work assigned
      3. do the work; name the drawing/part-studio tab '1250-26B-103'
      4. the exporter matches the issue by part number and fills in the links
  Step 2 still assigns work before anything exists in CAD. Nothing is lost.
"""

import argparse
import json
import os
import sys

import github_api
import identity

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
SECRETS = os.path.join(HERE, "secrets.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subsystem", type=int, help="limit to one subsystem number")
    ap.add_argument("--assembly", action="store_true",
                    help="suggest the next SUBASSEMBLY number (the A prefix)")
    ap.add_argument("--bot", help="bot letter, e.g. B (default: from config.part_prefix)")
    ap.add_argument("--config", default=CONFIG)
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    tok = None
    if os.path.exists(SECRETS):
        tok = (json.load(open(SECRETS, encoding="utf-8")).get("github_token") or "").strip() or None
    tok = os.environ.get("GITHUB_TOKEN", tok)

    owner, repo = cfg["output"]["repo_url"].rstrip("/").split("/")[-2:]
    gh = github_api.GitHub(owner, repo, tok)

    prefix = cfg.get("part_prefix") or {}
    team = prefix.get("team", "1250")
    yy = prefix.get("yy", "26")
    bot = (args.bot or prefix.get("bot") or "B").upper()

    # Scan every issue, open and closed. Closed still burns the number -- a part
    # that shipped and got obsoleted must not have its number reused.
    used = {}
    for i in gh.issues():
        ident = identity.parse(i["title"] or "")
        if not ident or ident.team != team or ident.yy != yy or ident.bot != bot:
            continue
        key = (ident.subsystem, ident.is_subassembly)
        used.setdefault(key, set()).add(int(ident.part))

    subs = ({str(args.subsystem): (cfg.get("subsystems") or {}).get(str(args.subsystem))}
            if args.subsystem else
            {k: v for k, v in (cfg.get("subsystems") or {}).items() if not k.startswith("_")})

    print("scanned {} issue(s) in {}/{} for {}-{}{}-*\n".format(
        len(gh.issues()), owner, repo, team, yy, bot))
    print("{:<4} {:<12} {:<26} {}".format("sub", "name", "used", "next free"))
    print("-" * 72)
    for num, name in sorted(subs.items(), key=lambda x: int(x[0])):
        n = int(num)
        for is_asm in ([True] if args.assembly else [False, True]):
            taken = used.get((n, is_asm), set())
            nxt = 1
            while nxt in taken:
                nxt += 1
            if nxt > 99:
                print("{:<4} {:<12} SUBSYSTEM FULL (01-99 all used)".format(n, name or "?"))
                continue
            ident_str = "{}-{}{}-{}{}{:02d}".format(team, yy, bot, "A" if is_asm else "", n, nxt)
            label = (name or "?") + (" (asm)" if is_asm else "")
            shown = ", ".join(str(x) for x in sorted(taken)[:8]) or "-"
            if len(taken) > 8:
                shown += ", ..."
            if taken or not is_asm:
                print("{:<4} {:<12} {:<26} {}".format(n, label, shown, ident_str))
    print("\nNumbers come from GitHub issue titles, so open the issue FIRST to claim")
    print("one -- that is also how work gets assigned before any CAD exists.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
