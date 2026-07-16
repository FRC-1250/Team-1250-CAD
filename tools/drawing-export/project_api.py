"""GitHub Projects (v2) writes. GraphQL only -- Projects v2 has no REST API.

Adds an issue to the season's project board and populates the fields we can
derive for free. Costs ZERO Onshape quota.

WHAT WE CAN FILL, AND WHAT WE CANNOT
  Link to PDF        the commit-pinned PDF in the repo        derivable
  Link to Drawing    the Onshape drawing tab                  derivable
  Link to Component  the subsystem assembly (config eid)      derivable
  Subsystem          from the part number                     derivable via config map
  ---------------------------------------------------------------------------
  Component Type     FDM/CNC/DWG/ASM -- a FABRICATION decision, not implied by a
                     drawing existing. 31 FDM parts last year had no drawing at all.
  Need / Produced    human quantities. Nothing in Onshape knows them.
  Status             human judgement about readiness.

We deliberately do not guess at those four. A wrong Subsystem or Status on the
board people fabricate from is worse than an empty cell someone fills in.
"""

import json
import urllib.error
import urllib.request

ONSHAPE_DOC = "https://cad.onshape.com/documents"


def drawing_tab_url(did, vid, eid):
    """Deep link to the drawing tab at the exact exported version."""
    return "{}/{}/v/{}/e/{}".format(ONSHAPE_DOC, did, vid, eid)


def component_url(did, vid, assembly_eid=None):
    """Source CAD. The subsystem assembly if config knows its element id.

    TODO (spec 10a-adjacent): resolving down to the specific PART STUDIO a drawing
    references would need GET /drawings/.../views (1 call per drawing) plus mapping
    `modelReferenceId` to an element -- expensive and unverified. The subsystem
    assembly is free and close enough for now.
    """
    if assembly_eid:
        return "{}/{}/v/{}/e/{}".format(ONSHAPE_DOC, did, vid, assembly_eid)
    return "{}/{}/v/{}".format(ONSHAPE_DOC, did, vid)


class Projects:
    def __init__(self, token, org):
        self.tok, self.org = token, org
        self._cache = {}

    def _gql(self, query, variables=None):
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request("https://api.github.com/graphql", data=body, method="POST")
        req.add_header("Authorization", "Bearer " + self.tok)
        req.add_header("User-Agent", "frc1250-drawing-export")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=45) as f:
                d = json.loads(f.read())
                if d.get("errors"):
                    raise RuntimeError("; ".join(e.get("message", "?") for e in d["errors"]))
                return d.get("data")
        except urllib.error.HTTPError as e:
            raise RuntimeError("GraphQL {}: {}".format(e.code, e.read()[:200].decode()))

    def project(self, number):
        """Project id + field metadata. Cached -- fields don't change mid-run."""
        if number in self._cache:
            return self._cache[number]
        d = self._gql("""
        query($org: String!, $num: Int!) {
          organization(login: $org) { projectV2(number: $num) {
            id title
            fields(first: 40) { nodes {
              ... on ProjectV2FieldCommon { id name dataType }
              ... on ProjectV2SingleSelectField { id name dataType options { id name } } } } } } }""",
                      {"org": self.org, "num": number})
        p = ((d or {}).get("organization") or {}).get("projectV2")
        if not p:
            raise RuntimeError("project #{} not found in {}".format(number, self.org))
        p["_fields"] = {f["name"]: f for f in p["fields"]["nodes"] if f}
        self._cache[number] = p
        return p

    def add_issue(self, project_number, issue_node_id):
        """Add an issue to the board. Idempotent -- returns the existing item if
        it's already there, rather than duplicating."""
        p = self.project(project_number)
        d = self._gql("""
        mutation($p: ID!, $c: ID!) {
          addProjectV2ItemById(input: {projectId: $p, contentId: $c}) { item { id } } }""",
                      {"p": p["id"], "c": issue_node_id})
        return d["addProjectV2ItemById"]["item"]["id"]

    def set_text(self, project_number, item_id, field_name, value):
        p = self.project(project_number)
        f = p["_fields"].get(field_name)
        if not f:
            return "field {!r} not on project".format(field_name)
        self._gql("""
        mutation($p: ID!, $i: ID!, $f: ID!, $v: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $p, itemId: $i, fieldId: $f, value: {text: $v}}) { projectV2Item { id } } }""",
                  {"p": p["id"], "i": item_id, "f": f["id"], "v": value})
        return None

    def set_single_select(self, project_number, item_id, field_name, option_name):
        """Set a single-select. Returns an error string if the option doesn't exist
        -- never silently picks a different one."""
        p = self.project(project_number)
        f = p["_fields"].get(field_name)
        if not f:
            return "field {!r} not on project".format(field_name)
        opts = {o["name"].upper(): o["id"] for o in f.get("options") or []}
        oid = opts.get((option_name or "").upper())
        if not oid:
            return "option {!r} not in {} (have: {})".format(
                option_name, field_name, sorted(opts))
        self._gql("""
        mutation($p: ID!, $i: ID!, $f: ID!, $v: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $p, itemId: $i, fieldId: $f,
            value: {singleSelectOptionId: $v}}) { projectV2Item { id } } }""",
                  {"p": p["id"], "i": item_id, "f": f["id"], "v": oid})
        return None
