# OS 02340123 tests

A shareable Linux test suite for the Technion Operating Systems course. The first
suite covers the mandatory HW1 `smash` commands, redirection, pipelines, job
control, and Ctrl+C. Future homework suites can live alongside `tests/hw1`.

The entire runner is **one Python script**, `test_suite.py`, with no installation
step or additional Python packages. Clone this repository inside the course folder.

Tests compare your solution with saved output generated from the maintainer's
chosen build. This is a regression suite, not an official staff solution: a bug in
that build can become a reference, so review reference changes before sharing.
Some tests also independently check process IDs, user identity, fixture sizes,
and allocated disk usage.

There are **110 default HW1 cases**, plus two optional cases for hard links
and EOF. The 50 cases in `combined` range from short command sequences to a
100-job stress test and checks at the 20-token and 200-character limits. The
10 cases in `end_to_end` each exercise every mandatory built-in, both pipe
operators, overwrite and append redirection, wildcard expansion, and Ctrl+C in
one shell session. The maintainer generates and commits references from the
chosen Linux build. No solution source or binary needs to be shared.

## Requirements

- Linux; use the course VM for results closest to the grading environment.
- Python 3.6 or newer. Only the standard library is used; no `pip install` step.
- `make`, `g++`, Bash, and ordinary Linux utilities (`cat`, `grep`, `du`, `seq`,
  `wc`, `echo`, `printenv`, `sleep`, and `ls`). These are normally in the VM.
- Git to clone, update, and contribute.

Windows/macOS can list cases and run portable runner self-tests. Executing smash
or generating references requires Linux. Do not use Windows Python to launch a
Linux binary. The runner never changes the solution's Makefile.

## Repository layout

Keep this as a separate repository beside `HW1`:

```text
OS_02340123/
├── .git/                         Your course repository
├── HW1/
│   └── skeleton_smash/
└── OS_02340123_tests/
    ├── .git/                     Independent test repository
    ├── test_suite.py              One standalone Python runner
    ├── README.md
    └── tests/
        ├── hw1/
        └── optional_hw1/
```

If the course directory is itself a Git repository, add this line to **its**
`.gitignore` so it does not accidentally track an embedded repository:

```gitignore
/OS_02340123_tests/
```

This is an ordinary separate Git repository, not a Git submodule. It can also be
cloned anywhere else; pass the solution path explicitly in that case.

## Quick start for students

From the main course directory on Linux:

```bash
git clone https://github.com/evyatar-reif/OS_02340123_tests.git
cd OS_02340123_tests

python3 test_suite.py list
python3 test_suite.py run --solution ../HW1/skeleton_smash
```

The default `--solution` already points to that sibling path. Paths containing
spaces should be quoted. If tests report `MISSING reference`, fetch the committed
references from the maintainer with `git pull`; the maintainer's initial
generation procedure is below.

Every run copies the solution into a temporary build directory, excludes old
`*.o` files and the `smash` executable, and runs exactly:

```bash
make smash
```

This prevents stale-object linker errors without editing the Makefile, running
`make clean`, or overwriting the original build. Each case then gets a fresh
working directory and a new shell process. Relative paths in test input refer to
that isolated directory, not to your solution directory.

Useful selections:

```bash
# One case
python3 test_suite.py run --case hw1/aliases/basic

# An entire directory
python3 test_suite.py run --case hw1/redirection

# A quoted glob (the runner, not Bash, should expand it)
python3 test_suite.py run --case 'hw1/pipelines/*'

# Multiple selections
python3 test_suite.py run --case hw1/aliases/basic --case hw1/jobs/pipeline_with_background

# A directory group, or a group explicitly named in case.json
python3 test_suite.py run --group signals

# List the 50 combined command and stress cases
python3 test_suite.py list --group combined

# List the 10 end-to-end shell sessions
python3 test_suite.py list --group end_to_end

# A different tests root or future homework suite
python3 test_suite.py run --tests ./tests --suite hw1 --list
```

