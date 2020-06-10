import io
import subprocess
import sys
import tempfile
import traceback

import gnu_make_parse

PASSES = FAILS = 0

def test(*args, **kwargs):
    global PASSES, FAILS
    try:
        inner_test(*args, **kwargs)
        PASSES += 1
    except Exception:
        # Wow, what a bunch of horseshit. I want to print the traceback of the caught
        # exception, with all frames up to the root of the stack. The traceback.print_exc()
        # function stops at the caller's frame (i.e. here), which is basically completely
        # useless for us. Apparently this is not very important functionality, as there
        # is a Python bug open since 2006: https://bugs.python.org/issue1553375
        # So anyways, here's a hacky shitty way to get what we want. Maybe there's a better
        # way, but this works, so oh well.
        [etype, value, tb] = sys.exc_info()
        frames = [*reversed(list(traceback.walk_stack(None))),
                *traceback.walk_tb(tb)]
        tb = traceback.StackSummary.extract(frames)
        print('Traceback (most recent call last):')
        print(''.join(tb.format()), end='')
        print('%s: %s' % (etype.__name__, value))
        print()
        FAILS += 1

def inner_test(text, vars={}, rules=[]):
    exp_stderr = None

    # Run the input through gnu_make_parse
    f = io.StringIO(text)
    ctx = gnu_make_parse.ParseContext(enable_warnings=False)
    ctx.parse_file(f, 'test-file')

    for [k, v] in sorted(vars.items()):
        value = exc = None
        try:
            value = ctx.eval(ctx.variables[k])
        except Exception as e:
            exc = e

        if isinstance(v, type) and issubclass(v, Exception):
            assert exc is not None and isinstance(exc, v)
        else:
            assert exc is None and value == v, (k, value)

    with tempfile.NamedTemporaryFile(mode='wt') as f:
        # Collect input/expected output for make run
        f.write(text)
        f.write('\n')

        exp_stdout = []
        for [i, [k, v]] in enumerate(sorted(vars.items())):
            f.write('$(info %s="$(%s)")\n' % (k, k))

            if v == RecursionError:
                exp_stderr = exp_stderr or ('%s:%s: *** Recursive variable `%s\' references '
                        'itself (eventually).  Stop.\n' % (f.name, i+1, k)).encode()
            else:
                exp_stdout.append('%s="%s"\n' % (k, v))
        exp_stdout = ''.join(exp_stdout).encode()

        f.flush()

        # Run the input through make
        proc = subprocess.run(['make', '-f', f.name], capture_output=True)

        exp_stderr = exp_stderr or b'make: *** No targets.  Stop.\n'
        assert proc.stdout == exp_stdout, (proc.stdout, exp_stdout)
        assert proc.stderr == exp_stderr, (proc.stderr, exp_stderr)

def main():
    # Last newline gets trimmed from defines
    test('''define nl


endef''', vars={'nl': '\n'})

    # Space trimming
    test('''nothing :=
space := $(nothing) ''', vars={'space': ' '})

    # Recursive variable expansion
    test('''
x = $(y)
y = $(z)
z = abc''', vars={'x': 'abc'})

    # Detect infinite recursion
    test('x = $(x)', vars={'x': RecursionError})

    # Simple variable expansion
    test('''
x := a
y := $(x) b
x := c''', vars={'x': 'c', 'y': 'a b'})

    # Pattern substitution
    test('''
x := aa.o    ab.z    ba.o    bb.o
a := $(x:.o=.c)
b := $(x:%.o=%.c)
c := $(x:a%.o=%.c)
d := $(x:a%.o=a%.c)''', vars={
        'a': 'aa.c ab.z ba.c bb.c',
        'b': 'aa.c ab.z ba.c bb.c',
        'c': 'a.c ab.z ba.o bb.o',
        'd': 'aa.c ab.z ba.o bb.o',
    })

    # Appending, for recursive, simple, and undefined vars
    test('''
rec = $(base)
simple := $(base)
base = abc
rec += xyz
simple += xyz
und += xyz
# Redef
''', vars={'rec': 'abc xyz', 'simple': ' xyz', 'und': 'xyz'})

    # Same, but redefine variables in the middle to be the opposite type
    test('''
rec = $(base)
simple := $(base)
base = abc
rec += xyz
simple += xyz
rec := $(base2)
simple = $(base2)
base2 = abc
rec += xyz
simple += xyz
''', vars={'rec': ' xyz', 'simple': 'abc xyz'})


    # Function calls
    test('''
reverse = $(2) $(1)
var = $(call reverse,x,y)''', vars={'var': 'y x'})

    print('%s/%s tests passed.' % (PASSES, PASSES + FAILS))

if __name__ == '__main__':
    main()
