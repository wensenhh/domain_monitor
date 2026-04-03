import re


def clean_domain(value) -> str:
    s = "" if value is None else str(value)
    s = s.strip()
    while True:
        old = s
        s = s.strip().strip(",")
        if len(s) >= 2 and s[0] == s[-1] and s[0] in {"`", "'", '"'}:
            s = s[1:-1].strip()
        s = s.strip().strip(",")
        if s == old:
            break
    s = re.sub(r"\s+", "", s)
    s = s.rstrip("/").rstrip(".")
    return s