## Read failures and logs

Print a test's input and saved reference output without building or running
smash:

```bash
python3 test_suite.py show --case hw1/redirection/append
python3 test_suite.py show --case hw1/fg/bring_and_interrupt
```

Batch cases show the input, stdout, and stderr. Interactive cases show the
steps and terminal transcript. Both include any checked output files. A missing
or stale reference still lets you view the input and is reported with exit
status 1.

The runner reports `PASS`, `FAIL`, or, during generation, `WROTE`/`SKIP`. A failed
comparison includes a unified diff with expected and actual output. Timeouts,
unexpected exits/signals, oversized output, missing/stale references, and failed
interactive expectations are reported distinctly in the failure message.

Each failed case prints its log directory and a ready-to-copy `show --logs`
command. It displays the input, failure reason, and captured output, including
partial output from failed reference generation. For interactive cases it also
shows which steps the runner completed. Pass the run directory printed after
`Logs:` to `--logs`.

Each invocation creates an ignored directory under `results/` containing:

- `build.log` when compiling source;
- `summary.json` with per-case results;
- raw stdout/stderr or the interactive terminal transcript;
- normalized output used for comparison;
- checked output files and an interactive event log;
- `failure.txt` for each failed case.

Use `--results /some/path` to choose another log root and `--build-timeout 120`
to allow a slower build. Case timeouts are configured per case.

Exit statuses are `0` for all selected cases passing/generated, `1` for case
failures, `2` for setup/build/configuration failures, and `130` for interruption.
The runner cleans up its shell and child processes on failures, timeouts, and
Ctrl+C. Cleanup tracks the dedicated Linux session, including children that call
`setpgrp()` and therefore leave the parent's process group.

## Generate initial references from your own build

Only the maintainer needs the chosen master executable/source. To build a fresh
copy of your current solution and generate all default HW1 references:

```bash
python3 test_suite.py generate-reference \
  --reference ../HW1/skeleton_smash \
  --label 'Evyatar HW1 reference 2026-11-24'
```

Alternatively, snapshot an already compiled Linux executable:

```bash
python3 test_suite.py generate-reference \
  --reference-exe /absolute/path/to/master/smash \
  --label 'Reviewed master build'
```

Source generation also uses a fresh temporary build and `make smash`. Executable
generation takes a private copy of the binary first. Both modes use the same
fixtures, interactions, normalization, timeouts, and cleanup as student runs.

To generate references for the combined or end-to-end cases on the VM:

```bash
python3 test_suite.py generate-reference --reference ../HW1/skeleton_smash --group combined
python3 test_suite.py generate-reference --reference ../HW1/skeleton_smash --group end_to_end
```

The generator requires the reference to be selected explicitly. Running
`test_suite.py run` never records or updates expected results. Expected output is stored
inside each case's `expected/` directory; metadata records the executable's
SHA-256, a public label, the generation time, and the test fingerprint. Local
solution paths and the solution code are not committed in that metadata.

Initially, generate one case to check the setup:

```bash
python3 test_suite.py generate-reference --reference ../HW1/skeleton_smash \
  --case hw1/aliases/basic
python3 test_suite.py run --case hw1/aliases/basic
```

Then generate the rest, review, and share:

```bash
python3 test_suite.py generate-reference --reference ../HW1/skeleton_smash
git status --short
git add tests
git diff --cached
git commit -m "Add reviewed HW1 references"
git push origin main
```

A reference-generation failure does not become an expected timeout/crash or an
empty golden file. Fix the master, the interaction, or the case configuration and
rerun that case. Existing references are skipped unless `--update` is supplied.

## Change references intentionally

After changing an input file, configuration, or fixtures, the old reference is
marked **STALE**. Comparison refuses to use it. Re-record the selected case from
the intended master:

```bash
python3 test_suite.py generate-reference --reference ../HW1/skeleton_smash \
  --case hw1/aliases/basic --update --label 'Reviewed alias fix'

git diff -- tests/hw1/aliases/basic
python3 test_suite.py run --case hw1/aliases/basic
git add tests/hw1/aliases/basic
git commit -m "Update alias regression reference"
git push
```

`--update` applies to every selected case. Omit `--case` only when intentionally
regenerating the whole selected suite. Generation swaps each completed reference
directory into place, preserving an existing reference if writing the replacement
fails. Changes to the runner's normalization format may also require regeneration.

Do not simply update a reference to make an unexplained candidate failure pass.
First decide which behavior is intended. Generated outputs are reviewable text,
so a maintainer can correct a reference manually, but regeneration from a reviewed
master is preferable and preserves useful provenance.

## Add a simple test

Create a folder under the appropriate group. A batch test needs only `input.txt`:

```bash
mkdir -p tests/hw1/redirection/my_case
cat > tests/hw1/redirection/my_case/input.txt <<'EOF'
pwd > out.txt
cat out.txt
pwd
quit
EOF
```

End normal batch tests with `quit` or `quit kill`. Otherwise a shell that does not
handle input EOF may loop forever; that is tested only in the optional EOF case.

Add optional `case.json` to describe fixtures, checked files, or a timeout:

```json
{
  "description": "Redirect pwd and then restore stdout",
  "timeout": 5,
  "groups": ["regression"],
  "files": ["out.txt"]
}
```

Then record and test it:

```bash
python3 test_suite.py generate-reference --reference ../HW1/skeleton_smash \
  --case hw1/redirection/my_case
python3 test_suite.py run --case hw1/redirection/my_case
```

Discovery is automatic. No central list or runner code needs editing. To modify
an existing test, edit its input/configuration and regenerate it with `--update`.

An expected directory contains:

```text
expected/
├── result.json             Exit status, provenance, test fingerprint, file kinds
├── stdout.txt              Batch mode
├── stderr.txt              Batch mode
├── transcript.txt          Interactive mode, instead of separate streams
└── files/                  Checked output files, when configured
```

Output file existence is compared as well as content. To compare binary bytes,
use `"files": [{"path": "out.bin", "binary": true}]`. Text files use the same
normalization as console output.

## Fixtures and environment

Put static starting files under a case's `fixtures/` directory; they are copied
into the test working directory. Alternatively, describe them in `setup`:

```json
{
  "setup": [
    {"type": "directory", "path": "tree/sub"},
    {"type": "file", "path": "tree/sub/a.txt", "text": "hello\n"},
    {"type": "symlink", "path": "tree/link", "target": "sub/a.txt"}
  ],
  "env": {"OS_TEST_A": "alpha", "OS_TEST_B": "beta"}
}
```

Supported setup types are `directory`, `file`, `symlink`, `hardlink`, `fifo`, and
`socket`. Hard links use `target` relative to the working directory; symlink
targets are relative to the link's parent, following Linux rules. Fixture and
checked-file paths must stay inside the isolated directory. Absolute paths and
`..` traversal are rejected. Symlinks escaping that directory are also rejected.

The process inherits your environment plus the explicit overrides. Tests force
`LC_ALL=C` and `LANG=C` for predictable messages. Avoid recording a complete
environment dump or depending on unrelated machine-specific variables.

## Ctrl+C, signals, and interactive cases

Interactive cases use a real pseudo-terminal with input echo disabled. Sending
Ctrl+C writes the terminal interrupt character; this exercises terminal signal
delivery rather than merely terminating the child from the runner. PTYs merge
stdout and stderr, so use batch mode when testing which stream a message uses.

An example `case.json` (no input.txt is needed):

```json
{
  "description": "Interrupt a foreground sleep and keep the shell alive",
  "mode": "interactive",
  "timeout": 8,
  "steps": [
    {"expect": "smash> "},
    {"send": "sleep 30"},
    {"wait_child": "foreground", "command": "sleep", "foreground": true},
    {"ctrl_c": true},
    {"expect": "smash: process {{foreground}} was killed\n"},
    {"expect": "smash> "},
    {"wait_exit": "foreground"},
    {"send": "quit"}
  ]
}
```

