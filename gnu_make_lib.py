################################################################################
## Begin gmake library functions ###############################################
################################################################################

import glob
import os

def addprefix(prefix, names):
    return ' '.join(prefix + x for x in names.split())

def addsuffix(suffix, names):
    return ' '.join(x + suffix for x in names.split())

def and_(*args):
    arg = ''
    for arg in args:
        arg = arg.strip()
        if not arg:
            return arg
    return arg

# Whether a string matches some patterns, for the $(filter) and $(filter-out) functions
def _match_filter(s, patterns):
    for pat in patterns:
        # Deal with %, which is the only special character, and only permitted once
        if '%' in pat:
            [pre, _, suf] = pat.partition('%')
            if s.startswith(pre) and s.endswith(suf):
                return True
        elif s == pat:
            return True
    return False

def filter(pattern, text):
    pattern = pattern.split()
    return ' '.join(s for s in text.split() if _match_filter(s, pattern))

def filter_out(pattern, text):
    pattern = pattern.split()
    return ' '.join(s for s in text.split() if not _match_filter(s, pattern))

def findstring(pattern, text):
    return pattern if pattern in text else ''

def notdir(arg):
    return ' '.join(os.path.split(v)[1] for v in arg.split())

def or_(*args):
    for arg in args:
        arg = arg.strip()
        if arg:
            return arg
    return ''

def patsubst(old, new, s):
    [prefix, _, suffix] = old.partition('%')
    parts = []
    for part in s.split():
        if part.startswith(prefix) and part.endswith(suffix):
            part = new.replace('%', part[len(prefix):-len(suffix) or None], 1)
        parts.append(part)
    return ' '.join(parts)

def realpath(arg):
    return os.path.realpath(arg)

def sort(arg):
    return ' '.join(sorted(set(arg.split())))

def strip(arg):
    return ' '.join(arg.split())

def subst(old, new, s):
    return s.replace(old, new)

def wildcard(arg):
    return ' '.join(sorted(glob.glob(arg)))

################################################################################
## End gmake library functions #################################################
################################################################################
