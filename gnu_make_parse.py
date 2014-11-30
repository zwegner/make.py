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

class ParseContext:
    def __init__(self):
        self.variables = {}
        self.if_stack = [True]
        self.else_stack = []

    def eval(self, expr):
        while True:
            i = expr.rfind('$(')
            if i < 0:
                return expr
            j = expr.find(')', i)
            name = expr[i+2:j]
            if name.startswith('sort '):
                value = ' '.join(sorted(set(name[5:].split())))
            elif name.startswith('filter '):
                # XXX patterns can use %
                (pattern, text) = name[7:].split(',', 1)
                pattern = pattern.split()
                value = ' '.join(x for x in text.split() if x in pattern)
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
            else:
                value = self.variables[name]
            expr = expr[:i] + value + expr[j+1:]

    def is_eq(self, expr):
        expr = expr.split(',')
        assert len(expr) == 2
        return expr[0] == expr[1]

    def parse(self, path):
        initial_if_stack_depth = len(self.if_stack)
        with open(path) as f:
            line_prefix = ''
            for line in f:
                # Handle continuations first, before anything else
                line = line_prefix + line.strip()
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

                line_strip = line.strip()
                line_split = line.split()
                if line.startswith('ifeq ('):
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
                elif line.startswith('include '):
                    if self.if_stack[-1]:
                        include_path = self.eval(line[8:])
                        if os.path.exists(include_path):
                            self.parse(include_path)
                        else:
                            print('WARNING: include file %r does not exist' % include_path)
                elif line.startswith('$(error '):
                    assert line.endswith(')')
                    line = line[8:-1]
                    if self.if_stack[-1]:
                        print('ERROR: %s' % line)
                        exit(1)
                elif len(line_split) >= 2 and line_split[1] == '=':
                    if self.if_stack[-1]:
                        value = ' '.join(line_split[2:])
                        self.variables[line_split[0]] = value
                elif len(line_split) >= 2 and line_split[1] == ':=':
                    if self.if_stack[-1]:
                        value = self.eval(' '.join(line_split[2:]))
                        self.variables[line_split[0]] = value
                elif len(line_split) >= 2 and line_split[1] == '+=':
                    if self.if_stack[-1]:
                        value = self.eval(' '.join(line_split[2:]))
                        if line_split[0] in self.variables:
                            self.variables[line_split[0]] += ' ' + value
                        else:
                            self.variables[line_split[0]] = value
                else:
                    print('ERROR: could not parse %r' % line)
                    exit(1)
        assert not line_prefix
        assert initial_if_stack_depth == len(self.if_stack)

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
    for (k, v) in sorted(ctx.variables.items()):
        print('%s: %r' % (k, v))
