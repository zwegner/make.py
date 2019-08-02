*This is a mirror/fork of Matt Craighead's project [make-py](https://code.google.com/archive/p/make-py/).*

make.py is a build tool written in Python that uses "rules.py" Python scripts to specify its build rules. Because rules.py files are written in Python, they have full access to all of its powerful language features.

make.py intends to be fast, powerful, reliable, and yet minimalistic:
* Parallel builds are supported and enabled by default to take full advantage of multicore CPUs.
* The parallel build engine properly handles parallelization between rules specified from different rules.py files.
* Automatically prioritizes rules that are part of deep dependency chains, to prevent CPUs from going idle.
* Produces log output that tells you *exactly* what you care about most, rather than spamming you with useless information:
  * At an interactive shell, provides a real-time rolling build progress indicator that tells you how many targets are still left to be built and which ones are currently building.
  * If your code is warning-and-error-free, this real-time progress indicator is literally the only thing make.py will print (it's even erased when the build finishes).
  * For code with warnings and errors, these are captured from the child processes and presented in a way that makes them clearly stand out from the rolling progress indicator.
  * Supports regex-based filtering of build output: if a tool prints a boilerplate useless message like "Generating code" that cannot be suppressed via command line option, you can filter it by regex so it doesn't pollute your build log.
  * Automatically disables the real-time progress indicator and falls back to a more traditional (but still minimalistic) log when stdout is redirected to a file.
* To ensure more reliable builds:
  * Attempts to exit as cleanly as possible when the user hits Ctrl-C.
  * Targets are automatically rebuilt when their rules' command lines change.
  * "Stale" targets from rules that no longer exist are automatically cleaned up (e.g. when you remove a .c file and its corresponding rules, all of its corresponding .o files will be deleted on the next build).
  * Automatically deletes targets of rules that failed, to avoid leaving possibly bogus build results laying around.
  * Automatically canonicalizes paths so multiple paths (absolute or relative) referring to the same file are handled correctly. This includes dealing with case insensitivity on Windows.
* Because a rule is just an arbitrary command line with a few extra properties, and because a rules.py file is an arbitrary Python script, only your imagination limits what sorts of builds and tests can be described.
* Supports both Windows and Unix-based systems (Linux, MacOSX, etc.).
* Supports Make-like .d files for correct header file dependencies when compiling C/C++.
* Built-in support for parsing the /showIncludes output of the Microsoft Visual Studio compiler to automatically generate .d files.
* Supports order-only dependencies (essential for auto-generated header files).
* Supports multi-target rules (a single command that generates multiple output files simultaneously).
* Takes care of a minor annoyance: automatically creates the directories that output files will live in, if they don't already exist.
* The entire tool is a single source file, make.py, that is about 450 lines of code.

Planned features that are not supported yet:
* Using SHA1 hashes instead of timestamps to determine when rebuilds are necessary.

make.py requires Python 3.1 or newer.
