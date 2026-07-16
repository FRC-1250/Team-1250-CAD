"""Stage B: PDF -> git commit -> GitHub issue comment.

Decoupled from Stage A by design (spec 5b). Stage A is quota-constrained and
fragile; this side is effectively free and retryable. SQLite is the seam -- this
re-runs freely against PDFs already paid for, and a GitHub failure can never burn
Onshape quota.

    py publish.py --dry-run          show what would happen, touch nothing
    py publish.py                    commit PDFs + comment on matched issues
    py publish.py --create-issues    also OPEN issues for unmatched parts

Auto-create is OFF by default. Creating issues is outward-facing, visible to the
whole team, and awkward to undo. As of 2026-07-16 no issue body contains a
1250-26B-style part number, so EVERY drawing is currently unmatched -- enabling
this blind would open one issue per drawing, some duplicating existing ones
(e.g. #91 'Chassis Tubes' already covers the chassis tube drawings).
Run once without it, read the report, then decide.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

import github_api
import identity
import project_api
import store

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
SECRETS = os.path.join(HERE, "secrets.json")


def resolve(path):
    """Config paths are relative to this file, so a fresh clone just works."""
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(HERE, path))


def git(repo, *args, check=True):
    p = subprocess.run(
        ["git"] + list(args), cwd=repo, capture_output=True, text=True
    )
    if check and p.returncode != 0:
        raise RuntimeError("git {}: {}".format(" ".join(args), p.stderr.strip()))
    return p.stdout.strip()


def file_on_project(cfg, projects, issue_node_id, ident, doc, blob_url, version_id,
                    element_id, kind):
    """Add the issue to the season's board and fill the derivable columns.

    Returns (item_id, [notes]). Notes are non-fatal problems worth printing --
    a missing field or option is reported, never silently skipped, and never
    guessed around.
    """
    pcfg = cfg.get("project") or {}
    num = pcfg.get("number")
    notes = []
    item = projects.add_issue(num, issue_node_id)

    did, vid = doc["did"], version_id
    is_drawing = kind == "drawing"

    values = {
        # A part studio has no drawing, so no PDF and no drawing tab -- only the
        # component link. Writing empty strings would look like a broken link.
        "Link to PDF": blob_url if is_drawing else None,
        "Link to Drawing": project_api.drawing_tab_url(did, vid, element_id) if is_drawing else None,
        "Link to Component": (project_api.drawing_tab_url(did, vid, element_id) if not is_drawing
                              else project_api.component_url(did, vid, doc.get("assembly_eid"))),
    }
    for field, val in values.items():
        if not val:
            continue
        err = projects.set_text(num, item, field, val)
        if err:
            notes.append(err)

    sub = (cfg.get("subsystems") or {}).get(str(ident.subsystem))
    if sub:
        err = projects.set_single_select(num, item, "Subsystem", sub)
        if err:
            notes.append(err)
    else:
        notes.append("no subsystem name configured for {}".format(ident.subsystem))

    # Component Type: derivable ONLY for drawings, and only because the numbering
    # says so -- the `A` prefix marks a subassembly. "If we made a drawing for it,
    # it should be tagged FAB for parts and ASSY for assemblies" -> DWG / ASM.
    #
    # For a part studio we set NOTHING: FDM vs CNC is a fabrication decision and
    # nothing in Onshape distinguishes a part bound for a printer from one bound
    # for a mill. Guessing would misfile it on the board people build from.
    if is_drawing:
        ctype = "ASM" if ident.is_subassembly else "DWG"
        err = projects.set_single_select(num, item, "Component Type", ctype)
        if err:
            notes.append(err)
    else:
        notes.append("Component Type left blank -- pick FDM or CNC by hand")

    # Need / Produced / Status stay empty: human quantities and judgement.
    return item, notes


def author_label(cfg, creator_id, creator_name):
    """Who to credit. Local lookup -- no API call.

    Mapped   -> '@login' (pings them).
    Unmapped -> the plain Onshape name: informative, but never pings the wrong
                person. See users.unresolved_policy for 'fallback'/'subsystem_owner'.
    """
    u = cfg.get("users") or {}
    m = {k: v for k, v in (u.get("map") or {}).items() if not k.startswith("_")}
    login = m.get(creator_id)
    if login:
        return "@" + login
    policy = u.get("unresolved_policy", "none")
    if policy == "fallback" and u.get("fallback_login"):
        return "@" + u["fallback_login"]
    if policy == "subsystem_owner":
        return None  # resolved by caller, which knows the subsystem
    return creator_name or None


def subsystem_label(cfg, n):
    """'1 - Chassis'. Local lookup, no API call.

    Explicit config.subsystems wins; otherwise fall back to the document `key`
    (which doubles as the --doc CLI arg, so it may not be the name you want to
    show); otherwise the bare number.
    """
    explicit = (cfg.get("subsystems") or {}).get(str(n))
    if explicit:
        return "{} - {}".format(n, explicit)
    for d in cfg.get("documents") or []:
        if d.get("subsystem") == n and d.get("key"):
            return "{} - {}".format(n, d["key"].title())
    return str(n)


def comment_body(cfg, ident, blob_url, version_name, doc_name, author=None):
    by = "\n| Drawn by | {} |".format(author) if author else ""
    return (
        "**Drawing PDF updated** -- `{id}`\n\n"
        "| | |\n|---|---|\n"
        "| Part | `{id}` |\n"
        "| Subsystem | {sub} |{by}\n"
        "| Onshape document | {doc} |\n"
        "| Version | {ver} |\n\n"
        "[View PDF]({url})\n\n"
        "<sub>Posted automatically by `tools/drawing-export`. The link is pinned to "
        "the commit, so it always shows this version of the drawing.</sub>"
    ).format(id=ident.id, sub=subsystem_label(cfg, ident.subsystem), by=by,
             doc=doc_name, ver=version_name, url=blob_url)


def issue_body(cfg, ident, blob_url, author=None):
    by = " Last versioned by {}.".format(author) if author else ""
    return (
        "### Component Description\n\n"
        "_Auto-created by `tools/drawing-export` for part `{id}`, which had a drawing "
        "but no tracking issue._\n\n"
        "Subsystem **{sub}**.{by} Please fill in a real description.\n\n"
        "### Notes / Links\n\n"
        "[Drawing PDF]({url})\n\n"
        "<sub>The part number `{id}` is in this issue's title so future drawing "
        "exports link here automatically.</sub>"
    ).format(id=ident.id, sub=subsystem_label(cfg, ident.subsystem), by=by, url=blob_url)


def _file(cfg, projects, docs_by_key, r, ident, blob, node_id, issue_no):
    """Add to the project board, if enabled. Never fatal -- the PDF is committed
    and the issue is commented regardless; the board is a bonus, not a gate."""
    if not projects or not node_id:
        return
    doc = docs_by_key.get(r["document_key"]) or {"did": r["document_id"]}
    try:
        _, notes = file_on_project(cfg, projects, node_id, ident, doc, blob,
                                   r["version_id"], r["element_id"],
                                   r["element_kind"] or "drawing")
        pnum = (cfg.get("project") or {}).get("number")
        print("       filed on project #{} (links + subsystem)".format(pnum))
        for n in notes:
            print("       NOTE: {}".format(n))
    except Exception as e:
        print("       project filing FAILED (issue #{} is still linked): {}".format(issue_no, e))


def main():
    ap = argparse.ArgumentParser(description="Publish exported PDFs to GitHub.")
    ap.add_argument("--dry-run", action="store_true", help="report only; touch nothing")
    ap.add_argument("--create-issues", action="store_true",
                    help="OPEN issues for unmatched parts (off by default -- read the docstring)")
    ap.add_argument("--push", action="store_true", help="push the commit (default: commit locally only)")
    ap.add_argument("--config", default=CONFIG,
                    help="alternate config, e.g. config.sandbox.json (spec 5b.5)")
    ap.add_argument("--db", default="state.db")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    repo = resolve(cfg["output"]["repo_path"])
    owner, name = cfg["output"]["repo_url"].rstrip("/").split("/")[-2:]

    token = None
    if os.path.exists(SECRETS):
        token = (json.load(open(SECRETS, encoding="utf-8")).get("github_token") or "").strip() or None
    token = os.environ.get("GITHUB_TOKEN", token)

    st = store.Store(args.db)
    rows = st.db.execute(
        "SELECT ds.element_id, ds.source_id, e.output_path, ds.identifier, ds.version_name, "
        "       ds.document_name, ds.element_name, ds.creator_id, ds.creator_name, "
        "       ds.document_key, ds.version_id, ds.document_id, ds.element_kind "
        "FROM drawing_state ds "
        "LEFT JOIN export e "
        "  ON ds.element_id=e.element_id AND ds.source_id=e.source_id "
        "LEFT JOIN publish p "
        "  ON p.element_id=ds.element_id AND p.source_id=ds.source_id "
        "WHERE (e.status='DONE' OR ds.element_kind='partstudio') "
        "  AND (p.status IS NULL OR p.status NOT IN ('LINKED','COMMITTED')) "
    ).fetchall()

    if not rows:
        print("nothing to publish (no DONE exports awaiting publication)")
        return 0

    print("{} PDF(s) to publish".format(len(rows)))
    if not os.path.isdir(os.path.join(repo, ".git")):
        sys.exit("repo not found at {} -- clone it first".format(repo))

    gh = github_api.GitHub(owner, name, token)
    pcfg = cfg.get("project") or {}
    projects = (project_api.Projects(token, pcfg.get("org", owner))
                if pcfg.get("enabled") and token else None)
    docs_by_key = {d["key"]: d for d in (cfg.get("documents") or [])}
    if not args.dry_run and not token:
        sys.exit(
            "No GitHub token.\n"
            '  Add "github_token": "ghp_..." to secrets.json (gitignored),\n'
            "  or set GITHUB_TOKEN. Needs repo scope. Or pass --dry-run."
        )

    # --- stage files -------------------------------------------------------
    staged, plan = [], []
    for r in rows:
        ident = identity.parse(r["identifier"] or "")
        if ident is None:
            print("  WARN: stored identifier {!r} no longer parses; skipping".format(r["identifier"]))
            continue
        rel = "{}/{}".format(ident.bot_folder, ident.filename)
        dest = os.path.join(repo, ident.bot_folder, ident.filename)
        # output_path is relative to output.root. Tolerate absolute paths written
        # by older versions, but fall back to deriving it -- a stale absolute path
        # from a moved tree must not fail a PDF that exists.
        stored = r["output_path"] or ""
        src = stored if os.path.isabs(stored) else os.path.join(resolve(cfg["output"]["root"]), stored)
        if not os.path.exists(src):
            derived = os.path.join(resolve(cfg["output"]["root"]), rel)
            if os.path.exists(derived):
                print("  note: stored path {!r} missing; using {}".format(stored, derived))
                src = derived
            else:
                print("  SKIP {}: PDF not found (looked in {} and {})".format(
                    ident.id, stored, derived))
                continue
        hits = gh.find_by_identifier(ident.id)
        plan.append((r, ident, rel, dest, hits, src))

    print("\n=== plan ===")
    for r, ident, rel, dest, hits, _src in plan:
        where = ("issue #{}".format(hits[0]["number"]) if len(hits) == 1
                 else "AMBIGUOUS: {}".format([h["number"] for h in hits]) if hits
                 else ("CREATE new issue" if args.create_issues else "NO MATCH -- no issue, PDF still committed"))
        print("  {}  ->  {}   [{}]".format(ident.id, rel, where))

    unmatched = [p for p in plan if not p[4]]
    if unmatched and not args.create_issues:
        print("\n  {} part(s) have no matching issue.".format(len(unmatched)))
        print("  Their PDFs will still be committed and linkable -- only the comment is skipped.")
        print("  To open issues for them, re-run with --create-issues (read publish.py's docstring first:")
        print("  no issue currently contains a 1250-26B-style number, so nothing will match yet).")

    if args.dry_run:
        print("\ndry-run: nothing written.")
        return 0

    # --- commit ------------------------------------------------------------
    for r, ident, rel, dest, hits, src in plan:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        git(repo, "add", rel)
        staged.append((r, ident, rel, hits))

    # Committing and linking are not atomic. A run interrupted between them (a
    # hung push, ctrl-C, a 500 from GitHub) leaves the PDF committed with no
    # publish row -- and an early return here would strand it forever, because
    # the next run also finds "nothing to commit". So: if there is nothing new to
    # commit, fall through to linking against HEAD rather than bailing out.
    if git(repo, "status", "--porcelain"):
        msg = "Add drawing PDFs: {}".format(", ".join(i.id for _, i, _, _ in staged))
        git(repo, "commit", "-m", msg)
        sha = git(repo, "rev-parse", "HEAD")
        print("\ncommitted {} ({} file(s))".format(sha[:8], len(staged)))
    else:
        sha = git(repo, "rev-parse", "HEAD")
        print("\nnothing new to commit; PDFs already in the repo at {}".format(sha[:8]))
        print("(continuing to the linking step -- a previous run may have been interrupted)")

    if args.push:
        git(repo, "push")
        print("pushed to {}".format(cfg["output"]["repo_url"]))
    else:
        print("NOT pushed (use --push). Commit-pinned links are only valid once pushed.")

    # --- link --------------------------------------------------------------
    for r, ident, rel, hits in staged:
        blob = "{}/blob/{}/{}".format(cfg["output"]["repo_url"].rstrip("/"), sha, rel)
        try:
            if len(hits) == 1:
                author = author_label(cfg, r["creator_id"], r["creator_name"])
                url = gh.comment(hits[0]["number"], comment_body(
                    cfg, ident, blob, r["version_name"], r["document_name"], author))
                st.db.execute(
                    "INSERT OR REPLACE INTO publish(element_id,source_id,format,status,identifier,"
                    "repo_path,commit_sha,blob_url,issue_number,comment_url,published_at) "
                    "VALUES(?,?,'PDF','LINKED',?,?,?,?,?,?,?)",
                    (r["element_id"], r["source_id"], ident.id, rel, sha, blob,
                     hits[0]["number"], url, store.now()),
                )
                print("  {} -> commented on #{}".format(ident.id, hits[0]["number"]))
                _file(cfg, projects, docs_by_key, r, ident, blob, hits[0].get("node_id"),
                      hits[0]["number"])
            elif len(hits) > 1:
                st.db.execute(
                    "INSERT OR REPLACE INTO publish(element_id,source_id,format,status,identifier,"
                    "repo_path,commit_sha,blob_url,last_error,published_at) "
                    "VALUES(?,?,'PDF','AMBIGUOUS',?,?,?,?,?,?)",
                    (r["element_id"], r["source_id"], ident.id, rel, sha, blob,
                     "matches issues {}".format([h["number"] for h in hits]), store.now()),
                )
                print("  {} -> AMBIGUOUS ({}); commented on none".format(
                    ident.id, [h["number"] for h in hits]))
            elif args.create_issues:
                # The drawing's tab name already IS "<id> <description>" -- that is
                # the convention. Using it directly keeps the human's description
                # and guarantees the id is in the title (so the next run matches).
                # Building it from id + filename-stem duplicated the id, because
                # the stem is the id: "1250-26B-102 1250-26B-102".
                title = (r["element_name"] or ident.id).strip()
                if ident.id not in title:
                    title = "{} {}".format(ident.id, title)
                author = author_label(cfg, r["creator_id"], r["creator_name"])
                num, url, node = gh.create_issue(title, issue_body(cfg, ident, blob, author))
                st.db.execute(
                    "INSERT OR REPLACE INTO publish(element_id,source_id,format,status,identifier,"
                    "repo_path,commit_sha,blob_url,issue_number,comment_url,published_at) "
                    "VALUES(?,?,'PDF','LINKED',?,?,?,?,?,?,?)",
                    (r["element_id"], r["source_id"], ident.id, rel, sha, blob, num, url, store.now()),
                )
                print("  {} -> created issue #{}".format(ident.id, num))
                _file(cfg, projects, docs_by_key, r, ident, blob, node, num)
            else:
                st.db.execute(
                    "INSERT OR REPLACE INTO publish(element_id,source_id,format,status,identifier,"
                    "repo_path,commit_sha,blob_url,published_at) "
                    "VALUES(?,?,'PDF','COMMITTED',?,?,?,?,?)",
                    (r["element_id"], r["source_id"], ident.id, rel, sha, blob, store.now()),
                )
                print("  {} -> committed, no issue matched".format(ident.id))
            st.db.commit()
        except github_api.GitHubError as e:
            print("  {} -> FAILED: {}".format(ident.id, e))
    st.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
