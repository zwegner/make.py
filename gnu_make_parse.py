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
import copy
import fnmatch
import glob
import inspect
import os
import re
import shlex
import sys

import gnu_make_lib

# This is fairly restrictive, just to be safe for now
re_rule = re.compile(r'^((?:[-\w./%$()]|\\ )+):(.*)$')

re_variable_assign = re.compile(r'(\w+)\s*(=|:=|\+=|\?=)\s*(.*)')

re_variable_subst = re.compile(r'([.%\w]*)=([.%\w]*)')

def expr_is_fn(expr, fn):
    return isinstance(expr, tuple) and expr[0] == fn

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

def Glob(expr):
    return ('glob', expr)

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
        elif expr_is_fn(arg, 'join'):
            r.extend(arg[1:])
        else:
            r.append(arg)
    if not r:
        return ''
    if len(r) == 1:
        return r[0]
    return ('join', *r)

# Create a lookup table for library functions
lib_fns = {}
fn_arg_limit = {}
for [name, fn] in gnu_make_lib.__dict__.items():
    if not name.startswith('_') and callable(fn):
        # Name ending with _ --> hack to get around Python keywords for stuff like $(or)
        if name.endswith('_'):
            name = name.replace('_', '')
        else:
            name = name.replace('_', '-')
        lib_fns[name] = fn
        argspec = inspect.getfullargspec(fn)
        assert not argspec.varkw and not argspec.kwonlyargs
        if argspec.varargs:
            fn_arg_limit[name] = 0
        else:
            fn_arg_limit[name] = len(argspec.args)

# Simple object for storing info on parsed rules. We don't need any functionality really.
class Rule:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

# Kinda dumb tokenization function--like str.find() but finds the first of multiple tokens
def find_first(s, tokens, start=0):
    first_token = first_idx = None
    for token in tokens:
        idx = s.find(token, start)
        if idx == -1:
            continue
        if first_idx is None or idx < first_idx:
            first_idx = idx
            first_token = token
    return (first_token, first_idx)

