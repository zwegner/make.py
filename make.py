#!/usr/bin/env python3
#
# make.py (http://code.google.com/p/make-py/)
# $Revision$
# Copyright (c) 2012-2013 Matt Craighead
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
import hashlib
import itertools
import multiprocessing
import os
import pickle
import pipes
import queue
import re
import shlex
import shutil
import struct
import subprocess
import sys
import threading
import time
from optparse import OptionParser

# The imp module is deprecated, but importlib doesn't have the functionality we need.
# Or rather, it might, but the documentation doesn't make it clear. So just suppress the warning.
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import imp

visited = set()
enqueued = set()
completed = set()
building = set()
rules = {}
make_db = {}
normpath_cache = {}
task_queue = queue.PriorityQueue()
priority_queue_counter = 0 # tiebreaker counter to fall back to FIFO when rule priorities are the same
any_errors = False

# This is used to work around some Python bugs:
# 1. It would be nice if sys.stdout.write from multiple threads were atomic, but I've observed problems.
# 2. On Windows, if one thread calls subprocess.Popen while another thread has a file handle from open()
# open, the file handle will be incorrectly and unintentionally inherited by the child process.  This
# leads to really strange file locking errors.
# XXX Split io_lock into stdout_lock and subprocess_io_lock
# XXX Maybe make one or both conditional on platform (certainly I don't think Unix has the subprocess bug)
io_lock = threading.Lock()

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
    if path in normpath_cache:
        return normpath_cache[path]
    ret = os.path.normpath(path)
    if os.name == 'nt':
        ret = ret.lower().replace('\\', '/')
    normpath_cache[path] = ret
    return ret

if os.name == 'nt': # evaluate this condition only once, rather than per call, for performance
    def joinpath(cwd, path):
        return path if (path[0] == '/' or path[1] == ':') else '%s/%s' % (cwd, path)
else:
    def joinpath(cwd, path):
        return path if path[0] == '/' else '%s/%s' % (cwd, path)

def remove_path(path):
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.exists(path):
        os.unlink(path)

