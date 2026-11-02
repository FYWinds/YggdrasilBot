"""Test QQ groups. Database tests key their rows by group and delete them by group, so no two tests may share one:
every test group id comes from ``fresh_group``, never a literal."""

from itertools import count

_ids = count(990_101)


def fresh_group() -> int:
    """A test group (990xxx) that nothing else in this run gets."""
    return next(_ids)