class ParseContext:
    def __init__(self, enable_warnings=True, root_path='.'):
        self.enable_warnings = enable_warnings
        self.info_stack = []
        self.variables = {'MAKE': 'make'}
        self.recursive_vars = {}
        self.current_rule = None
        self.rules = []
        self.if_stack = [True]
        self.else_stack = []
        self.cur_macro = None
        self.cur_macro_lines = None
        self.root_path = root_path

    def parse_atom(self, expr, start=0):
        # Find the first special character
        (token, i) = find_first(expr, ('$',), start=start)
        if i is None:
            return [None, expr[start:]]

        assert token == '$'

        expr_prefix = expr[start:i]
        subst = None
        arg_limit = 0

        # Get the name of the variable/expression being evaluated (single letter or parenthesized)
        # Also, reset expr to be the remainder of the line for the next loop iteration.
        if expr[i+1] in {'(', '{'}:
            fn_args = ['']
            start = i + 2

            expr_closer = ')' if expr[i+1] == '(' else '}'

            # For the first match, include whitespace.
            tokens = ('$', expr_closer, ':', ',', ' ', '\t')
            # To match make's weird parsing rules, allow a limit to the number of
            # commas that are matched
            while True:
                (token, j) = find_first(expr, tokens, start=start)
                if j is None:
                    self.error('parse error: unclosed expression')

                fn_args[-1] = Join(fn_args[-1], expr[start:j])
                start = j + 1

                # Whitespace: we should now have a function name, set an arg limit
                if token == ' ' or token == '\t':
                    assert len(fn_args) == 1
                    arg_limit = fn_arg_limit.get(fn_args[0], 0)
                    fn_args.append('')
                # Comma: normal arg
                elif token == ',':
                    fn_args.append('')
                # Variable: recursively parse a sub-expression
                elif token == '$':
                    [start, part] = self.parse_atom(expr, j)
                    fn_args[-1] = Join(fn_args[-1], part)
                # Substitutions, like $(SRCS:%.c=%.o)
                elif token == ':':
                    fn_args.append(':')
                    fn_args.append('')
                    subst = True
                else:
                    break

                # If we've reached the number of arguments for this function, stop parsing commas
                if arg_limit and len(fn_args) > arg_limit:
                    tokens = ('$', expr_closer, ':')
                # Otherwise, just discard spaces
                else:
                    tokens = ('$', expr_closer, ':', ',')

            assert token == ')'

            end = j+1

            name = fn_args.pop(0)

            if fn_args and isinstance(fn_args[0], str):
                fn_args[0] = fn_args[0].lstrip() 

            if subst:
                index = fn_args.index(':')
                [pattern] = fn_args[index+1:]
                del fn_args[index:]
                m = re_variable_subst.match(pattern)
                assert m
                [old, new] = m.groups()
                if '%' in old:
                    assert old.count('%') == 1
                else:
                    old = '%' + old
                    new = '%' + new
                subst = (old, new)

        # No parentheses/braces: just a one-letter variable
        else:
            fn_args = []
            end = i+2
            name = expr[i+1]

        # Literal $
        if name == '$':
            value = '$'

        # Special variables
        elif name == '@':
            value = MetaVar('target')
        elif name == '^':
            value = UnpackList(MetaVar('deps'))

        # Basic library functions
        elif name in lib_fns:
            assert arg_limit == 0 or len(fn_args) == arg_limit
            value = (lib_fns[name], *fn_args)

        elif name == 'call':
            fn = fn_args[0]
            if fn not in self.variables:
                self.error('function %r does not exist' % fn)
            # Create a new variable context with $(1) etc filled in with args
            old_vars = self.variables
            self.variables = self.variables.copy()
            for [i, arg] in enumerate(fn_args):
                self.variables[str(i)] = arg
            # Evaluate
            fn = self.variables.get(fn, '')
            value = self.eval(fn)
            self.variables = old_vars
        elif fn_args:
            self.error('unknown function %r' % (name,))
        # Normal variables
        else:
            value = Var(name)

        # Wrap expression with a substitution when necessary
        if subst:
            (old, new) = subst
            value = (gnu_make_lib.patsubst, old, new, value)

        return [end, Join(expr_prefix, value)]

    def parse_expr(self, expr):
        result = []
        # Construct the result list
        start = 0
        while True:
            [start, atom] = self.parse_atom(expr, start)
            result.append(atom)
            if start is None:
                break
        return Join(*result)

    def eval(self, expr, rule=None):
        if isinstance(expr, str):
            return expr
        if isinstance(expr, list):
            return [self.eval(item, rule=rule) for item in expr]
        assert isinstance(expr, tuple), expr
        [fn, *args] = expr

        # Evaluate arguments. Also handle the "unpack" function in the arguments, as
        # that must be handled in the parent (i.e. here). Ugh
        new_args = []
        for arg in args:
            arg = self.eval(arg, rule=rule)
            if expr_is_fn(arg, 'unpack') and isinstance(arg[1], list):
                for a in arg[1]:
                    new_args.append(a)
                    # XXX HACK! is this always appropriate?
                    if fn == 'join':
                        new_args.append(' ')
            else:
                new_args.append(arg)
        args = new_args

        # Library function--only call it when all args are fully evaluated 
        if callable(fn):
            if not all(isinstance(arg, (str, list)) for arg in args):
                return expr
            value = fn(*args)
        elif fn == 'join':
            # We'll hope that Join() can simplify this enough. If not, we still return
            # here, since otherwise we get infinite recursion
            return Join(*args)
        elif fn == 'var':
            [name] = args
            if name not in self.variables:
                self.warning('variable %r does not exist' % (name,))
            value = self.variables.get(name, '')
        elif fn == 'unpack':
            [arg] = args
            value = arg
            return (fn, *args)
        elif fn == 'metavar':
            # Only evaluate metavars when given a rule, by looking up the attribute
            if rule:
                [attr] = args
                return getattr(rule, attr)
            return expr
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
        if isinstance(path, str):
            return os.path.normpath('%s/%s' % (self.root_path, path))
        return Join(self.root_path + '/', path)

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
                paths = split_spaces(self.parse_and_eval(line[8:].lstrip()))
                for path in paths:
                    include_path = self.get_norm_path(path)
                    if os.path.exists(include_path):
                        self.parse(include_path)
                    elif not line.startswith('-'):
                        self.error('include file %r does not exist' % include_path)
                    else:
                        self.warning('include file %r does not exist' % include_path)
        elif line.startswith('$(warning '):
            assert line.endswith(')')
            line = self.parse_and_eval(line[10:-1])
            if self.if_stack[-1]:
                self.warning(repr(line))
        elif line.startswith('$(error '):
            assert line.endswith(')')
            line = self.parse_and_eval(line[8:-1])
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
                    line = line[1:]
                    
                    self.current_rule.cmds.append(line)
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
                        self.recursive_vars[name] = False
                    elif assign == '+=':
                        # Match the += behavior: based on the previous definition of
                        # the variable we're appending to, += is either recursive or not.
                        # In case you haven't noticed, make is awful
                        if self.recursive_vars.get(name, False):
                            assert name in self.variables
                            self.variables[name] = Join(self.variables[name], ' ', expr)
                        else:
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
                        self.recursive_vars[name] = True
            else:
                m = re_rule.match(line)
                if m is not None:
                    assert not self.current_rule
                    target, deps = m.groups()
                    [target] = split_spaces(self.parse_and_eval(target))
                    target = parse_globs(target)

                    deps = split_spaces(self.parse_and_eval(deps))
                    deps = [parse_globs(dep) for dep in deps]

                    # Check for order-only deps
                    if '|' in deps:
                        idx = deps.index('|')
                        [deps, oo_deps] = deps[:idx], deps[idx+1:]
                        if '|' in oo_deps:
                            self.error('multiple | separators in line')
                    else:
                        oo_deps = []
                    self.current_rule = Rule(target=target, deps=deps, oo_deps=oo_deps, cmds=[])
                elif line:
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

            # Remove newline from the end
            line = line.rstrip('\n')

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

            if not line:
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
            # Create a copy of the target expression with any patterns transformed to use
            # the 'target_glob' variable
            rule.sub_target = expand_globs(rule.target, 'target_glob')

            # Evaluate expressions for dependencies, transforming globs as above
            rule.deps = [expand_globs(dep, 'target_glob') for dep in rule.deps]

            # Do an initial evaluation of the command lines. Be sure to pass rule=None
            # here, which is a weird hacky way of signifying to the evaluation
            # function that we don't want to substitute variables yet
            rule.cmds = eval_cmds(self, rule.cmds, rule=None)

            # Ignore +, -, and @ at the beginning of commands
            for cmd in enumerate(rule.cmds):
                if isinstance(cmd[0], str):
                    cmd[0] = cmd[0].lstrip('+-@')

            if not rule.cmds:
                continue

            rules.append(rule)

        return rules

