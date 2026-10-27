"""med.py — the code this seal is about."""


def median(xs):
    """The median of an odd-length list of distinct ints."""
    if len(xs) % 2 == 0:
        raise ValueError("median() is defined here for odd-length lists only")
    return sorted(xs, key=abs)[len(xs) // 2]