Step actions:

| Action | Behavior |
| --- | --- |
| `{"send": "pwd"}` | Send a line; the runner appends the newline. |
| `{"expect": "pattern"}` | Match a regex after the previous expectation; default stream is `transcript`. |
| `{"wait_child": "job", "command": "sleep"}` | Observe a direct child through `/proc` and bind its PID to `job`. |
| `{"wait_child": "job", "command": "sleep", "foreground": true}` | Also wait until the shell is sleeping, avoiding an interrupt before it starts waiting for the child. Can re-observe an existing binding after `fg`. |
| `{"ctrl_c": true}` | Send the actual terminal Ctrl+C character. |
| `{"signal": {"name": "SIGINT", "target": "shell"}}` | Send a specific signal to the shell or a bound child name. |
| `{"wait_exit": "job"}` | Wait until that process has exited, including zombie state. |
| `{"delay": 0.1}` | Explicit small delay; prefer observable events whenever possible. |

`expect`, `wait_child`, and `wait_exit` can have their own positive `timeout`.
The overall case deadline still applies. Expectations are multiline regular
expressions: escape `[` as `\\[` in JSON, for example. Placeholder values are
regex-escaped automatically, so paths containing dots are matched literally.

An expectation can capture a printed PID using named groups:

```json
{
  "expect": "pid (?P<pid>[0-9]+)",
  "capture": {"job": "pid"}
}
```

Prefer `wait_child` for signals: it independently observes a real process rather
than trusting a printed PID. Signal targets are checked against the test's Linux
session before sending; arbitrary PIDs outside that session are rejected.

## PID and machine-specific output

Normal comparison is exact, including spaces, newlines, job IDs, signal numbers,
and stream choice. The runner does not strip all numbers or sort output lines.
Only these controlled substitutions are performed:

- The fresh working directory becomes `{{WORKDIR}}`, including paths under it.
- The known shell PID becomes `{{SHELL_PID}}` in recognized PID messages.
- Observed/captured child PIDs become named tokens such as `{{PID:foreground}}`.
- Other recognized child PIDs become `{{PID:1}}`, `{{PID:2}}`, etc. The same number
  always receives the same token across the case's streams and output files.
- Interactive PTY `\r\n` line endings become `\n`.

Default recognized PID messages cover `showpid`, successful `kill`, Ctrl+C, and
`quit kill` entries. To support a command-specific PID format, add a regex with a
named `pid` group, for example the `fg` output:

```json
{"pid_patterns": ["sleep 30 & (?P<pid>[0-9]+)"]}
```

`showpid` is checked against the actual launched shell PID. For a case that runs
`showpid` inside a pipeline child, set `"allow_child_showpid": true`. PID
normalization preserves identities; it is not proof that every very short-lived
reported child existed. Signal tests use independently observed child bindings
to make that check stronger. Avoid tests relying on the nondeterministic ordering
of concurrent output.

For `whoami`, `"identity": true` verifies the four-line identity against the
current Linux account (including `getgid()`) before substituting `{{IDENTITY}}`.
Wrong user details are not normalized away.

File and directory sizes can differ across filesystems. Explicit value rules
verify the number before replacing it with a portable token:

```json
{
  "values": [
    {
      "name": "DIRECTORY_BYTES",
      "kind": "size",
      "path": "folder",
      "pattern": "folder's type is \"directory\" and takes up (?P<value>[0-9]+) bytes"
    },
    {
      "name": "DISK_KB",
      "kind": "du",
      "path": "tree",
      "pattern": "Total disk usage: (?P<value>[0-9]+) KB"
    }
  ]
}
```

`size` uses fixture `lstat().st_size`; `du` uses the real Linux `du -sk` on the
fixture. `"path": "."` is allowed for a value referring to the whole working
directory. Values are measured before running the test; do not mutate their
target fixtures during that test. A missing pattern or incorrect number fails
the case/reference generation instead of hiding the discrepancy.