def split_spaces(text):
    if not isinstance(text, str):
        return text
    # XXX handle escaping exactly as make does
    return text.split()

def parse_globs(expr):
    if isinstance(expr, str):
        result = []
        for part in [expr]:
            if isinstance(part, str) and any(token in part for token in ['%', '*']):
                part = Glob(part)
            result.append(part)
        return Join(*result)
    return expr

# Recursively evaluate a pseudo-AST, replacing usage of globs with a variable substitution
def expand_globs(expr, glob_var):
    if not isinstance(expr, tuple):
        return expr
    [fn, *args] = expr
    if fn == 'glob':
        [pattern] = args
        [prefix, suffix] = pattern.split('%', 1)
        return Join(prefix, MetaVar(glob_var), suffix)
    else:
        return (fn, *(expand_globs(arg, glob_var) for arg in args))

def format_expr(expr, indent=0):
    if isinstance(expr, str):
        return repr(expr)
    elif isinstance(expr, list):
        return format_list(expr, indent=indent)
    assert isinstance(expr, tuple), expr
    [fn, *args] = expr
    if fn == 'join':
        return "''.join(%s)" % format_list(args, indent=indent)
    elif fn == 'metavar':
        [var] = args
        return var
    elif fn == 'unpack':
        [value] = args
        return '*' + format_expr(value, indent=indent)
    elif callable(fn):
        assert inspect.getmodule(fn) == gnu_make_lib
        args = [format_expr(a, indent=indent) for a in args]
        return '%s(%s)' % (fn.__name__, ', '.join(args))
    else:
        assert 0, fn

