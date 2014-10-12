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
import sys

def eval_expr(expr, variables):
    while True:
        i = expr.find('$(')
        if i < 0:
            return expr
        j = expr.find(')', i)
        name = expr[i+2:j]
        expr = expr[:i] + variables[name] + expr[j+1:]

def parse_makefile(path, variables):
    if_stack = [True]
    else_stack = []

    with open(path) as f:
        line_prefix = ''
        for line in f:
            line = line_prefix + line.strip()
            if not line or line.startswith('#'):
                continue
            if line.endswith('\\'):
                line_prefix = line[:-1] + ' '
                continue
            line_prefix = ''
            line_split = line.split()
            if line.startswith('ifeq ('):
                assert line.endswith(')')
                line = line[6:-1].split(',')
                assert len(line) == 2
                line[0] = eval_expr(line[0], variables)
                line[1] = eval_expr(line[1], variables)
                result = line[0] == line[1]
                else_stack.append(result)
                if_stack.append(if_stack[-1] & result)
            elif line.startswith('ifneq ('):
                assert line.endswith(')')
                line = line[7:-1].split(',')
                assert len(line) == 2
                line[0] = eval_expr(line[0], variables)
                line[1] = eval_expr(line[1], variables)
                result = line[0] != line[1]
                else_stack.append(result)
                if_stack.append(if_stack[-1] & result)
            elif line.startswith('else ifeq ('):
                assert line.endswith(')')
                line = line[11:-1].split(',')
                assert len(line) == 2
                line[0] = eval_expr(line[0], variables)
                line[1] = eval_expr(line[1], variables)
                result = line[0] == line[1]
                if_stack[-1] = if_stack[-2] and not else_stack[-1] and result
                else_stack[-1] = else_stack[-1] or result
            elif line == 'else':
                if_stack[-1] = if_stack[-2] and not else_stack[-1]
            elif line_split[0] == 'endif':
                else_stack.pop()
                if_stack.pop()
            elif line.startswith('$(error '):
                assert line.endswith(')')
                line = line[8:-1]
                if if_stack[-1]:
                    print('ERROR: %s' % line)
                    exit(1)
            elif len(line_split) >= 2 and line_split[1] == ':=':
                if if_stack[-1]:
                    variables[line_split[0]] = ' '.join(line_split[2:])
            elif len(line_split) >= 2 and line_split[1] == '+=':
                if if_stack[-1]:
                    variables[line_split[0]] += ' ' + ' '.join(line_split[2:])
            else:
                print('ERROR: could not parse %r' % line)
                exit(1)
    assert if_stack == [True]

parser = argparse.ArgumentParser()
parser.add_argument('-d', action='append', dest='defines', default=[], help='set a variable')
parser.add_argument('-f', '--file', help='input file to parse')
args = parser.parse_args()

print(args)

variables = {}
for d in args.defines:
    (k, v) = d.split('=', 1)
    variables[k] = v
parse_makefile(args.file, variables)
for (k, v) in sorted(variables.items()):
    print('%s: %r' % (k, v))