Normalizers do not rewrite arbitrary utility error messages. The course VM and
its utilities are the intended baseline; materially different Linux distributions
can format external-command output differently.

## Optional cases and assignment scope

The default `hw1` suite respects the published assumptions: no pipe chains, no
combined pipe/redirection commands, no general quoted-argument parsing, and no
signals/background execution for special commands. Alias definitions are the
exception requiring their specified single-quote format. Signal tests operate on
ordinary foreground/background commands. The timeout bonus is not included.

Two cases are separate from the default suite:

```bash
python3 test_suite.py generate-reference --reference ../HW1/skeleton_smash \
  --suite optional_hw1
python3 test_suite.py run --suite optional_hw1
```

- `optional_hw1/du_hardlinks`: checks real-du inode deduplication, pending course
  clarification.
- `optional_hw1/eof`: checks EOF termination as an extra robustness property.

Do not interpret optional-case failures as confirmed mandatory grading failures.
Keep the suite aligned with new staff clarifications rather than guessing behavior.

## Contribute tests to GitHub

If you have write access, make a branch in your clone. Otherwise fork
`evyatar-reif/OS_02340123_tests` on GitHub and clone your fork:

```bash
git clone https://github.com/YOUR_USERNAME/OS_02340123_tests.git
cd OS_02340123_tests
git remote add upstream https://github.com/evyatar-reif/OS_02340123_tests.git
git checkout -b add-alias-test
```

Add the test, generate its reference from the agreed master if you have it, and
run the selected case. Test the runner itself if its code changed:

```bash
python3 test_suite.py self-test
git add tests/hw1/aliases/my_case
git diff --cached
git commit -m "Test alias behavior after removal"
git push -u origin add-alias-test
```

Open a pull request against `evyatar-reif/OS_02340123_tests:main`. Describe the
behavior, the relevant assignment rule, and which tests you ran. Include input,
configuration/fixtures, and reviewed expected output together. If you do not have
the agreed master, submit the case and ask the maintainer to generate the
reference; do not invent outputs or record an unexplained failing candidate.

The GitHub Actions workflow tests the framework on Linux using a small mock
program. That program is **not an HW1 solution or master**. CI does not need or
publish students' solution source, and it does not establish the correctness of
the maintainer's golden outputs. Do not commit `results/`, compiled executables,
object files, or solution source to this test repository.

## Add a future homework suite

Create `tests/hw2/...` and select it with `--suite hw2`. Discovery, text reference
comparison, fixtures, and metadata can be reused. The current execution adapter
is specifically for `smash` and `make smash`; another homework with a different
binary or protocol will need an adapter rather than pretending it is a shell.

## Configuration reference

| Setting | Default / meaning |
| --- | --- |
| `description` | Human-readable case description. |
| `mode` | `batch`; use `interactive` for PTY steps. |
| `timeout` | 10 seconds for the whole execution. |
| `exit_code` | 0; nonmatching exits/signals fail before recording. |
| `groups` | Additional group names for `--group`. |
| `env` | String environment overrides. |
| `setup` | Declarative fixture actions. |
| `steps` | Interactive actions in order. |
| `files` | Relative output files to compare. |
| `pid_patterns` | Additional regexes with a named `pid` group. |
| `identity` | False; validate and normalize `whoami` identity. |
| `values` | Checked machine-dependent numeric values. |
| `allow_child_showpid` | False; allow pipeline-child PIDs in `showpid`. |
| `allow_leftovers` | False; explicitly allow live children when smash exits. They are still cleaned up. |
| `max_output_bytes` | 2 MiB total console output and a 2 MiB limit per checked file. |

Unknown top-level settings are rejected to catch spelling mistakes. Unknown
placeholders fail clearly. Stored references are text plus JSON metadata; binary
checked files retain their exact bytes.