def format_list(items, indent=0):
    assert isinstance(items, list)
    items = [format_expr(item, indent=indent) for item in items]
    if sum(len(item)+2 for item in items) + indent < 100:
        return '[%s]' % ', '.join(items)
    indent = ' ' * indent
    items = ['%s    %s,\n' % (indent, item) for item in items]
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
        if not cmds_are_simplified(rule.cmds):
            continue
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
        if not isinstance(rule.cmds, list):
            continue
        for cmd in rule.cmds:
            nice_cmd = []
            add_d_file = False
            d_file_path = None
            var_sets_used = set()
            if not isinstance(cmd, list):
                new_cmds.append(cmd)
                continue
            for arg in cmd:
                # Check for arguments with not-yet-evaluated expressions
                if not isinstance(arg, str):
                    assert isinstance(arg, (tuple, list))
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
                dep = None
                # Try to put the dep path in terms of the target
                if '.' in target_name:
                    prefix, _, suffix = target_name.rpartition('.')
                    if dep_name.startswith(prefix):
                        new_suffix = dep_name[len(prefix):]
                        dep = (gnu_make_lib.patsubst, '%.' + suffix, '%' + new_suffix, MetaVar('target'))

                if dep is None:
                    dep = Join(MetaVar('src_dir'), '/', dep_name)

                new_deps.append(dep)

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

# Find glob-based targets within rules, and create sets of matching rules
# based on the dependencies of all rules
def match_glob_targets(rules):
    globs = []
    new_rules = []
    for rule in rules:
        # See if this rule has a glob in the target (like %.o: ...)
        if expr_is_fn(rule.target, 'glob'):
            [_, target_glob] = rule.target
            globs.append((target_glob, rule, set()))
        else:
            new_rules.append(rule)
    rules = new_rules

    # For each glob-based rule, see which dependencies match the pattern. If
    # so, add them to the set of matches
    for [target_pat, glob_rule, matches] in globs:
        # Don't support * and % together, let's hope nobody is that crazy
        assert '*' not in target_pat
        target_pat = target_pat.replace('%', '*', 1)
        prefix, _, suffix = target_pat.partition('*')

        for rule in rules:
            for dep in rule.deps:
                # XXX fnmatch or fnmatchcase?
                if isinstance(dep, str) and fnmatch.fnmatchcase(dep, target_pat):
                    assert dep.startswith(prefix) and dep.endswith(suffix)
                    glob_value = dep[len(prefix):-len(suffix) or None]

                    matches.add(glob_value)

    return [rules, globs]

def cmds_are_simplified(cmds):
    return isinstance(cmds, list) and all(isinstance(cmd, list) for cmd in cmds)

# Parse and evaluate a list of command lines. Evaluation will substitute all
# the local, make-level variables, like say $(SRCS) appearing in the makefile.
# Evaluation won't replace metavariables or things like that, i.e. stuff like
# $@, $^, globs, etc., which will generally get replaced by Python code. It's
# important that we don't evaluate those constructs now, as they help a lot
# with rule deduplication.
def eval_cmds(ctx, cmds, rule=None):
    new_cmds = []
    if not isinstance(cmds, list):
        return cmds
    for cmd in cmds:
        if isinstance(cmd, str):
            cmd = ctx.parse_expr(cmd)
        if not isinstance(cmd, list):
            cmd = ctx.eval(cmd, rule=rule)
        new_cmds.append(cmd)

    if not cmds_are_simplified(new_cmds):
        new_cmds = (gnu_make_lib._split_cmds, new_cmds)
    return new_cmds

def finalize_rule(ctx, rule):
    rule.deps = [ctx.eval(dep, rule=rule) for dep in rule.deps]
    rule.cmds = ctx.eval(rule.cmds, rule=rule)

def get_finalized_rules(ctx):
    rules = ctx.get_cleaned_rules()
    [rules, glob_rules] = match_glob_targets(rules)

    all_rules = []
    # For each rule with a pattern in the target and each file that matches the
    # pattern, create a sub-rule based on that match (basically filling out a template)
    for [target, glob_rule, matches] in glob_rules:
        for match in sorted(matches):
            # Create a copy of the rule, and set the 'target_glob' variable on it,
            # which will be used during evaluation for any glob expression
            # within the rule (via ctx.eval())
            rule = copy.copy(glob_rule)
            rule.target_glob = match
            # Also replace the target with a concrete instantiation
            rule.target = target.replace('%', match, 1)

            finalize_rule(ctx, rule)

            all_rules.append(rule)

    # Do a final eval for all the regular rule commands too
    for rule in rules:
        finalize_rule(ctx, rule)

    all_rules.extend(rules)
    return all_rules

