"""What this controller speaks with nodes, as ranges, and how to tell whether a node can work with it.

Each side declares (min, max). They work together when the ranges overlap and then use the
highest number both speak. A new format arrives by raising max; the old one may be dropped
(min raised) one release later at the earliest, so a node one release behind still works (N-1).
"""
from __future__ import annotations

# Controller <-> node messages: node/protocol.go ProtocolMin..ProtocolVersion.
PROTOCOL = (1, 1)
# The mesh config.json a data plane reads: node/mesh/config.go ConfigSchemaMin..ConfigSchema.
CONFIG_SCHEMA = (2, 2)


def common(ours, theirs) -> int | None:
    """The highest number both ranges contain, or None when they do not overlap."""
    try:
        low, high = max(int(ours[0]), int(theirs[0])), min(int(ours[1]), int(theirs[1]))
    except (TypeError, ValueError, IndexError):
        return None
    return high if low <= high else None
