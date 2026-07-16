"""Map Onshape user ids -> GitHub logins, for attributing drawings in issue comments.

    py reconcile_users.py                 list BOTH sides; you do the matching
    py reconcile_users.py --write         apply exact matches, record suggestions
    py reconcile_users.py --from-fixture  read company users from a capture, 0 API calls

Costs 1 Onshape call (`GET /companies/{cid}/users`) -- or ZERO with --from-fixture.

Why matching is manual: most GitHub accounts active on Team-1250-CAD have no
`name` set, so there is nothing to match an Onshape display name against. Exact
name matches are applied automatically. Everything else is a SUGGESTION and is
never auto-applied -- a wrong @mention pings the wrong student, which is worse
than no mention at all.

To spend nothing:
    Explorer (session auth -- do NOT click Authorize) -> Companies -> getCompanyUsers
    cid=<onshape.company_id>. Save the response to fixtures/company_users.json:
        py reconcile_users.py --from-fixture
"""

import argparse
import json
import os
import sys

import github_api
import osapi
import store

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
SECRETS = os.path.join(HERE, "secrets.json")
FIXTURE = os.path.join(HERE, "fixtures", "company_users.json")


def norm(s):
    """Loose name key: casefold, strip punctuation, collapse whitespace."""
    if not s:
        return ""
    keep = [c.lower() for c in s if c.isalnum() or c.isspace()]
    return " ".join("".join(keep).split())


def suggest(onshape_name, gh_users):
    """Conservative candidates. Advisory only -- never auto-applied."""
    out = []
    parts = norm(onshape_name).split()
    if not parts:
        return out
    first, last = parts[0], parts[-1]
    for u in gh_users:
        login = u["login"].lower()
        gname = norm(u.get("name"))
        if gname and gname == norm(onshape_name):
            continue  # exact match, handled elsewhere
        why = None
        if len(first) >= 3 and login.startswith(first):
            why = "login starts with {!r}".format(first)
        elif len(last) >= 3 and last in login:
            why = "login contains {!r}".format(last)
        elif gname and first in gname.split():
            why = "GitHub name shares first name"
        if why:
            out.append((u["login"], why))
    return out


def user_identity(rec):
    """(id, name) from a BTCompanyUserInfo.

    `id` on that object is the COMPANY-MEMBERSHIP id, not the user's -- the real
    user id is nested under `user`. Getting this wrong yields None and silently
    breaks the mapping key.
    """
    u = rec.get("user") or {}
    return u.get("id") or rec.get("id"), rec.get("name") or u.get("name")


def seen_creators(db_path):
    """Onshape users observed as version creators, from state.db. Free.

    This is the source that MATTERS. The company roster misses anyone who
    accesses documents by sharing rather than membership -- which is most of the
    team. Hunter Mackler created chassis V13-V16 and is not a company user.
    """
    if not os.path.exists(db_path):
        return []
    st = store.Store(db_path)
    rows = st.db.execute(
        "SELECT DISTINCT creator_id, creator_name FROM drawing_state "
        "WHERE creator_id IS NOT NULL"
    ).fetchall()
    st.close()
    return [{"id": r["creator_id"], "name": r["creator_name"], "_src": "version creator"}
            for r in rows]


def onshape_users(cfg, args):
    """Every user in the company. From a fixture (free) or one live call."""
    if args.from_fixture:
        if not os.path.exists(FIXTURE):
            sys.exit(
                "No {}.\n"
                "  Capture it free from the Explorer (session auth; do NOT Authorize):\n"
                "    Companies -> getCompanyUsers -> cid={}\n"
                "  Save the response there, then re-run.".format(
                    os.path.relpath(FIXTURE, HERE), cfg["onshape"]["company_id"]))
        data = json.load(open(FIXTURE, encoding="utf-8"))
        return data.get("items", data) if isinstance(data, dict) else data

    ak = os.environ.get("ONSHAPE_ACCESS_KEY")
    sk = os.environ.get("ONSHAPE_SECRET_KEY")
    if not (ak and sk) and os.path.exists(SECRETS):
        s = json.load(open(SECRETS, encoding="utf-8"))
        ak, sk = s.get("access_key"), s.get("secret_key")
    if not (ak and sk):
        sys.exit("No Onshape credentials. Use --from-fixture, or fill in secrets.json.")

    st = store.Store(args.db)
    client = osapi.Client(cfg, st, "reconcile", access_key=ak, secret_key=sk)
    print("querying Onshape company users (1 API call) ...")
    try:
        r = client._request(
            "GET", "/companies/{}/users".format(cfg["onshape"]["company_id"]),
            {"limit": 100})
    finally:
        st.close()
    return r.get("items", r) if isinstance(r, dict) else r


