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
import sys

from gnu_make_lib import *

# This is fairly restrictive, just to be safe for now
re_rule = re.compile(r'^((?:[-\w./%]|\\ )+):(.*)$')

re_variable_assign = re.compile(r'(\S+)\s*(=|:=|\+=|\?=)\s*(.*)')

re_variable_subst = re.compile(r'([.%\w]*)=([.%\w]*)')

# A few "classes" for storing unevaluated variables and the like, that are returned
# from parse_expr(). This gives us a unified representation of strings/variables
# that can be evaluated either later during parsing or at runtime (i.e. translated
# into Python code and inserted into the rules.py file we output).

def MetaVar(value):
    return ('metavar', value)

def Var(value):
    return ('var', value)

def UnpackList(value):
    return ('unpack', value)

def Glob():
    return ('Glob',)

def PatSubst(value, old, new):
    return ('pat-subst', value, old, new)

def Join(*args):
    # Collapse consecutive strings
    r = []
    for arg in args:
        if isinstance(arg, str):
            if not arg:
                continue
            elif r and isinstance(r[-1], str):
                r[-1] = r[-1] + arg
            else:
                r.append(arg)
        elif isinstance(arg, tuple) and arg[0] == 'join':
            r.extend(arg[1:])
        else:
            r.append(arg)
    if not r:
        return ''
    if len(r) == 1:
        return r[0]
    return ('join', *r)

# Simple object for storing info on parsed rules. We don't need any functionality really.
class Rule:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

# Kinda dumb tokenization function--like str.find() but finds the first of multiple tokens
def find_first(s, tokens):
    first_token = first_idx = None
    for token in tokens:
        idx = s.find(token)
        if idx == -1:
            continue
        if first_idx is None or idx < first_idx:
            first_idx = idx
            first_token = token
    return (first_token, first_idx)

