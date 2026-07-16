"""Drawing identifier parsing.

Tab name -> part identifier, per the team convention:

    <team>-<YY><bot>-[A]<subsystem><part>

    1250-26B-101     team 1250, 2026 B-bot, subsystem 1 (chassis), part 01
    1250-26A-A503    subassembly drawing (A prefix), subsystem 5, drawing 03

The identifier GATES export (spec 4.8): drawings whose tab name does not match
are not exported at all. This is deliberate -- it enforces the numbering scheme.
Every rejection must therefore be logged; see export.py.
"""

import re

# .search() not .match(): names may carry trailing descriptions,
# e.g. '1250-26B-101 Gearbox Plate' or 'Chassis 1250-26B-1101'.
PATTERN = re.compile(
    r"(?P<id>"
    r"(?P<team>\d{3,5})-"
    r"(?P<yy>\d{2})"
    r"(?P<bot>[A-Z])-"
    r"(?P<asm>A?)"
    r"(?P<sn>\d{3,4})"
    r")"
)


class Identity:
    """A parsed drawing identifier."""

    __slots__ = ("id", "team", "yy", "bot", "is_subassembly", "subsystem", "part", "raw")

    def __init__(self, m, raw):
        self.id = m.group("id")
        self.team = m.group("team")
        self.yy = m.group("yy")
        self.bot = m.group("bot")
        self.is_subassembly = bool(m.group("asm"))
        sn = m.group("sn")
        # Part is assumed to be exactly the last 2 digits; subsystem is the rest.
        # KNOWN AMBIGUITY (spec 4.8): if part numbers can be 3 digits, '1101' is
        # ambiguous (subsystem 11 part 01 vs subsystem 1 part 101). Harmless for
        # matching -- .id is the key -- but wrong for .subsystem grouping.
        self.subsystem = int(sn[:-2])
        self.part = sn[-2:]
        self.raw = raw

    @property
    def description(self):
        """The human-readable remainder of the name, with the id removed.

            '1250-26B-102 Gearbox Plate' -> 'Gearbox Plate'
            'Chassis 1250-26B-1101'      -> 'Chassis'
            '1250-26B-102'               -> ''
        """
        rest = self.raw.replace(self.id, " ")
        return " ".join(rest.split()).strip(" -_:")

    @property
    def bot_folder(self):
        """Repo folder for this drawing, e.g. '1250-26B'.

        Derived from the DRAWING's own id, not its document, so A-bot and B-bot
        drawings sort correctly even when they share a document.
        """
        return "{}-{}{}".format(self.team, self.yy, self.bot)

    @property
    def filename(self):
        """PDF filename, e.g. '1250-26B-501.pdf'.

        Built from the identifier, never the raw tab name -- real names contain
        literal double quotes (inch marks: 'Tube 2"x1"x18.5" Drawing 1'), which
        are illegal in Windows filenames. Gating on the regex makes an invalid
        filename unconstructable rather than something to sanitize.

        No version suffix: git history is the version history.
        """
        return self.id + ".pdf"

    def __repr__(self):
        return "<Identity {}>".format(self.id)


def parse(name):
    """Return an Identity, or None if `name` does not follow the convention."""
    if not name:
        return None
    m = PATTERN.search(name)
    return Identity(m, name) if m else None
