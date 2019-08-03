#!/usr/bin/env python3
#
# make.py (http://code.google.com/p/make-py/)
# $Revision$
# Copyright (c) 2014 Matt Craighead
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
# associated documentation files (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge, publish, distribute,
# sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or
# substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
# NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT
# OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

# This is a work-in-progress helper script for parsing GNU makefiles.  This can be useful sometimes
# when you need to convert existing makefiles into rules.py files.

import argparse
import collections
import glob
import os
import re
import shlex

# This is fairly restrictive, just to be safe for now
re_rule = re.compile(r'^([-\w./]+):(.*)$')

re_variable_assign = re.compile(r'(\S+)\s*(=|:=|\+=|\?=)\s*(.*)')

class ParseContext:
    def __init__(self, enable_warnings=True):
        self.enable_warnings = enable_warnings
        self.info_stack = []
        self.macros = {}
        self.variables = {'MAKE': 'make'}
        self.current_rule = None
        self.rules = []
        self.if_stack = [True]
        self.else_stack = []
        self.cur_macro = None

    def eval(self, expr):
        while True:
            i = expr.rfind('$(')
            if i < 0:
                return expr
            j = expr.find(')', i)
            name = expr[i+2:j]
            if name.startswith('sort '):
                value = ' '.join(sorted(set(name[5:].split())))
            elif name.startswith('strip '):
                value = name[6:].strip()
            elif name.startswith('findstring '):
                (pattern, text) = name[11:].split(',', 1)
                value = pattern if pattern in text else ''
            elif name.startswith('filter '):
                # XXX patterns can use %
                (pattern, text) = name[7:].split(',', 1)
                pattern = pattern.split()
                value = ' '.join(x for x in text.split() if x in pattern)
            elif name.startswith('filter-out '):
                # XXX patterns can use %
                (pattern, text) = name[11:].split(',', 1)
                pattern = pattern.split()
                value = ' '.join(x for x in text.split() if x not in pattern)
            elif name.startswith('addprefix '):
                (prefix, names) = name[10:].split(',', 1)
                value = ' '.join(prefix + x for x in names.split())
            elif name.startswith('addsuffix '):
                (suffix, names) = name[10:].split(',', 1)
                value = ' '.join(x + suffix for x in names.split())
            elif name.startswith('notdir '):
                value = name[7:]
                index = value.rfind('/')
                if index >= 0:
                    value = value[index+1:]
            elif name.startswith('wildcard '):
                value = ' '.join(glob.glob(name[9:]))
            elif name.startswith('or '):
                for arg in name[3:].split(','):
                    if arg:
                        value = arg
                        break
                else:
                    value = ''
            else:
                if name not in self.variables:
                    self.warning('variable %r does not exist' % name)
                value = self.variables.get(name, '')
            expr = expr[:i] + value + expr[j+1:]

    def is_eq(self, expr):
        expr = expr.split(',')
        assert len(expr) == 2
        return expr[0] == expr[1].lstrip()

    def print_message(self, prefix, message):
        (path, line_nb) = self.info_stack[-1]
        print('%s [%s:%s]: %s' % (prefix, path, line_nb, message))

    def error(self, message):
        self.print_message('ERROR', message)
        exit(1)

    def warning(self, message):
        if self.enable_warnings:
            self.print_message('WARNING', message)

    def parse_line(self, line):
        line_strip = line.strip()
        line_split = line.split()

        # First, check if we're inside a rule
        if self.current_rule:
            # If the line starts with a tab, this line is a command for the rule
            if line.startswith('\t'):
                (_, _, rule_cmds) = self.current_rule
                rule_cmds.append(self.eval(line[1:]))
                return
            # Otherwise, we're done with the rule. Handle the rule and parse
            # this line normally.
            self.flush_rule()

        if line.startswith('define '):
            self.cur_macro = line[7:]
            self.macros[self.cur_macro] = []
        elif line.startswith('ifeq ('):
            assert line.endswith(')')
            if self.if_stack[-1]:
                result = self.is_eq(self.eval(line[6:-1]))
            else:
                result = False
            self.else_stack.append(result)
            self.if_stack.append(self.if_stack[-1] & result)
        elif line.startswith('ifneq ('):
            assert line.endswith(')')
            if self.if_stack[-1]:
                result = not self.is_eq(self.eval(line[7:-1]))
            else:
                result = False
            self.else_stack.append(result)
            self.if_stack.append(self.if_stack[-1] & result)
        elif line.startswith('ifdef '):
            line = line[6:]
            result = self.variables.get(line, '') != ''
            self.else_stack.append(result)
            self.if_stack.append(self.if_stack[-1] & result)
        elif line.startswith('ifndef '):
            line = line[7:]
            result = self.variables.get(line, '') == ''
            self.else_stack.append(result)
            self.if_stack.append(self.if_stack[-1] & result)
        elif line.startswith('else ifeq ('):
            assert line.endswith(')')
            if self.if_stack[-2]:
                result = self.is_eq(self.eval(line[11:-1]))
            else:
                result = False
            self.if_stack[-1] = self.if_stack[-2] and not self.else_stack[-1] and result
            self.else_stack[-1] = self.else_stack[-1] or result
        elif line.startswith('else ifneq ('):
            assert line.endswith(')')
            if self.if_stack[-2]:
                result = not self.is_eq(self.eval(line[12:-1]))
            else:
                result = False
            self.if_stack[-1] = self.if_stack[-2] and not self.else_stack[-1] and result
            self.else_stack[-1] = self.else_stack[-1] or result
        elif line.startswith('else ifdef '):
            line = line[11:]
            result = self.variables.get(line, '') != ''
            self.if_stack[-1] = self.if_stack[-2] and not self.else_stack[-1] and result
            self.else_stack[-1] = self.else_stack[-1] or result
        elif line == 'else':
            self.if_stack[-1] = self.if_stack[-2] and not self.else_stack[-1]
        elif line_strip == 'endif':
            self.else_stack.pop()
            self.if_stack.pop()
        elif line.startswith('include ') or line.startswith('-include '):
            if self.if_stack[-1]:
                include_path = self.eval(line[8:].lstrip())
                if os.path.exists(include_path):
                    self.parse(include_path)
                elif not line.startswith('-'):
                    self.error('include file %r does not exist' % include_path)
                else:
                    self.warning('include file %r does not exist' % include_path)
        elif line.startswith('$(error '):
            assert line.endswith(')')
            line = line[8:-1]
            if self.if_stack[-1]:
                self.error(line)
        elif line.startswith('$(eval $('):
            assert line.endswith('))')
            for line in self.macros[line[9:-2]]:
                self.parse_line(line)
        else:
            m = re_variable_assign.match(line)
            if m is not None:
                (name, assign, value) = m.groups()
                if self.if_stack[-1]:
                    if assign == ':=':
                        self.variables[name] = self.eval(value)
                    elif assign == '+=':
                        if name in self.variables:
                            self.variables[name] += ' ' + self.eval(value)
                        else:
                            self.variables[name] = self.eval(value)
                    elif assign == '?=':
                        if self.variables.get(name, '') == '':
                            self.variables[name] = value
                    else:
                        assert assign == '='
                        self.variables[name] = value
            else:
                m = re_rule.match(line)
                if m is not None:
                    assert not self.current_rule
                    target, rule_deps = m.groups()
                    rule_deps = self.eval(rule_deps)
                    self.current_rule = (target, rule_deps, [])
                else:
                    self.error('could not parse %r' % line)

    def flush_rule(self):
        if not self.current_rule:
            return
        self.rules.append(self.current_rule)
        self.current_rule = None

    def parse(self, path):
        initial_if_stack_depth = len(self.if_stack)
        info = [path, 0]
        self.info_stack.append(info)
        with open(path) as f:
            line_prefix = ''
            for line_nb, line in enumerate(f):
                # Set line number for error messages
                info[1] = line_nb + 1

                # Remove whitespace from the right side (not the left since
                # we need to preserve tabs)
                line = line.rstrip()

                # Handle continuations first, before anything else
                if line_prefix:
                    line = line_prefix + line.lstrip()
                if line.endswith('\\'):
                    line_prefix = line[:-1] + ' '
                    continue
                line_prefix = ''

                # Remove comments and ignore blank lines
                i = line.find('#')
                if i >= 0:
                    line = line[:i]
                if not line:
                    continue

                # Are we inside a macro definition?
                if self.cur_macro is not None:
                    if line.strip() == 'endef':
                        self.cur_macro = None
                    else:
                        self.macros[self.cur_macro].append(line)
                    continue

                self.parse_line(line)

            # Clean up if we're inside a rule definition at the end
            self.flush_rule()

        assert not line_prefix
        assert initial_if_stack_depth == len(self.if_stack)

        self.info_stack.pop()