class ParseContext:
    def __init__(self, enable_warnings=True):
        self.enable_warnings = enable_warnings
        self.info_stack = []
        self.variables = {'MAKE': 'make'}
        self.current_rule = None
        self.rules = []
        self.if_stack = [True]
        self.else_stack = []
        self.cur_macro = None
        self.cur_macro_lines = None

    def parse_expr(self, expr):
        result = []
        while True:
            # Find the first special character
            (token, i) = find_first(expr, ('%', '$'))
            if i is None:
                break

            # Add everything before the dollar to the result list
            result.append(expr[:i])

            # Translate % into a glob
            if token == '%':
                result.append(Glob())
                expr = expr[i+1:]
                continue

            assert token == '$'

            # Get the name of the variable/expression being evaluated (single letter or parenthesized)
            # Also, reset expr to be the remainder of the line for the next loop iteration.
            if expr[i+1] == '(':
                j = expr.find(')', i)
                name = expr[i+2:j]
                expr = expr[j+1:]
                # XXX
                if '$' in name:
                    self.error('recursive expressions not supported yet')
            else:
                name = expr[i+1]
                expr = expr[i+2:]

            # Check for substitutions, like $(SRCS:%.c=%.o)
            subst = None
            if ':' in name:
                name, _, subst = name.partition(':')
                m = re_variable_subst.match(subst)
                assert m
                [old, new] = m.groups()
                if '%' in old:
                    assert old.count('%') == 1
                else:
                    old = '%' + old
                    new = '%' + new
                subst = (old, new)

            # Literal $
            if name == '$':
                value = '$'

            # Special variables
            elif name == '@':
                value = MetaVar('target')
            elif name == '<':
                value = UnpackList(MetaVar('rule_deps'))

            # Functions
            elif name.startswith('sort '):
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
            elif name.startswith('call '):
                args = name[5:].split(',')
                fn = args[0]
                if fn not in self.variables:
                    self.error('function %r does not exist' % fn)
                # Create a new variable context with $(1) etc filled in with args
                old_vars = self.variables
                self.variables = self.variables.copy()
                for [i, arg] in enumerate(args):
                    self.variables[str(i)] = arg
                # Evaluate
                value = self.eval(self.variables.get(fn, ''))
                self.variables = old_vars

            elif name.startswith('or '):
                for arg in name[3:].split(','):
                    if arg:
                        value = arg
                        break
                else:
                    value = ''
            else:
                value = Var(name)

            # Wrap expression with a substitution when necessary
            if subst:
                (old, new) = subst
                value = PatSubst(value, old, new)

            result.append(value)

        result.append(expr)
        return Join(*result)

    def eval(self, expr):
        if isinstance(expr, str):
            return expr
        assert isinstance(expr, tuple)
        [fn, *args] = expr
        if fn == 'join':
            value = ''.join(self.eval(arg) for arg in args)
        elif fn == 'var':
            [name] = args
            if name not in self.variables:
                self.warning('variable %r does not exist' % name)
            value = self.variables.get(name, '')
        elif fn == 'pat-subst':
            [value, old, new] = args
            return pat_subst(self.eval(value), old, new)
        else:
            assert 0, fn
        # Recursively evaluate while not fully expanded
        return self.eval(value)

    def parse_and_eval(self, expr):
        expr = self.parse_expr(expr)
        return self.eval(expr)

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

    def get_norm_path(self, path):
        assert isinstance(path, str)
        return os.path.normpath('%s/%s' % (self.root_path, path))

    def parse_line(self, line):
        line_strip = line.strip()
        line_split = line.split()

        if line.startswith('define '):
            self.cur_macro = line[7:]
            self.cur_macro_lines = []
        elif line.startswith('ifeq ('):
            assert line.endswith(')')
            if self.if_stack[-1]:
                result = self.is_eq(self.parse_and_eval(line[6:-1]))
            else:
                result = False
            self.else_stack.append(result)
            self.if_stack.append(self.if_stack[-1] & result)
        elif line.startswith('ifneq ('):
            assert line.endswith(')')
            if self.if_stack[-1]:
                result = not self.is_eq(self.parse_and_eval(line[7:-1]))
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
                result = self.is_eq(self.parse_and_eval(line[11:-1]))
            else:
                result = False
            self.if_stack[-1] = self.if_stack[-2] and not self.else_stack[-1] and result
            self.else_stack[-1] = self.else_stack[-1] or result
        elif line.startswith('else ifneq ('):
            assert line.endswith(')')
            if self.if_stack[-2]:
                result = not self.is_eq(self.parse_and_eval(line[12:-1]))
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
                # XXX parsing expressions and token splitting needs to be combined!
                paths = shlex.split(self.parse_and_eval(line[8:].lstrip()))
                for path in paths:
                    include_path = self.get_norm_path(path)
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
            value = self.parse_and_eval(line[7:-1])
            for line in value.splitlines():
                self.parse_line(line)
        # No recognized construct: check for rule commands, variable assignment, and rule definitions
        else:
            # If the line starts with a tab, this line is a command for the rule
            if line.startswith('\t'):
                if not self.current_rule:
                    self.error('command not inside a rule')
                if self.if_stack[-1]:
                    # XXX parsing expressions and token splitting needs to be combined!
                    line = line[1:]
                    cmd = shlex.split(self.parse_and_eval(line))
                    self.current_rule.cmds.append(cmd)
                return
            # Otherwise, if we were inside a rule, we're done. Flush the rule and parse
            # this line normally.
            elif self.current_rule:
                self.flush_rule()

            m = re_variable_assign.match(line)
            if m is not None:
                (name, assign, value) = m.groups()
                if self.if_stack[-1]:
                    expr = self.parse_expr(value)
                    if assign == ':=':
                        self.variables[name] = self.eval(expr)
                    elif assign == '+=':
                        if name in self.variables:
                            self.variables[name] += ' ' + self.eval(expr)
                        else:
                            self.variables[name] = self.eval(expr)
                    elif assign == '?=':
                        if self.variables.get(name, '') == '':
                            self.variables[name] = expr
                    else:
                        assert assign == '='
                        self.variables[name] = expr
            else:
                m = re_rule.match(line)
                if m is not None:
                    assert not self.current_rule
                    target, deps = m.groups()
                    target = self.parse_and_eval(target)
                    # XXX parsing expressions and token splitting needs to be combined!
                    deps = shlex.split(deps)
                    deps = [self.parse_and_eval(dep) for dep in deps]
                    # Check for order-only deps
                    if '|' in deps:
                        idx = deps.index('|')
                        [deps, oo_deps] = deps[:idx], deps[idx+1:]
                        if '|' in oo_deps:
                            self.error('multiple | separators in line')
                    else:
                        oo_deps = []
                    self.current_rule = Rule(target=target, deps=deps, oo_deps=oo_deps, cmds=[])
                else:
                    self.error('could not parse %r' % line)

    def flush_rule(self):
        if not self.current_rule:
            return
        self.rules.append(self.current_rule)
        self.current_rule = None

    def parse(self, path):
        with open(path) as f:
            self.parse_file(f, path)

    def parse_file(self, f, path):
        initial_if_stack_depth = len(self.if_stack)
        info = [path, 0]
        self.info_stack.append(info)

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
                    assert self.cur_macro_lines[-1] == '\n'
                    self.variables[self.cur_macro] = Join(*self.cur_macro_lines[:-1])
                    self.cur_macro = None
                    self.cur_macro_lines = None
                else:
                    self.cur_macro_lines.append(self.parse_expr(line))
                    self.cur_macro_lines.append('\n')
                continue

            self.parse_line(line)

        # Clean up if we're inside a rule definition at the end
        self.flush_rule()

        assert not line_prefix
        assert initial_if_stack_depth == len(self.if_stack)

        self.info_stack.pop()

    def get_cleaned_rules(self):
        rules = []
        for rule in self.rules:
            rule.target = self.get_norm_path(rule.target)
            rule.deps = shlex.split(rule.deps)
            rule.deps = [self.get_norm_path(dep) for dep in rule.deps]
            # Ignore - at the beginning of commands
            rule.cmds = [[cmd[0].lstrip('-')] + cmd[1:] for cmd in rule.cmds]
            # Ruthlessly remove @echo commands
            rule.cmds = [cmd for cmd in rule.cmds if cmd[0] != '@echo']
            if not rule.cmds:
                continue
            rules.append(rule)
        return rules