def github_users(cfg):
    owner, repo = cfg["output"]["repo_url"].rstrip("/").split("/")[-2:]
    token = None
    if os.path.exists(SECRETS):
        token = (json.load(open(SECRETS, encoding="utf-8")).get("github_token") or "").strip() or None
    gh = github_api.GitHub(owner, repo, os.environ.get("GITHUB_TOKEN", token))
    logins = {i["user"]["login"] for i in gh.issues() if i.get("user")}
    out = []
    for login in sorted(logins):
        u, _ = gh._request("GET", "/users/{}".format(login))
        out.append({"login": u["login"], "name": u.get("name")})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="apply exact matches + record suggestions")
    ap.add_argument("--from-fixture", action="store_true",
                    help="read company users from fixtures/company_users.json (0 API calls)")
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--db", default="state.db")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))

    # Company roster (1 call, or free from fixture) UNION version creators (free).
    # The union matters: only 2 people are company members, but drawings are
    # created by others who access documents via sharing. Attribution needs them.
    roster = []
    for rec in onshape_users(cfg, args):
        oid, oname = user_identity(rec)
        flags = [k for k in ("admin", "guest", "light") if rec.get(k)]
        roster.append({"id": oid, "name": oname, "_src": "company member",
                       "_flags": ",".join(flags) or "-"})
    by_id = {u["id"]: u for u in roster if u["id"]}
    for c in seen_creators(args.db):
        if c["id"] not in by_id:
            c["_flags"] = "-"
            roster.append(c)
            by_id[c["id"]] = c

    os_users = roster
    gh_users = github_users(cfg)

    named = sum(1 for u in gh_users if u.get("name"))
    print("\n=== Onshape users ({}) ===".format(len(os_users)))
    for u in os_users:
        print("  {:<26} {:<26} [{}] {}".format(
            u.get("name") or "(no name)", u.get("id") or "(NO ID)",
            u.get("_src"), u.get("_flags", "")))
    if not any(u.get("_src") == "version creator" for u in os_users):
        print("  note: no version creators recorded yet -- run export.py once so")
        print("        drawing authors (who may not be company members) appear here.")

    print("\n=== GitHub users active on repo ({}; {} with a name set) ===".format(
        len(gh_users), named))
    for u in gh_users:
        print("  {:<28} {}".format(u["login"], u.get("name") or "(no name on GitHub)"))

    existing = {k: v for k, v in cfg["users"]["map"].items() if not k.startswith("_")}
    by_name = {norm(u["name"]): u["login"] for u in gh_users if u.get("name")}

    print("\n=== proposed mapping ===")
    exact, sugg, unresolved = {}, {}, []
    for u in os_users:
        oid, oname = u.get("id"), u.get("name")
        if not oid:
            continue
        if oid in existing:
            print("  MAPPED     {:<28} -> {}".format(oname, existing[oid]))
            continue
        hit = by_name.get(norm(oname))
        if hit:
            exact[oid] = hit
            print("  EXACT      {:<28} -> {}".format(oname, hit))
            continue
        cands = suggest(oname, gh_users)
        if cands:
            sugg[oid] = {"onshape_name": oname,
                         "candidates": [{"login": l, "why": w} for l, w in cands]}
            print("  SUGGEST    {:<28} -> {}".format(
                oname, ", ".join("{} ({})".format(l, w) for l, w in cands)))
        else:
            unresolved.append((oid, oname))
            print("  UNRESOLVED {:<28} -- no candidate".format(oname))

    if sugg or unresolved:
        print("\n" + "=" * 72)
        print("PASTE INTO config.json -> users.map   (verify every line):")
        print()
        for oid, s in sugg.items():
            print('    "{}": "{}",   // {}  <- SUGGESTED, verify'.format(
                oid, s["candidates"][0]["login"], s["onshape_name"]))
        for oid, oname in unresolved:
            print('    "{}": "GITHUB_LOGIN_HERE",   // {}'.format(oid, oname))
        print()
        print("Unmapped users fall back to users.unresolved_policy = {!r}".format(
            cfg["users"]["unresolved_policy"]))
        print("=" * 72)

    if args.write:
        cfg["users"]["map"].update(exact)
        cfg["users"]["suggestions"] = {
            "_comment": cfg["users"]["suggestions"].get("_comment", ""), **sugg}
        with open(args.config, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        print("\nwrote {}: {} exact applied, {} suggestion(s) recorded (NOT active)".format(
            os.path.basename(args.config), len(exact), len(sugg)))
    elif exact or sugg:
        print("\n(report only -- re-run with --write to apply exact matches)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
