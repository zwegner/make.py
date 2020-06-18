import io
import os
import shlex
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

def test(name, text, **kwargs):
    global PASSES, FAILS

    # Check test name against filters
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg in name or (not name and arg in text):
                break
        else:
            return

    try:
        inner_test(name, text, **kwargs)
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
    def check(msg, value, expected):
        if value != expected:
            raise TestFailure(
                'failure in test "%s", key %s:\n'
                '  expected: %s\n'
                '    actual: %s\n' % (name, msg, repr(expected), repr(value)))

    # First, write a makefile and compare the output with what we're expecting.
    # We augment the makefile with $(info) calls to print out the values of all
    # variables we're testing. We run make first before comparing our internal
    # evaluation, so we can easily capture the correct output during development.
    with tempfile.NamedTemporaryFile(mode='wt') as f:
        f.write(text)
        f.write('\n')

        # Collect input/expected output for make run
        exp_stdout = []
        exp_stderr = None
        for [i, [k, v]] in enumerate(sorted(vars.items())):
            f.write('$(info %s="$(%s)")\n' % (k, k))

            if v == RecursionError:
                exp_stderr = exp_stderr or ('%s:%s: *** Recursive variable `%s\' references '
                        'itself (eventually).  Stop.\n' % (f.name, i+1, k)).encode()
            else:
                exp_stdout.append('%s="%s"\n' % (k, v))
        # Get the shell-quoted command line for each command
        for rule in rules:
            for cmd in rule['cmds']:
                exp_stdout.append('%s\n' % ' '.join(shlex.quote(arg) for arg in cmd))
        exp_stdout = ''.join(exp_stdout).encode()

        if not rules:
            exp_stderr = exp_stderr or b'make: *** No targets.  Stop.\n'
        else:
            exp_stderr = exp_stderr or b''

        f.flush()

        # Run the input through make, making all the targets in order, so we can
        # compare the command lines
        targets = [rule['target'] for rule in rules]
        make_cmd = ['make', '--dry-run', '--always-make', '-f', f.name, *targets]
        proc = subprocess.run(make_cmd, capture_output=True)
        check('stderr', proc.stderr, exp_stderr)
        check('stdout', proc.stdout, exp_stdout)

    # Run the input through gnu_make_parse
    f = io.StringIO(text)
    ctx = gnu_make_parse.ParseContext(enable_warnings=enable_warnings)
    # Add fake file/line info for any error messages that might happen after parsing
    ctx.info_stack.append(['test.py', 0])
    ctx.parse_file(f, name)

    # Make sure our internal eval matches the expected value for each variable
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

    # Match rules
    out_rules = ctx.get_cleaned_rules()
    check('rule len', len(rules), len(out_rules))
    for [rule, exp] in zip(out_rules, rules):
        for [k, v] in sorted(exp.items()):
            value = ctx.eval(getattr(rule, k))
            check(k, value, v)

