import io
import subprocess
import sys
import tempfile
import traceback

import gnu_make_parse

PASSES = FAILS = 0

class TestFailure(Exception): pass

# Wow, what a bunch of horseshit. I want to print the traceback of the caught
# exception, with all frames up to the root of the stack. The traceback.print_exc()
# function stops at the caller's frame, which is basically completely useless for
# us. Apparently this is not very important functionality, as there is a Python
# bug open since 2006: https://bugs.python.org/issue1553375
# So anyways, here's a hacky shitty way to get what we want. Maybe there's a better
# way, but this works, so oh well.
def get_traceback():
    frames = [*reversed(list(traceback.walk_stack(None)))]
    tb = traceback.StackSummary.extract(frames)
    # Chop the last line, that's the call to this function (gross)
    return tb.format()[:-1]

def test(*args, **kwargs):
    global PASSES, FAILS
    try:
        inner_test(*args, **kwargs)
        PASSES += 1
    except TestFailure as e:
        print('Traceback (most recent call last):')
        print(''.join(get_traceback()), end='')
        print(e)
        FAILS += 1
    except Exception:
        # Format the exception, and insert all the lines above us in the call stack
        lines = traceback.format_exception(*sys.exc_info())
        lines[1:1] = get_traceback()
        print(''.join(lines))
        FAILS += 1

def inner_test(name, text, vars={}, rules=[], enable_warnings=True):
    exp_stderr = None

    def check(msg, value, expected):
        if value != expected:
            raise TestFailure(
                'failure in test "%s", key %s:\n'
                '  expected: %s\n'
                '    actual: %s\n' % (name, msg, repr(expected), repr(value)))

    # Run the input through gnu_make_parse
    f = io.StringIO(text)
    ctx = gnu_make_parse.ParseContext(enable_warnings=enable_warnings)
    ctx.parse_file(f, name)

    for [k, v] in sorted(vars.items()):
        value = exc = None
        try:
            value = ctx.eval(ctx.variables[k])
        except Exception as e:
            exc = e

        if isinstance(v, type) and issubclass(v, Exception):
            assert exc is not None and isinstance(exc, v)
        else:
            check(k, exc, None)
            check(k, value, v)

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
        check('stdout', proc.stdout, exp_stdout)
        check('stderr', proc.stderr, exp_stderr)

# Test a single expression. Add the $(space) variable for convenience
def test_expr(expr, expected):
    test('expr', 'nothing:=\nspace:=$(nothing) \nz := %s' % expr, vars={'space': ' ', 'z': expected})

def main():
    test('last newline gets trimmed from defines', '''
define nl


endef''', vars={'nl': '\n'})

    test('space trimming', '''
nothing :=
space := $(nothing) # space after var''', vars={'space': ' '})

    foo = 'a    b        c    d  '
    test('spaces in variables', 'foo := %s' % foo, vars={'foo': foo})

    test('special character handling', '''
nothing :=
space := $(nothing) # space at eol
comma := ,
x := a b c
y := $(subst $(space),$(comma),$(x))
z := $(subst $(space),$(comma),$(x)  ,,a)# extra args
''', vars={'x': 'a b c', 'y': 'a,b,c', 'z': 'a,b,c,,,,a', 'space': ' '})

    test('recursive variable expansion', '''
x = $(y)
y = $(z)
z = abc''', vars={'x': 'abc'})

    test('detect infinite recursion', 'x = $(x)', vars={'x': RecursionError})

    test('simple variable expansion', '''
x := a
y := $(x) b
x := c''', vars={'x': 'c', 'y': 'a b'})

    test('pattern substitution', '''
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

    test('appending for recursive/simple/undefined vars', '''
rec = $(base)
simple := $(base)
base = abc
rec += xyz
simple += xyz
und += xyz
# Redef
''', vars={'rec': 'abc xyz', 'simple': ' xyz', 'und': 'xyz'}, enable_warnings=False)

    test('appending with variables switching types', '''
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
''', vars={'rec': ' xyz', 'simple': 'abc xyz'}, enable_warnings=False)

    test('function calls', '''
reverse = $(2) $(1)
var = $(call reverse,x,y)''', vars={'var': 'y x'})

    # Standard functions
    test_expr('$(sort   a   b c f e d c)', 'a b c d e f')
    test_expr('$(sort   a,b   a b a a,b b,a)', 'a a,b b b,a')
    test_expr('$(sort   a$(space)b  , b$(space)a ,  a$(space)b)', ', a b')

    test_expr('$(strip       x,       )', 'x,')
    test_expr('$(strip       x,    y   )', 'x, y')
    test_expr('$(strip       x    y   )', 'x y')

    test_expr('$(findstring   a  ,a)', '')
    test_expr('$(findstring   a  ,a  )', 'a  ')
    test_expr('$(findstring   a  ,  a  )', 'a  ')
    test_expr('$(findstring   a  ,  a  )', 'a  ')

    test_expr('$(filter   a  b  ,   a b c   d , a)', 'a b a')
    test_expr('$(filter-out   a  b  , a b c   d , a)', 'c d ,')
    # % is any string. Also make sure other characters are treated literally
    test_expr('$(filter a% b%, a b c aa d ba)', 'a b aa ba')
    test_expr('$(filter-out a% b%, a b c aa d ba)', 'c d')
    test_expr('$(filter a* b?, a* b? a b c aa d ba)', 'a* b?')
    test_expr('$(filter-out a* b?, a* b? a b c aa d ba)', 'a b c aa d ba')
    test_expr('$(filter a%b%c, a%b%c ab%c aabcc abc aabbcc axc)', 'a%b%c ab%c')

    test_expr('$(addprefix   a,    x  y   z)', 'ax ay az')
    test_expr('$(addprefix   a,    x,y,  y,   z)', 'ax,y, ay, az')
    test_expr('$(addsuffix   a,    x  y   z)', 'xa ya za')
    test_expr('$(addsuffix   a,    x,y,  y,   z)', 'x,y,a y,a za')

    test_expr('$(subst     {,x,a { {,{)', 'a x x,x')
    test_expr('$(subst     {, x, a { {,{)', ' a  x  x, x')

    test_expr('$(patsubst a%bc, x%yz , abc ab%c a%bc aabc)', ' xyz  ab%c  x%yz   xayz ')
    test_expr('$(patsubst a%b%c, x%y%z , abc ab%c a%bc xyz)', 'abc  xy%z  a%bc xyz')

    test_expr('$(notdir   a  a/b   a/b/c  x,y/z/a,b,c)', 'a b c a,b,c')

    # Wildcard test--this depends on the contents of test_files
    test_expr('$(wildcard test_f*/*.c)', 'test_files/a.c test_files/b.c test_files/c.c')

    print('%s/%s tests passed.' % (PASSES, PASSES + FAILS))

if __name__ == '__main__':
    main()
