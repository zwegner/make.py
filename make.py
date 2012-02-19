#!/usr/bin/env python3
#
# make.py (http://code.google.com/p/make-py/)
# Copyright 2012 Matt Craighead
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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

def run_cmd(rule):
    # Always delete the targets first
    for t in rule.targets:
        if os.path.exists(t):
            os.unlink(t)

    with io_lock:
        p = subprocess.Popen(rule.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

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
    def __init__(self, targets, deps, cmd, d_file, order_only_deps, vs_show_includes, stdout_filter):
        self.targets = targets
        self.deps = deps
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
        rule = Rule(targets, deps, cmd, d_file, order_only_deps, vs_show_includes, stdout_filter)
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

def main():
    # Parse command line
    parser = OptionParser()
    parser.add_option('-c', dest='clean', action='store_true', default=False, help='clean before building')
    parser.add_option('-f', dest='files', action='append', help='specify the path to a rules.py file', metavar='FILE')
    parser.add_option('-j', dest='jobs', type='int', default=None, help='specify the number of parallel jobs')
    parser.add_option('-v', dest='verbose', action='store_true', help='print verbose build output')
    parser.add_option('--no-parallel', dest='parallel', action='store_false', default=True, help='disable parallel build')
    (options, args) = parser.parse_args()
    if options.jobs is None:
        options.jobs = multiprocessing.cpu_count() # default to one job per CPU

    # Set up rule DB
    ctx = BuildContext()
    cwd = os.getcwd()
    for f in options.files:
        pathname = normpath(os.path.join(cwd, f))
        if options.verbose:
            print("Parsing '%s'..." % pathname)
        description = ('.py', 'U', imp.PY_SOURCE)
        with open(pathname, 'r') as file:
            rules_py_module = imp.load_module('rules', file, pathname, description)
            rules_py_module.rules(ctx)

    # Do the build
    if options.clean and os.path.exists('_out'):
        stdout_write("Cleaning '_out'...\n")
        shutil.rmtree('_out')
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