def run_cmd(rule, options):
    # Always delete the targets first
    local_make_db = make_db[rule.cwd]
    for t in rule.targets:
        remove_path(t)
        if t in local_make_db:
            del local_make_db[t]

    built_text = "Built '%s'.\n" % "'\n  and '".join(rule.targets)
    if progress_line: # need to precede "Built [...]" with erasing the current progress indicator
        built_text = '\r%s\r%s' % (' ' * usable_columns, built_text)

    all_out = []
    for cmd in rule.cmds:
        # Run command, capture/filter its output, and get its exit code.
        # XXX Do we want to add an additional check that all the targets must exist?
        with io_lock:
            try:
                p = subprocess.Popen(cmd, cwd=rule.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            except Exception as e:
                p = None
                out = str(e)
                code = 1
        if p is not None:
            out = p.stdout.read().decode().strip() # XXX What encoding should we use here??  This assumes UTF-8
            code = p.wait()
        if rule.msvc_show_includes:
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
            out = '\n'.join(line for line in out.splitlines() if not r.match(line))

        if options.verbose or code:
            if os.name == 'nt':
                out = '%s\n%s' % (subprocess.list2cmdline(cmd), out)
            else:
                out = '%s\n%s' % (' '.join(pipes.quote(x) for x in cmd), out)
            out = out.rstrip()
        if out:
            all_out.append(out)
        if code:
            global any_errors
            any_errors = True
            stdout_write("%s%s\n\n" % (built_text, '\n'.join(all_out)))
            for t in rule.targets:
                remove_path(t)
            exit(1)

    for t in rule.targets:
        local_make_db[t] = rule.signature()
    if all_out:
        stdout_write('%s%s\n\n' % (built_text, '\n'.join(all_out)))
    elif not progress_line:
        stdout_write(built_text)

class Rule:
    def __init__(self, targets, deps, cwd, cmds, d_file, order_only_deps, msvc_show_includes, stdout_filter, latency):
        self.targets = targets
        self.deps = deps
        self.cwd = cwd
        self.cmds = cmds
        self.d_file = d_file
        self.order_only_deps = order_only_deps
        self.msvc_show_includes = msvc_show_includes
        self.stdout_filter = stdout_filter
        self.latency = latency
        self.priority = 0

    # order_only_deps, stdout_filter, priority are excluded from signatures because none of them should affect the targets' new content.
    def signature(self):
        info = (self.targets, self.deps, self.cwd, self.cmds, self.d_file, self.msvc_show_includes)
        return hashlib.sha1(pickle.dumps(info)).hexdigest()

class BuildContext:
    def __init__(self, vars):
        self.vars = vars

    def add_rule(self, targets, deps, cmds, d_file=None, order_only_deps=[], msvc_show_includes=False, stdout_filter=None, latency=1):
        cwd = self.cwd
        if not isinstance(targets, list):
            assert isinstance(targets, str) # we expect targets to be either a str (a single target) or a list of targets
            targets = [targets]
        targets = [normpath(joinpath(cwd, x)) for x in targets]
        assert isinstance(deps, list) # we expect deps to be a list of deps
        assert isinstance(cmds, list) # cmds is intended to be a list of lists of arg strings
        if isinstance(cmds[0], str):
            cmds = [cmds] # but, we allow just a single command as a list of strings -- wrap it with another list
        if d_file is not None:
            assert isinstance(d_file, str) # we expect d_file to be ether None or a str (the path of the .d file)
            d_file = normpath(joinpath(cwd, d_file))
        assert isinstance(order_only_deps, list)
        order_only_deps = [normpath(joinpath(cwd, x)) for x in order_only_deps]
        assert stdout_filter is None or isinstance(stdout_filter, str)

        rule = Rule(targets, deps, cwd, cmds, d_file, order_only_deps, msvc_show_includes, stdout_filter, latency)
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
    if target in enqueued:
        return

    # Recursively handle the dependencies, including .d file dependencies and order-only deps
    deps = [normpath(joinpath(rule.cwd, x)) for x in rule.deps]
    d_file_deps = []
    if rule.d_file and os.path.exists(rule.d_file):
        with io_lock:
            with open(rule.d_file, 'rt') as f:
                d_file_deps = f.read()
        d_file_deps = d_file_deps.replace('\\\n', '')
        if '\\' in d_file_deps: # shlex.split is slow, don't use it unless we really need it
            d_file_deps = shlex.split(d_file_deps)[1:]
        else:
            d_file_deps = d_file_deps.split()[1:]
        d_file_deps = [normpath(joinpath(rule.cwd, x)) for x in d_file_deps]
    for dep in itertools.chain(deps, d_file_deps, rule.order_only_deps):
        build(dep, options)
    if any(dep not in completed for dep in itertools.chain(deps, d_file_deps, rule.order_only_deps)):
        return

    # Don't build if already up to date
    # Slightly different rules for regular deps vs. d_file_deps -- always rebuild when a d_file_dep is nonexistent,
    # whereas we want to fail with an error when a regular dep is nonexistent
    target_timestamp = min(get_timestamp_if_exists(t) for t in rule.targets)
    dep_timestamps = [get_timestamp_if_exists(dep) for dep in deps]
    for (dep, dep_timestamp) in zip(deps, dep_timestamps):
        if dep_timestamp < 0:
            if progress_line:
                stdout_write("\r%s\rERROR: dependency '%s' of '%s' is nonexistent\n" % (' ' * usable_columns, dep, ' '.join(rule.targets)))
            else:
                stdout_write("ERROR: dependency '%s' of '%s' is nonexistent\n" % (dep, ' '.join(rule.targets)))
            global any_errors
            any_errors = True
            exit(1)
    if target_timestamp >= 0 and all(dep_timestamp <= target_timestamp for dep_timestamp in dep_timestamps):
        if all(0 <= get_timestamp_if_exists(dep) <= target_timestamp for dep in d_file_deps):
            if all(make_db[rule.cwd].get(t) == rule.signature() for t in rule.targets):
                completed.add(target)
                return

    # Create the directories that the targets are going to live in, if they don't already exist
    for t in rule.targets:
        target_dir = os.path.dirname(t)
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)

    if options.parallel:
        # Enqueue this task to a builder thread -- note that PriorityQueue needs the sense of priority reversed
        global priority_queue_counter
        task_queue.put((-rule.priority, priority_queue_counter, rule))
        priority_queue_counter += 1
        enqueued.update(rule.targets)
    else:
        # Build the target immediately
        run_cmd(rule, options)
        completed.update(rule.targets)