def write_rule(f, rule, indent):
    ind = ' ' * indent
    f.write(ind + 'deps = %s\n' % format_list(rule.deps, indent=indent))
    if rule.pred_list_idx is not None:
        f.write(ind + 'deps += _src_list_%s\n' % rule.pred_list_idx)
    if rule.oo_deps:
        f.write(ind + 'rule_oo_deps = %s\n' % format_list(rule.oo_deps, indent=indent))
        rule_oo_dep_str = ', order_only_deps=rule_oo_deps'
    else:
        rule_oo_dep_str = ''
    f.write(ind + 'rule_cmds = %s\n' % format_expr(rule.cmds, indent=indent))
    f.write(ind + 'ctx.add_rule(target, deps, rule_cmds%s)\n' % rule_oo_dep_str)
    if rule.succ_list_idx is not None:
        f.write(ind + '_src_list_%s.append(target)\n' % rule.succ_list_idx)

def convert_rules(ctx, f):
    # Process the parsed rules with a series of cleaning/simplifying/deduplicating steps:

    # Normalize paths, parse command lines, remove echos, etc.
    rules = ctx.get_cleaned_rules()

    # Find all arguments used, so we can build lists of common args
    args_used_by, var_set_idx = get_args_used_map(rules)

    # Reformulate commands in terms of variables
    process_rule_cmds(rules, var_set_idx)

    # Find all rules that have outputs used by other rules, to dynamically build dependency lists
    src_lists = process_rule_links(rules)

    rules, glob_rules = match_glob_targets(rules)

    # Find patterns with dependency/target directories (for now, just find any directories
    # where every target only depends on files in the same source directory)
    dir_mapping, dir_blacklist = process_rule_dirs(rules)

    # Now that we've simplified the rules a bunch, find all the rules that have the same
    # command/dependency structure, but differ only in source directory and target name
    rule_map, rule_srcs = deduplicate_rules(rules, dir_mapping, dir_blacklist)

    # Read gnu_make_lib.py, the library of functions potentially used at both compile time
    # and run time
    # XXX use inspect.getsourcelines() and dead code elimination
    base_dir = os.path.dirname(sys.argv[0])
    with open('%s/gnu_make_lib.py' % base_dir) as lib_f:
        lib = lib_f.read()

    # Write out the processed rules into the output rules.py file
    f.write(lib)
    f.write('\n')

    f.write('def rules(ctx):\n')

    if not rules and not glob_rules:
        f.write('    pass\n')

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

    # Write out lists of glob rules
    for [target, glob_rule, matches] in glob_rules:
        f.write('\n')
        f.write('    # %s\n' % target)
        f.write('    matches = %s\n' % format_list(sorted(matches), indent=4))
        f.write('    for target_glob in matches:\n')
        f.write('        target = %s\n' % format_expr(glob_rule.sub_target, indent=8))
        write_rule(f, glob_rule, indent=8)

    # Output all the processed rules
    for rule in rules:
        if rule_key(rule) in skip_rules:
            continue

        f.write('\n')
        target = os.path.normpath('%s/%s' % (rule.target_dir, rule.target_name))
        f.write('    # %s\n' % target)
        f.write('    target = %r\n' % target)
        if rule.src_dir is not None:
            f.write('    src_dir = %r\n' % rule.src_dir)
        write_rule(f, rule, indent=4)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', action='append', dest='defines', default=[], help='set a variable')
    parser.add_argument('--no-warnings', action='store_false', dest='warnings',
            help='disable all warnings during generation')
    parser.add_argument('-f', '--file', help='input file to parse')
    parser.add_argument('-o', '--output', default='out_rules.py',
            help='path to output rules.py file')
    args = parser.parse_args()

    root_path = os.path.dirname(args.file) or '.'
    ctx = ParseContext(enable_warnings=args.warnings, root_path=root_path)
    for d in args.defines:
        (k, v) = d.split('=', 1)
        ctx.variables[k] = v
    ctx.parse(args.file)

    # Write out the processed rules into the output rules.py file
    with open(args.output, 'wt') as f:
        convert_rules(ctx, f)

if __name__ == '__main__':
    main()