# Test a single expression. Add the $(space) variable for convenience
def test_expr(expr, expected):
    test('', 'nothing:=\nspace:=$(nothing) \nz := %s' % expr, vars={'space': ' ', 'z': expected})

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

    test('double variable expansion', '''
a := xyz
b := 123
c := a
x = $($(c))
y := $(x)
c := b
z := $(x)
''', vars={'x': '123', 'y': 'xyz', 'z': '123'})

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

    test('pattern substitution after var expansion', '''
a_obj := a.o b.o c.o
b_obj := 1.o 2.o 3.o
sel := b
a_c = $($(sel)_obj:.o=.c)
b_c := $(a_c)
sel := a
''', vars={'a_c': 'a.c b.c c.c', 'b_c': '1.c 2.c 3.c'})

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

    test_expr('$(addprefix   a,    x  y   z)', 'ax ay az')
    test_expr('$(addprefix   a,    x,y,  y,   z)', 'ax,y, ay, az')
    test_expr('$(addsuffix   a,    x  y   z)', 'xa ya za')
    test_expr('$(addsuffix   a,    x,y,  y,   z)', 'x,y,a y,a za')

    test_expr('$(and ,,,,a)', '')
    test_expr('$(and ,  , a ,b)', '')

    test_expr('$(filter   a  b  ,   a b c   d , a)', 'a b a')
    test_expr('$(filter-out   a  b  , a b c   d , a)', 'c d ,')
    # % is any string. Also make sure other characters are treated literally
    test_expr('$(filter a% b%, a b c aa d ba)', 'a b aa ba')
    test_expr('$(filter-out a% b%, a b c aa d ba)', 'c d')
    test_expr('$(filter a* b?, a* b? a b c aa d ba)', 'a* b?')
    test_expr('$(filter-out a* b?, a* b? a b c aa d ba)', 'a b c aa d ba')
    test_expr('$(filter a%b%c, a%b%c ab%c aabcc abc aabbcc axc)', 'a%b%c ab%c')

    test_expr('$(findstring   a  ,a)', '')
    test_expr('$(findstring   a  ,a  )', 'a  ')
    test_expr('$(findstring   a  ,  a  )', 'a  ')
    test_expr('$(findstring   a  ,  a  )', 'a  ')

    test_expr('$(notdir   a  a/b   a/b/c  x,y/z/a,b,c)', 'a b c a,b,c')

    test_expr('$(or ,,,,a,b,c)', 'a')
    test_expr('$(or ,  , a ,b)', 'a')

    test_expr('$(patsubst a%bc, x%yz , abc ab%c a%bc aabc)', ' xyz  ab%c  x%yz   xayz ')
    test_expr('$(patsubst a%b%c, x%y%z , abc ab%c a%bc xyz)', 'abc  xy%z  a%bc xyz')
    test_expr('$(patsubst %, a%z, a b c)', ' aaz  abz  acz')

    path = os.path.realpath('test_files/a.c')
    test_expr('$(realpath test_files/a.c)', path)

    test_expr('$(sort   a   b c f e d c)', 'a b c d e f')
    test_expr('$(sort   a,b   a b a a,b b,a)', 'a a,b b b,a')
    test_expr('$(sort   a$(space)b  , b$(space)a ,  a$(space)b)', ', a b')

    test_expr('$(strip       x,       )', 'x,')
    test_expr('$(strip       x,    y   )', 'x, y')
    test_expr('$(strip       x    y   )', 'x y')
    test_expr('$(strip   a,    b,c,  d  , e , f )', 'a, b,c, d , e , f')
    test_expr('$(strip   a,    b,   c,  d  , e , f )', 'a, b, c, d , e , f')
    test_expr('$(strip   a,b,   c,  d  , e , f )', 'a,b, c, d , e , f')

    test_expr('$(subst     {,x,a { {,{)', 'a x x,x')
    test_expr('$(subst     {, x, a { {,{)', ' a  x  x, x')

    # Wildcard test--this depends on the contents of test_files
    test_expr('$(wildcard test_f*/*.c)', 'test_files/a.c test_files/b.c test_files/c.c')
    test('wildcard after var expansion', '''
t:=test
a:=$(wildcard $t_f*/*.c)''', vars={'a': 'test_files/a.c test_files/b.c test_files/c.c'})

    rule = {
        'target': 'exe',
        'deps': ['test_files/a.c'],
        'cmds': [['cc', '-o', 'exe', 'test_files/a.c']]
    }
    test('basic rule', '''
exe: test_files/a.c
\tcc -o exe test_files/a.c''', rules=[rule])

    # Test quoting: quotes are totally unprocessed by gmake, and passed to
    # the shell. We use shlex internally to parse command lines after they've
    # been evaluated
    rule = {
        'target': 'exe',
        'deps': ['test_files/a.c'],
        'cmds': [['echo', 'cool exe,', 'bro'],
            ['cc', '-o', 'exe', 'test_files/a.c']]
    }
    test('basic quoting within rule', '''
exe: test_files/a.c
\techo 'cool $aexe$(subst y,' ,,ybro)
\tcc -o exe test_files/a.c
''', rules=[rule], enable_warnings=False)

    # This is a weird test: turns out variables are evaluated in command
    # lines at the point they are executed, with the final state of all
    # variables at the end of the makefile. *Unless* the rule is inside
    # an $(eval) call, in which case the variables are evaluated then.
    # So this makefile creates three rules, with the one for a.c (which is
    # outside a macro) gets the final x=c value.
    rules = [{
        'target': 'test_files/%s.o' % base,
        'deps': ['test_files/%s.c' % base],
        'cmds': [['echo', 'c' if base == 'a' else base],
            ['cc', '-c', 'test_files/%s.c' % base]]
    } for base in ['a', 'b', 'c']]
    test('variables within rules are evaluated at the right time', '''
y = $x
define make_rule
$1.o: $1.c
\techo $y
\tcc -c $1.c
endef

x := a
test_files/a.o: test_files/a.c
\techo $x
\tcc -c test_files/a.c

x := b
$(eval $(call make_rule,test_files/b))
x := c
$(eval $(call make_rule,test_files/c))
''', rules=rules)
    print('%s/%s tests passed.' % (PASSES, PASSES + FAILS))

if __name__ == '__main__':
    main()