class BuilderThread(threading.Thread):
    def __init__(self, options):
        threading.Thread.__init__(self)
        self.options = options

    def run(self):
        while not any_errors:
            (priority, counter, rule) = task_queue.get()
            if rule is None:
                break
            building.update(rule.targets)
            run_cmd(rule, self.options)
            building.difference_update(rule.targets)
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
    if dir not in make_db:
        make_db[dir] = {}
        path = '%s/_out/make.db' % dir
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    (target, signature) = line.rstrip().rsplit(' ', 1)
                    make_db[dir][target] = signature
    if hasattr(rules_py_module, 'submakes'):
        for f in rules_py_module.submakes():
            parse_rules_py(ctx, options, normpath(joinpath(dir, f)), visited)
    ctx.cwd = dir
    if hasattr(rules_py_module, 'rules'):
        rules_py_module.rules(ctx)

# returns width-1 for interactive console, or None if stdout is redirected
def get_usable_columns():
    if os.name == 'nt':
        import ctypes

        h_stdout = ctypes.windll.kernel32.GetStdHandle(-11)
        csbi = ctypes.create_string_buffer(22)
        if not ctypes.windll.kernel32.GetConsoleScreenBufferInfo(h_stdout, csbi):
            return None

        (size_x, size_y, cursor_x, cursor_y, attr, win_left, win_top, win_right, win_bottom, win_max_x, win_max_y) = \
            struct.unpack('hhhhHhhhhhh', csbi.raw)
        return win_right - win_left
    elif sys.platform.startswith('linux') or sys.platform.startswith('darwin'):
        import fcntl, termios
        try:
            cr = struct.unpack('hh', fcntl.ioctl(1, termios.TIOCGWINSZ, '1234'))
        except:
            return None
        return cr[1] - 1
    else:
        return None # XXX maybe we can just use TIOCGWINSZ on *all* Unix platforms?  not sure if any of them don't support it

def propagate_latencies(target, latency):
    if target not in rules:
        return
    rule = rules[target]
    latency += rule.latency
    if latency <= rule.priority:
        return # nothing to do -- we are not increasing the priority of this rule
    rule.priority = latency # update this rule's latency

    # Recursively handle the dependencies, including order-only deps
    deps = [normpath(joinpath(rule.cwd, x)) for x in rule.deps]
    for dep in itertools.chain(deps, rule.order_only_deps):
        propagate_latencies(dep, latency)