def format_expr(expr):
    if isinstance(expr, str):
        assert '%' not in expr
        return repr(expr)
    assert isinstance(expr, tuple)
    (fn, *args) = expr
    if fn == 'join':
        return "(%s)" % (' + '.join(format_expr(a) for a in args))
    elif fn == 'metavar':
        [var] = args
        return var
    elif fn == 'unpack':
        (value,) = args
        return '*' + format_expr(value)
    elif fn == 'pat-subst':
        [value, old, new] = (format_expr(a) for a in args)
        return 'pat_subst(%s, %s, %s)' % (value, old, new)
    else:
        assert 0

def format_list(l, indent=0):
    indent = ' ' * indent
    items = ['%s    %s,\n' % (indent, format_expr(item)) for item in l]
    return '[\n%s%s]' % (''.join(items), indent)

def format_dict(d, indent=0, use_repr=False):
    d = d.items()
    if use_repr:
        d = ((repr(k), repr(v)) for k, v in d)
    indent = ' ' * indent
    items = ['%s    %s: %s,\n' % (indent, k, v) for k, v in d]
    return '{\n%s%s}' % (''.join(items), indent)

def rule_key(rule):
    return (tuple(rule.deps), tuple(tuple(c) for c in rule.cmds), rule.succ_list_idx, rule.pred_list_idx)

def get_args_used_map(rules):
    # Collect, for each argument, a list all commands that use that
    # argument, so we can deduplicate
    # XXX we should take order into account for correctness, since tools can change
    # behavior based on the order of arguments (obviously). For now, we're going
    # the imperfect way in the hope of getting maximum deduplication (and keeping this
    # code simple).
    args_used = collections.defaultdict(list)
    for rule in rules:
        for idx, cmd in enumerate(rule.cmds):
            for arg in cmd[1:]:
                # Only allow concrete values as arguments--any unevaluated expression
                # can have a different value per command and cannot be deduplicated here.
                if isinstance(arg, str):
                    args_used[arg].append((rule.target, idx))

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
        for arg in args:
            var_set_idx[arg] = idx

    return args_used_by, var_set_idx

def process_rule_cmds(rules, var_set_idx):
    # Preprocess arguments for variable replacements etc. so we can deduplicate
    # commands in build rules
    for rule in rules:
        new_cmds = []
        for cmd in rule.cmds:
            nice_cmd = []
            add_d_file = False
            d_file_path = None
            var_sets_used = set()
            for arg in cmd:
                # Check for arguments with not-yet-evaluated expressions
                if not isinstance(arg, str):
                    assert isinstance(arg, tuple)
                    nice_cmd.append(arg)
                # XXX disabled for now
                #elif arg.startswith('-MT') and arg[3:] == rule.target:
                #    add_d_file = True
                #elif arg.startswith('-MF'):
                #    d_file_path = arg[3:]
                elif arg in var_set_idx:
                    var_sets_used.add(var_set_idx[arg])
                else:
                    assert '$' not in arg, arg
                    nice_cmd.append(arg)

            for v in var_sets_used:
                nice_cmd.append(UnpackList(MetaVar('_vars_%s' % v)))

            # XXX disabled for now
            #if add_d_file:
            #    nice_cmd.append('"-MT%s" % target')
            #    assert d_file_path
            #    if d_file_path[:-1] == rule.target[:-1]:
            #        nice_cmd.append('"-MF%s.d" % target[:-2]')
            #    else:
            #        nice_cmd.append('"-MF%s"' % shlex.quote(d_file_path))

            new_cmds.append(nice_cmd)
        rule.cmds = new_cmds

