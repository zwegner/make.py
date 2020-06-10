def pat_subst(s, old, new):
    [prefix, _, suffix] = old.partition('%')
    parts = []
    for part in s.split():
        if part.startswith(prefix) and part.endswith(suffix):
            part = new.replace('%', part[len(prefix):-len(suffix)])
        parts.append(part)
    return ' '.join(parts)
