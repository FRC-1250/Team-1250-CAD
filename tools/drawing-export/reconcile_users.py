"""Map Onshape user ids -> GitHub logins, for attributing drawings in issue comments.

    py reconcile_users.py             report only
    py reconcile_users.py --write     apply exact matches, write suggestions to config

Costs ZERO Onshape quota -- Onshape identities come from state.db (already paid for
during export). GitHub reads are free and unauthenticated for public repos.

Why this is mostly manual: of the 7 GitHub accounts active on Team-1250-CAD,
4 have no `name` set at all. There is nothing to match an Onshape display name
against, so exact matching resolves only the couple of people who filled in their
profile. Suggestions are offered but NEVER auto-applied -- a wrong @mention pings
the wrong student, which is worse than no mention.
"""

import argparse
import json
import os
import sys

import github_api
import store

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
SECRETS = os.path.join(HERE, "secrets.json")


def norm(s):
    """Loose name key: casefold, drop punctuation/quotes/nicknames."""
    if not s:
        return ""
    keep = [c.lower() for c in s if c.isalnum() or c.isspace()]
    return " ".join("".join(keep).split())


def suggest(onshape_name, gh_users):
    """Heuristic candidates. Deliberately conservative, and advisory only."""
    out = []
    parts = norm(onshape_name).split()
    if not parts:
        return out
    first, last = parts[0], parts[-1]
    for u in gh_users:
        login = u["login"].lower()
        gname = norm(u.get("name"))
        why = None
        if gname and gname == norm(onshape_name):
            continue  # exact -- handled elsewhere
        if login.startswith(first) and len(first) >= 3:
            why = "login starts with first name {!r}".format(first)
        elif last and len(last) >= 3 and last in login:
            why = "login contains last name {!r}".format(last)
        elif gname and first in gname.split():
            why = "GitHub name shares first name"
        if why:
            out.append((u["login"], why))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="apply exact matches + record suggestions")
    ap.add_argument("--db", default="state.db")
    args = ap.parse_args()

    cfg = json.load(open(CONFIG, encoding="utf-8"))
    owner, repo = cfg["output"]["repo_url"].rstrip("/").split("/")[-2:]
    token = None
    if os.path.exists(SECRETS):
        token = (json.load(open(SECRETS, encoding="utf-8")).get("github_token") or "").strip() or None
    gh = github_api.GitHub(owner, repo, os.environ.get("GITHUB_TOKEN", token))

    # --- Onshape identities: free, already in state.db --------------------
    st = store.Store(args.db)
    people = st.db.execute(
        "SELECT DISTINCT creator_id, creator_name FROM drawing_state "
        "WHERE creator_id IS NOT NULL"
    ).fetchall()
    st.close()

    if not people:
        print("No Onshape creators in state.db yet.")
        print("They are captured during export (from the version's creator), so run")
        print("export.py at least once after this schema was added.")
        return 0

    # --- GitHub identities: free ------------------------------------------
    print("fetching GitHub users active on {}/{} ...".format(owner, repo))
    logins = set()
    for i in gh.issues():
        if i.get("user"):
            logins.add(i["user"]["login"])
    gh_users = []
    for login in sorted(logins):
        u, _ = gh._request("GET", "/users/{}".format(login))
        gh_users.append({"login": u["login"], "name": u.get("name")})

    named = sum(1 for u in gh_users if u.get("name"))
    print("  {} GitHub accounts, {} with a `name` set ({} anonymous)\n".format(
        len(gh_users), named, len(gh_users) - named))

    by_name = {norm(u["name"]): u["login"] for u in gh_users if u.get("name")}
    existing = {k: v for k, v in cfg["users"]["map"].items() if not k.startswith("_")}

    exact, sugg, unresolved = {}, {}, []
    for p in people:
        oid, oname = p["creator_id"], p["creator_name"]
        if oid in existing:
            print("  MAPPED    {:<24} -> {}".format(oname or oid, existing[oid]))
            continue
        hit = by_name.get(norm(oname))
        if hit:
            exact[oid] = hit
            print("  EXACT     {:<24} -> {}   (GitHub name matches)".format(oname, hit))
            continue
        cands = suggest(oname, gh_users)
        if cands:
            sugg[oid] = {"onshape_name": oname,
                         "candidates": [{"login": l, "why": w} for l, w in cands]}
            print("  SUGGEST   {:<24} -> {}".format(
                oname, ", ".join("{} ({})".format(l, w) for l, w in cands)))
        else:
            unresolved.append((oid, oname))
            print("  UNRESOLVED {:<23} -- no candidate".format(oname))

    if unresolved or sugg:
        print("\n" + "=" * 68)
        print("ACTION NEEDED -- {} unmapped Onshape user(s)".format(len(unresolved) + len(sugg)))
        print("Add confirmed pairs to config.json -> users.map:")
        print()
        for oid, oname in unresolved:
            print('    "{}": "GITHUB_LOGIN_HERE",     // {}'.format(oid, oname))
        for oid, s in sugg.items():
            best = s["candidates"][0]["login"]
            print('    "{}": "{}",     // {}  <- SUGGESTED, verify'.format(oid, best, s["onshape_name"]))
        print()
        print("Until mapped, comments use users.unresolved_policy = {!r}".format(
            cfg["users"]["unresolved_policy"]))
        print("=" * 68)

    if args.write:
        cfg["users"]["map"].update(exact)
        cfg["users"]["suggestions"] = {
            "_comment": cfg["users"]["suggestions"]["_comment"], **sugg}
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        print("\nwrote config.json: {} exact match(es) applied, {} suggestion(s) recorded".format(
            len(exact), len(sugg)))
        if sugg:
            print("Suggestions are NOT active. Move them into users.map after checking.")
    elif exact or sugg:
        print("\n(report only -- re-run with --write to apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
