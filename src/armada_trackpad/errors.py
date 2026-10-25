"""Domain errors, independent of the RPC layer."""


class Unavailable(Exception):  # noqa: N818 -- named after the wire error code `unavailable` it maps to
    """The requested operation cannot be done right now (no pads, bad wiring, touchscreen busy)."""
