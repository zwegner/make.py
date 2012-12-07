#!/usr/bin/env python3
#
# make.py (http://code.google.com/p/make-py/)
# Copyright (c) 2012 Matt Craighead
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

import errno
import imp
import multiprocessing
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from optparse import OptionParser

visited = set()
enqueued = set()
completed = set()
rules = {}
task_queue = queue.Queue()
io_lock = threading.Lock()
any_errors = False

# An atomic write to stdout from any thread
def stdout_write(x):
    with io_lock:
        sys.stdout.write(x)
        sys.stdout.flush()

# By querying both a file's existence and its timestamp in a single syscall, we can get
# a significant speedup, especially for network file systems.
def get_timestamp_if_exists(path):
    try:
        return os.stat(path).st_mtime
    except OSError as e:
        if e.errno == errno.ENOENT:
            return -1
        raise

def normpath(path):
    path = os.path.normpath(path)
    if os.name == 'nt':
        path = path.lower()
        path = path.replace('\\', '/')
    return path

def joinpath(cwd, path):
    if path[0] == '/' or (os.name == 'nt' and path[1] == ':'):
        return path # absolute path
    return '%s/%s' % (cwd, path)

def run_cmd(rule):
    # Always delete the targets first
    for t in rule.targets:
        if os.path.exists(t):
            os.unlink(t)

    with io_lock:
        p = subprocess.Popen(rule.cmd, cwd=rule.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    # XXX What encoding should we use here??
    out = str(p.stdout.read(), 'utf-8').strip()

    if rule.vs_show_includes:
        deps = set()
        r = re.compile('^Note: including file:\\s*(.*)$')
        new_out = []
        for line in out.splitlines():
            m = r.match(line)
            if m:
                dep = normpath(m.group(1))
                if not dep.startswith('c:/program files'):
                    deps.add(dep)
            else:
                new_out.append(line)
        with io_lock:
            with open(rule.d_file, 'wt') as f:
                assert len(rule.targets) == 1
                f.write('%s: \\\n' % rule.targets[0])
                for dep in sorted(deps):
                    f.write('  %s \\\n' % dep)
                f.write('\n')

        # In addition to filtering out the /showIncludes messages, filter the one remaining
        # line of output where it just prints the source file name
        if len(new_out) == 1:
            out = ''
        else:
            out = '\n'.join(new_out)
    elif rule.stdout_filter:
        r = re.compile(rule.stdout_filter)
        new_out = []
        for line in out.splitlines():
            m = r.match(line)
            if not m:
                new_out.append(line)
        out = '\n'.join(new_out)
    built_text = "Built '%s'.\n" % "'\n  and '".join(rule.targets)

    code = p.wait()
    if code:
        global any_errors
        any_errors = True
        stdout_write("%s%s\n\n'%s' failed with exit code %d\n" % (built_text, out, ' '.join(rule.cmd), code))
        for t in rule.targets:
            if os.path.exists(t):
                os.unlink(t)
        exit(1)

    if out:
        built_text = '%s%s\n' % (built_text, out)
    stdout_write(built_text)

class Rule:
    def __init__(self, targets, deps, cwd, cmd, d_file, order_only_deps, vs_show_includes, stdout_filter):
        self.targets = targets
        self.deps = deps
        self.cwd = cwd
        self.cmd = cmd
        self.d_file = d_file
        self.order_only_deps = order_only_deps
        self.vs_show_includes = vs_show_includes
        self.stdout_filter = stdout_filter

class BuildContext:
    def __init__(self):
        pass

    def add_rule(self, targets, deps, cmd, d_file=None, order_only_deps=[], vs_show_includes=False, stdout_filter=None):
        if not isinstance(targets, list):
            targets = [targets]
        cwd = self.cwd
        targets = [normpath(joinpath(cwd, x)) for x in targets]
        if d_file:
            d_file = normpath(joinpath(cwd, d_file))
        order_only_deps = [normpath(joinpath(cwd, x)) for x in order_only_deps]
        rule = Rule(targets, deps, cwd, cmd, d_file, order_only_deps, vs_show_includes, stdout_filter)
        for t in targets:
            if t in rules:
                print("ERROR: multiple ways to build target '%s'" % t)
                exit(1)
            rules[t] = rule

def build(target, options):
    if target in visited or target in completed:
        return
    if target not in rules:
        visited.add(target)
        completed.add(target)
        return
    rule = rules[target]
    visited.update(rule.targets)

    # Get the dependencies list, including .d file dependencies
    deps = rule.deps
    if rule.d_file and os.path.exists(rule.d_file):
        with io_lock:
            with open(rule.d_file, 'rt') as f:
                extra_deps = f.read()
        extra_deps = extra_deps.replace('\\\n', '').split()[1:]
        deps = deps + extra_deps
    deps = [normpath(joinpath(rule.cwd, x)) for x in deps]

    # Recursively handle the dependencies, including .d file dependencies and order-only deps
    for dep in deps:
        build(dep, options)
    for dep in rule.order_only_deps:
        build(dep, options)
    if not all(dep in completed for dep in deps):
        return
    if not all(dep in completed for dep in rule.order_only_deps):
        return
    if target in enqueued:
        return

    # Don't build if already up to date
    target_timestamp = min(get_timestamp_if_exists(t) for t in rule.targets)
    if target_timestamp >= 0:
        for dep in deps:
            dep_timestamp = get_timestamp_if_exists(dep)
            if target_timestamp < dep_timestamp:
                break
        else:
            completed.add(target)
            return

    # Create the directories that the targets are going to live in, if they don't already exist
    for t in rule.targets:
        target_dir = os.path.dirname(t)
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)

    if options.parallel:
        # Enqueue this task to a builder thread
        task_queue.put(rule)
        enqueued.update(rule.targets)
    else:
        # Build the target immediately
        run_cmd(rule)
        completed.update(rule.targets)