def main():
    # Parse command line
    parser = OptionParser(usage='%prog [options] target1_path [target2_path ...]')
    parser.add_option('-c', dest='clean', action='store_true', default=False, help='clean before building')
    parser.add_option('-f', dest='files', action='append', help='specify the path to a rules.py file (default is "rules.py")', metavar='FILE')
    parser.add_option('-j', dest='jobs', type='int', default=None, help='specify the number of parallel jobs (defaults to one per CPU)')
    parser.add_option('-v', dest='verbose', action='store_true', help='print verbose build output')
    parser.add_option('--var', dest='vars', type='str', action='append', default=[], metavar='KEY=VALUE',
            help='option in the form key=value, sets a variable in the ctx.vars dictionary for passing to rules')
    parser.add_option('--no-parallel', dest='parallel', action='store_false', default=True, help='disable parallel build')
    (options, args) = parser.parse_args()
    if options.jobs is None:
        options.jobs = multiprocessing.cpu_count() # default to one job per CPU
    if options.files is None:
        options.files = ['rules.py'] # default to "-f rules.py"
    cwd = os.getcwd()
    args = [normpath(joinpath(cwd, x)) for x in args]
    # Parse variables passed with the --var option
    variables = {k: v for var in options.vars for k, _, v in [var.partition('=')]}

    # Presumably -v should shut off the progress indicator; supporting it w/ --no-parallel seems like extra work for no gain.
    global progress_line, usable_columns
    usable_columns = get_usable_columns()
    progress_line = usable_columns is not None and not options.verbose and options.parallel

    # Set up rule DB, reading in make.db files as we go
    ctx = BuildContext(variables)
    for f in options.files:
        parse_rules_py(ctx, options, normpath(joinpath(cwd, f)), visited)
    for target in args:
        if target not in rules:
            print("ERROR: no rule to build target '%s'" % target)
            exit(1)
        propagate_latencies(target, 0)

    # Clean up stale targets from previous builds that no longer have rules; also do an explicitly requested clean
    for (cwd, db) in make_db.items():
        if options.clean:
            dir = '%s/_out' % cwd
            if os.path.exists(dir):
                stdout_write("Cleaning '%s'...\n" % dir)
                shutil.rmtree(dir)
            db.clear()
        for (target, signature) in list(db.items()):
            if target not in rules:
                if os.path.exists(target):
                    print("Deleting stale target '%s'..." % target)
                    remove_path(target)
                del db[target]

    if options.parallel:
        # Create builder threads
        threads = []
        for i in range(options.jobs):
            t = BuilderThread(options)
            t.daemon = True
            t.start()
            threads.append(t)

    # Do the build, and try to shut down as cleanly as possible if we get a Ctrl-C
    try:
        if options.parallel:
            # Enqueue work to the builders
            while True:
                visited.clear()
                for target in args:
                    build(target, options)

                # Show progress update and exit if done, otherwise sleep to prevent burning 100% of CPU
                # Be careful about iterating over data structures being edited concurrently by the BuilderThreads
                if any_errors:
                    break
                if progress_line:
                    incomplete_count = sum(1 for x in (visited - completed) if x in rules)
                    if incomplete_count:
                        progress = ' '.join(sorted(x.rsplit('/', 1)[-1] for x in set(building)))
                        progress = 'make.py: %d left, building: %s' % (incomplete_count, progress)
                    else:
                        progress = ''
                    if len(progress) < usable_columns:
                        pad = usable_columns - len(progress)
                        progress += ' ' * pad # erase old contents
                        progress += '\b' * pad # put cursor back at end of line
                    else:
                        progress = progress[0:usable_columns]
                    stdout_write('\r%s' % progress)
                if all(target in completed for target in args):
                    break
                time.sleep(0.1)
        else:
            for target in args:
                build(target, options)
    finally:
        if options.parallel:
            # Shut down the system by sending sentinel tokens to all the threads
            for i in range(options.jobs):
                task_queue.put((1000000, 0, None)) # lower priority than any real rule
            for t in threads:
                t.join()

        # Write out the final make.db files
        # XXX May want to do this "occasionally" as the build is running?  (not too often to avoid a perf hit, but often
        # enough to avoid data loss)
        for (cwd, db) in make_db.items():
            if not os.path.exists('%s/_out' % cwd):
                os.mkdir('%s/_out' % cwd)
            with open('%s/_out/make.db' % cwd, 'w') as f:
                for (target, signature) in db.items():
                    f.write('%s %s\n' % (target, signature))

    if any_errors:
        exit(1)

if __name__ == '__main__':
    main()
