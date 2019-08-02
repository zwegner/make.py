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
import glob
import os
import re
import shlex

# This is fairly restrictive, just to be safe for now
re_rule = re.compile(r'^([-\w./]+):(.*)$')

re_variable_assign = re.compile(r'(\S+)\s*(=|:=|\+=|\?=)\s*(.*)')

class ParseContext:
    def __init__(self):
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
                    rule_path, rule_deps = m.groups()
                    rule_deps = self.eval(rule_deps)
                    self.current_rule = (rule_path, rule_deps, [])
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

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', action='append', dest='defines', default=[], help='set a variable')
    parser.add_argument('-f', '--file', help='input file to parse')
    args = parser.parse_args()

    ctx = ParseContext()
    for d in args.defines:
        (k, v) = d.split('=', 1)
        ctx.variables[k] = v
    ctx.parse(args.file)
    #for (k, v) in sorted(ctx.variables.items()):
    #    print('%s: %r' % (k, v))

    with open('out_rules.py', 'wt') as f:
        f.write('def rules(ctx):\n')

        for (rule_path, rule_deps, rule_cmds) in ctx.rules:
            if not rule_cmds:
                continue
            rule_deps = shlex.split(rule_deps)
            rule_cmds = [shlex.split(cmd) for cmd in rule_cmds]
            # Ignore - at the beginning of commands
            rule_cmds = [[cmd[0].lstrip('-')] + cmd[1:] for cmd in rule_cmds]
            # Ruthlessly remove @echo commands
            rule_cmds = [cmd for cmd in rule_cmds if cmd[0] != '@echo']

            f.write('    target = %r\n' % rule_path)
            f.write('    rule_deps = %r\n' % rule_deps)
            f.write('    rule_cmds = [\n')
            for cmd in rule_cmds:
                nice_cmd = []
                for arg in cmd:
                    if arg == '$@':
                        nice_cmd.append('target')
                    elif arg == '$<':
                        nice_cmd.append('*rule_deps')
                    else:
                        assert not arg.startswith('$'), arg
                        nice_cmd.append(repr(arg))
                f.write('        [%s],\n' % ',\n          '.join(nice_cmd))
            f.write('    ]\n')
            f.write('    ctx.add_rule(target, rule_deps, rule_cmds)\n')