class BuilderThread(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)

    def run(self):
        while not any_errors:
            rule = task_queue.get()
            if rule is None:
                break
            run_cmd(rule)
            completed.update(rule.targets)

def parse_rules_py(ctx, options, pathname, visited):
    if pathname in visited:
        return
    visited.add(pathname)
    if options.verbose:
        print("Parsing '%s'..." % pathname)
    description = ('.py', 'U', imp.PY_SOURCE)
    with open(pathname, 'r') as file:
        rules_py_module = imp.load_module('rules%d' % len(visited), file, pathname, description)

    dir = os.path.dirname(pathname)
    if hasattr(rules_py_module, 'submakes'):
        for f in rules_py_module.submakes():
            parse_rules_py(ctx, options, normpath(joinpath(dir, f)), visited)
    ctx.cwd = dir
    if hasattr(rules_py_module, 'rules'):
        rules_py_module.rules(ctx)

def main():
    # Parse command line
    parser = OptionParser(usage='%prog [options] target1_path [target2_path ...]')
    parser.add_option('-c', dest='clean', action='store_true', default=False, help='clean before building')
    parser.add_option('-f', dest='files', action='append', help='specify the path to a rules.py file', metavar='FILE')
    parser.add_option('-j', dest='jobs', type='int', default=None, help='specify the number of parallel jobs')
    parser.add_option('-v', dest='verbose', action='store_true', help='print verbose build output')
    parser.add_option('--no-parallel', dest='parallel', action='store_false', default=True, help='disable parallel build')
    (options, args) = parser.parse_args()
    if options.jobs is None:
        options.jobs = multiprocessing.cpu_count() # default to one job per CPU
    if options.files is None:
        parser.print_help()
        exit(1)
    cwd = os.getcwd()
    args = [normpath(joinpath(cwd, x)) for x in args]

    # Set up rule DB
    ctx = BuildContext()
    for f in options.files:
        parse_rules_py(ctx, options, normpath(joinpath(cwd, f)), visited)

    # Do the build
    if options.clean:
        for dir in sorted({'%s/_out' % os.path.dirname(x) for x in visited}):
            if os.path.exists(dir):
                stdout_write("Cleaning '%s'...\n" % dir)
                shutil.rmtree(dir)
    if options.parallel:
        # Create builder threads
        threads = []
        for i in range(options.jobs):
            t = BuilderThread()
            t.daemon = True
            t.start()
            threads.append(t)

        # Enqueue work to the builders
        while True:
            visited.clear()
            for target in args:
                build(target, options)

            # Check if done, sleep to prevent burning 100% of CPU, check again immediately after the sleep
            if any_errors or all(target in completed for target in args):
                break
            time.sleep(0.1)
            if any_errors or all(target in completed for target in args):
                break

        # Shut down the system by sending sentinel tokens to all the threads
        for i in range(options.jobs):
            task_queue.put(None)
        for t in threads:
            t.join()
    else:
        for target in args:
            build(target, options)

    if any_errors:
        exit(1)

if __name__ == '__main__':
    main()