def process_rule_links(rules):
    # Find sources/sinks of each target, so we can maintain lists for cleaner output
    for rule in rules:
        rule.preds = []
        rule.succs = []
        rule.succ_list_idx = None
        rule.pred_list_idx = None
    for rule in rules:
        for other in rules:
            if rule is other:
                continue
            if any(dep == rule.target for dep in other.deps):
                rule.succs.append(other)
                other.preds.append(rule)

    src_lists = []
    for rule in rules:
        if len(rule.preds) > 1:
            rule.pred_list_idx = len(src_lists)
            for other in rule.preds:
                other.succ_list_idx = rule.pred_list_idx
            src_lists.append(rule)

            # Filter out dependencies that are part of the src list
            rule.deps = [dep for dep in rule.deps if not any(
                dep == other.target for other in rule.preds)]

    return src_lists

def process_rule_dirs(rules):
    # Detect formulaic source/destination directories
    dir_mapping = {}
    dir_blacklist = set()
    for rule in rules:
        target_dir, target_name = os.path.split(rule.target)
        if not target_dir:
            target_dir = '.'

        # Check if the target/deps follow a simple pattern
        for dep in rule.deps:
            dep_dir, dep_name = os.path.split(dep)
            if target_dir not in dir_mapping:
                dir_mapping[target_dir] = dep_dir
            elif dir_mapping[target_dir] != dep_dir:
                dir_blacklist.add(target_dir)

        # Pattern not met, just use all the literal dependencies
        src_dir = None
        new_deps = []
        if target_dir in dir_blacklist:
            new_deps = [repr(d) for d in rule.deps]
        # Otherwise, replace the dependency list with one in terms of a target directory
        elif rule.deps:
            src_dir = dir_mapping[target_dir]
            for dep in rule.deps:
                dep_dir, dep_name = os.path.split(dep)
                assert dep_dir == src_dir
                # Try to put the dep path in terms of the target
                if '.' in target_name:
                    prefix, _, suffix = target_name.rpartition('.')
                    if dep_name.startswith(prefix):
                        new_suffix = dep_name[len(prefix):]
                        dep_name = PatSubst(MetaVar('target'), '%' + suffix, '%' + new_suffix)

                new_deps.append(Join(MetaVar('src_dir'), '/', dep_name))

        rule.target_dir = target_dir
        rule.target_name = target_name
        rule.src_dir = src_dir
        rule.deps = new_deps
        del rule.target

    return dir_mapping, dir_blacklist

def deduplicate_rules(rules, dir_mapping, dir_blacklist):
    # Detect rules that differ only in target, so we can output loops instead of individual rules
    dir_mapping = {td: sd for td, sd in dir_mapping.items() if td not in dir_blacklist}
    rule_srcs = collections.defaultdict(lambda: collections.defaultdict(list))
    rule_map = {}
    if dir_mapping:
        # Collect all distinct rules
        for rule in rules:
            if rule.target_dir in dir_blacklist:
                continue
            assert not rule.deps or dir_mapping[rule.target_dir] == rule.src_dir, rule.deps
            key = rule_key(rule)
            rule_map[key] = rule
            rule_srcs[key][rule.target_dir].append(rule.target_name)

        # Prune the rule map to only include rules that are duplicated
        rule_srcs = {key: target_map for key, target_map in rule_srcs.items()
            if sum(len(srcs) for srcs in target_map.values()) > 1}

    return rule_map, rule_srcs