def format_list(l, indent=0):
    indent = ' ' * indent
    bump = ' ' * 4
    sep = ',\n' + indent + bump
    return '[\n%s%s\n%s]' % (indent + bump, sep.join(l), indent)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', action='append', dest='defines', default=[], help='set a variable')
    parser.add_argument('--no-warnings', action='store_false', dest='warnings',
            help='disable all warnings during generation')
    parser.add_argument('-f', '--file', help='input file to parse')
    args = parser.parse_args()

    ctx = ParseContext(enable_warnings=args.warnings)
    for d in args.defines:
        (k, v) = d.split('=', 1)
        ctx.variables[k] = v
    ctx.parse(args.file)
    #for (k, v) in sorted(ctx.variables.items()):
    #    print('%s: %r' % (k, v))

    with open('out_rules.py', 'wt') as f:
        f.write('def rules(ctx):\n')

        # Clean up rules
        rules = []
        for (target, rule_deps, rule_cmds) in ctx.rules:
            rule_deps = shlex.split(rule_deps)
            rule_cmds = [shlex.split(cmd) for cmd in rule_cmds]
            # Ignore - at the beginning of commands
            rule_cmds = [[cmd[0].lstrip('-')] + cmd[1:] for cmd in rule_cmds]
            # Ruthlessly remove @echo commands
            rule_cmds = [cmd for cmd in rule_cmds if cmd[0] != '@echo']
            rules.append((target, rule_deps, rule_cmds))

        # Collect, for each argument, a list all commands that use that
        # argument, so we can deduplicate
        args_used = collections.defaultdict(list)
        for (target, rule_deps, rule_cmds) in rules:
            for idx, cmd in enumerate(rule_cmds):
                for arg in cmd[1:]:
                    if arg.startswith('-') and arg not in {'-o', '-O'}:
                        args_used[arg].append((target, idx))

        # Create the inverse index: for each set of commands that use an
        # argument, accumulate all the arguments that are used by that
        # same set of commands
        args_used_by = collections.defaultdict(list)
        for arg, cmds in args_used.items():
            if len(cmds) < 5:
                continue
            args_used_by[tuple(cmds)].append(arg)

        # Write out argument list for deduplicated variables
        var_set_idx = {}
        for idx, (cmds, args) in enumerate(args_used_by.items()):
            f.write('    _vars_%s = %s\n' % (idx, format_list(map(repr, args), indent=4)))
            for arg in args:
                var_set_idx[arg] = idx

        # Preprocess arguments for variable replacements etc. so we can deduplicate
        # commands in build rules
        new_rules = []
        for (target, rule_deps, rule_cmds) in rules:
            new_cmds = []
            for cmd in rule_cmds:
                nice_cmd = []
                add_d_file = False
                d_file_path = None
                var_sets_used = set()
                for arg in cmd:
                    if arg == '$@':
                        nice_cmd.append('target')
                    elif arg == '$<':
                        nice_cmd.append('*rule_deps')
                    elif arg.startswith('-MT') and arg[3:] == target:
                        add_d_file = True
                    elif arg.startswith('-MF'):
                        d_file_path = arg[3:]
                    elif arg in var_set_idx:
                        var_sets_used.add(var_set_idx[arg])
                    else:
                        assert not arg.startswith('$'), arg
                        nice_cmd.append(repr(arg))

                for v in var_sets_used:
                    nice_cmd.append('*_vars_%s' % v)

                if add_d_file:
                    nice_cmd.append('"-MT%s" % target')
                    assert d_file_path
                    if d_file_path[:-1] == target[:-1]:
                        nice_cmd.append('"-MF%s.d" % target[:-2]')
                    else:
                        nice_cmd.append('"-MF%s"' % shlex.quote(d_file_path))

                new_cmds.append(nice_cmd)

            new_rules.append((target, rule_deps, new_cmds))
        rules = new_rules

        for (target, rule_deps, rule_cmds) in rules:
            if not rule_cmds:
                continue

            f.write('\n')
            f.write('    # %s\n' % target)
            f.write('    target = %r\n' % target)
            f.write('    rule_deps = %r\n' % rule_deps)
            f.write('    rule_cmds = [\n')
            for cmd in rule_cmds:
                f.write('        %s,\n' % format_list(cmd, indent=8))
            f.write('    ]\n')
            f.write('    ctx.add_rule(target, rule_deps, rule_cmds)\n')
