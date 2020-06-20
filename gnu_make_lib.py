import glob
import os
import shlex

# Utility functions (not exposed as make functions)

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

def _split_cmd(text):
    if not isinstance(text, str):
        return text
    s = shlex.shlex(text, punctuation_chars=True, posix=True)
    # Allow % in the middle of words, since here it's generally a glob pattern
    s.wordchars += '%'
    return list(s)

def _split_cmds(cmds):
    new_cmds = []
    for cmd in cmds:
        if not isinstance(cmd, list):
            cmd = _split_cmd(cmd)
        if isinstance(cmd, list):
            # Check for shell syntax that we can't handle
            # XXX We should be more strict really. And when we find it, should
            # pass to 'sh'
            for token in ['&&', '||', '|', '<', '>']:
                if token in cmd:
                    assert False, ('unsupported shell syntax in command: '
                            '%s in %s' % (token, cmd))

            # Split sub-commands by semicolon
            while ';' in cmd:
                idx = cmd.index(';')
                new_cmds.append(cmd[:idx])
                cmd = cmd[idx+1:]

        new_cmds.append(cmd)

    return new_cmds

# GNU Make function library

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