def write_rule(f, rule, indent):
    ind = ' ' * indent
    f.write(ind + 'rule_deps = %s\n' % format_list(rule.deps, indent=indent))
    if rule.pred_list_idx is not None:
        f.write(ind + 'rule_deps += _src_list_%s\n' % rule.pred_list_idx)
    if rule.oo_deps:
        f.write(ind + 'rule_oo_deps = %s\n' % format_list(rule.oo_deps, indent=indent))
        rule_oo_dep_str = ', order_only_deps=rule_oo_deps'
    else:
        rule_oo_dep_str = ''
    f.write(ind + 'rule_cmds = [\n')
    for cmd in rule.cmds:
        f.write(ind + '    %s,\n' % format_list(cmd, indent=indent+4))
    f.write(ind + ']\n')
    f.write(ind + 'ctx.add_rule(target, rule_deps, rule_cmds%s)\n' % rule_oo_dep_str)
    if rule.succ_list_idx is not None:
        f.write(ind + '_src_list_%s.append(target)\n' % rule.succ_list_idx)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', action='append', dest='defines', default=[], help='set a variable')
    parser.add_argument('--no-warnings', action='store_false', dest='warnings',
            help='disable all warnings during generation')
    parser.add_argument('-f', '--file', help='input file to parse')
    parser.add_argument('-o', '--output', default='out_rules.py',
            help='path to output rules.py file')
    args = parser.parse_args()

    ctx = ParseContext(enable_warnings=args.warnings)
    ctx.root_path = os.path.dirname(args.file)
    for d in args.defines:
        (k, v) = d.split('=', 1)
        ctx.variables[k] = v
    ctx.parse(args.file)

    # Process the parsed rules with a series of cleaning/simplifying/deduplicating steps:

    # Normalize paths, parse command lines, remove echos, etc.
    rules = ctx.get_cleaned_rules()

    # Find all arguments used, so we can build lists of common args
    args_used_by, var_set_idx = get_args_used_map(rules)

    # Reformulate commands in terms of variables
    process_rule_cmds(rules, var_set_idx)

    # Find all rules that have outputs used by other rules, to dynamically build dependency lists
    src_lists = process_rule_links(rules)

    # Find patterns with dependency/target directories (for now, just find any directories
    # where every target only depends on files in the same source directory)
    dir_mapping, dir_blacklist = process_rule_dirs(rules)

    # Now that we've simplified the rules a bunch, find all the rules that have the same
    # command/dependency structure, but differ only in source directory and target name
    rule_map, rule_srcs = deduplicate_rules(rules, dir_mapping, dir_blacklist)

    # Read gnu_make_lib.py, the library of functions potentially used at both compile time
    # and run time
    base_dir = os.path.dirname(sys.argv[0])
    with open('%s/gnu_make_lib.py' % base_dir) as f:
        lib = f.read()

    # Write out the processed rules into the output rules.py file
    with open(args.output, 'wt') as f:
        f.write(lib)
        f.write('\n')

        f.write('def rules(ctx):\n')

        for idx, (cmds, args) in enumerate(args_used_by.items()):
            f.write('    _vars_%s = %s\n' % (idx, format_list(args, indent=4)))

        for rule in src_lists:
            f.write('    _src_list_%s = []\n' % rule.pred_list_idx)

        # Write out lists of duplicated rules
        skip_rules = set()
        if rule_srcs:
            dir_mapping = {target_dir: src_dir for target_dir, src_dir in dir_mapping.items()
                if any(target_dir in target_map for key, target_map in rule_srcs.items())}
            f.write('    dir_mapping = %s\n' % format_dict(dir_mapping, indent=4, use_repr=True))

            # Find all rules that are used more than once, and write out some for loops to
            # process all the targets that use the same rule
            for [i, [key, target_map]] in enumerate(rule_srcs.items()):
                rule = rule_map[key]
                skip_rules.add(key)
                f.write('\n')
                f.write('    target_map_%s = %s\n' % (i, format_dict(target_map, indent=4, use_repr=True)))
                f.write('    for [target_dir, targets] in target_map_%s.items():\n' % i)
                f.write('        src_dir = dir_mapping[target_dir]\n')
                f.write('        for target_name in targets:\n')
                f.write('            target = "%s/%s" % (target_dir, target_name)\n')
                write_rule(f, rule, indent=12)

        # Output all the processed rules
        for rule in rules:
            if rule_key(rule) in skip_rules:
                continue

            f.write('\n')
            target = '%s/%s' % (rule.target_dir, rule.target_name)
            f.write('    # %s\n' % target)
            f.write('    target = %r\n' % target)
            if rule.src_dir is not None:
                f.write('    src_dir = %r\n' % rule.src_dir)
            write_rule(f, rule, indent=4)
