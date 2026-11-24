#!/usr/bin/env python3
"""Standalone Linux smash suite: run, generate-reference, list, self-test.
Python 3.6+, standard library only. See README.md for test authoring.
"""


from pathlib import Path
from unittest import mock
import argparse
import contextlib
import datetime
import difflib
import errno
import fnmatch
import hashlib
import io
import json
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
# CASES



class SuiteError(Exception):
    """An invalid case, failed interaction, or execution error."""


def positive_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SuiteError("{} must be a positive number".format(name))
    if not math.isfinite(value) or value <= 0:
        raise SuiteError("{} must be a positive finite number".format(name))
    return value


def inside(root, relative):
    """Reject absolute paths, traversal, and symlinks escaping the fixture root."""
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts or str(relative) in ("", "."):
        raise SuiteError("Expected a relative file path: {!s}".format(relative))
    target = root / relative
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        raise SuiteError("Path escapes the test directory: {!s}".format(relative))
    return target


class Case:
    def __init__(self, root, path):
        self.path = path
        self.id = path.relative_to(root).as_posix()
        try:
            self.config = json.loads((path / "case.json").read_text(encoding="utf-8")) \
                if (path / "case.json").is_file() else {}
        except (ValueError, OSError) as error:
            raise SuiteError("{}: {}".format(self.id, error))
        if not isinstance(self.config, dict):
            raise SuiteError("{}: case.json must be an object".format(self.id))
        allowed = {"description", "mode", "timeout", "groups", "env", "setup", "steps",
                   "files", "pid_patterns", "identity", "values", "allow_leftovers",
                   "allow_child_showpid", "max_output_bytes", "exit_code"}
        unknown = set(self.config) - allowed
        if unknown:
            raise SuiteError("{}: unknown settings {}".format(self.id, sorted(unknown)))
        self.mode = self.config.get("mode", "batch")
        if self.mode not in ("batch", "interactive"):
            raise SuiteError("{}: mode must be batch or interactive".format(self.id))
        self.timeout = positive_number(self.config.get("timeout", 10), "timeout")
        self.max_output = self.config.get("max_output_bytes", 2 * 1024 * 1024)
        if isinstance(self.max_output, bool) or not isinstance(self.max_output, int) or self.max_output <= 0:
            raise SuiteError("max_output_bytes must be a positive integer")
        if self.mode == "batch" and not (path / "input.txt").is_file():
            raise SuiteError("{}: a batch case needs input.txt".format(self.id))
        if self.mode == "interactive" and not self.config.get("steps"):
            raise SuiteError("{}: an interactive case needs steps".format(self.id))
        if not isinstance(self.config.get("env", {}), dict):
            raise SuiteError("env must be an object")
        for key, value in self.config.get("env", {}).items():
            if not isinstance(value, str) or "\0" in value or "=" in key or "\0" in key:
                raise SuiteError("env entries must be valid string names and values")
        for field in ("groups", "setup", "steps", "files", "pid_patterns", "values"):
            if not isinstance(self.config.get(field, []), list):
                raise SuiteError("{} must be an array".format(field))
        for pattern in self.config.get("pid_patterns", []):
            try:
                compiled = re.compile(pattern)
            except (re.error, TypeError) as error:
                raise SuiteError("Invalid PID pattern: {}".format(error))
            if "pid" not in compiled.groupindex:
                raise SuiteError("PID patterns need a named group: (?P<pid>[0-9]+)")
        for entry in self.config.get("files", []):
            name = entry if isinstance(entry, str) else entry["path"]
            inside(path, name)

    def fingerprint(self):
        digest = hashlib.sha256()
        paths = []
        for name in ("input.txt", "case.json"):
            if (self.path / name).is_file():
                paths.append(self.path / name)
        fixture_root = self.path / "fixtures"
        if fixture_root.is_dir():
            paths.extend(sorted(fixture_root.rglob("*")))
        for path in paths:
            relative = path.relative_to(self.path).as_posix().encode("utf-8")
            digest.update(relative + b"\0")
            if path.is_symlink():
                digest.update(b"symlink\0" + os.readlink(str(path)).encode("utf-8"))
            elif path.is_file():
                digest.update(b"file\0" + path.read_bytes())
            else:
                digest.update(b"directory")
            digest.update(b"\0")
        return digest.hexdigest()


def discover(root, cases=None, group=None, suite=None):
    root = Path(root).resolve()
    found = set()
    if not root.is_dir():
        raise SuiteError("Tests directory does not exist: {}".format(root))
    for filename in ("input.txt", "case.json"):
        for path in root.rglob(filename):
            relative = path.relative_to(root)
            if any(part.startswith(".") or part in ("expected", "fixtures") for part in relative.parts):
                continue
            found.add(path.parent)
    selected = []
    for path in sorted(found):
        case = Case(root, path)
        if suite and not (case.id == suite or case.id.startswith(suite.rstrip("/") + "/")):
            continue
        if cases and not any(fnmatch.fnmatchcase(case.id, pattern) or case.id == pattern or
                             case.id.startswith(pattern.rstrip("/") + "/") for pattern in cases):
            continue
        if group and group not in case.config.get("groups", []) and group not in Path(case.id).parts:
            continue
        selected.append(case)
    if not selected:
        raise SuiteError("No matching tests. Use --list to see available cases.")
    return selected


def prepare(case, work):
    fixtures = case.path / "fixtures"
    if fixtures.is_dir():
        # Copy symbolic links themselves, then validate their destinations.
        for source in sorted(fixtures.rglob("*")):
            name = source.relative_to(fixtures)
            target = inside(work, name)
            if source.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(os.readlink(str(source)), str(target))
                inside(work, name)
            elif source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(source), str(target))
    for action in case.config.get("setup", []):
        kind = action.get("type")
        path = inside(work, action["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "directory":
            path.mkdir(exist_ok=True)
        elif kind == "file":
            path.write_text(action.get("text", ""), encoding="utf-8")
        elif kind == "symlink":
            os.symlink(action["target"], str(path))
            inside(work, action["path"])
        elif kind == "hardlink":
            os.link(str(inside(work, action["target"])), str(path))
        elif kind == "fifo":
            os.mkfifo(str(path))
        elif kind == "socket":
            import socket
            handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                handle.bind(str(path))
            finally:
                handle.close()
        else:
            raise SuiteError("Unknown fixture action: {!r}".format(kind))


TOKEN = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


def expand(text, bindings, regex=False):
    def replace(match):
        name = match.group(1)
        if name not in bindings:
            raise SuiteError("Unknown placeholder: {}".format(name))
        value = str(bindings[name])
        return re.escape(value) if regex else value
    return TOKEN.sub(replace, text)


# SESSION



if os.name == "posix":
    import fcntl
    import pty
    import termios


def process_info(pid):
    try:
        text = Path("/proc/{}/stat".format(pid)).read_text()
        fields = text[text.rfind(")") + 2:].split()
        return {"pid": int(pid), "state": fields[0], "parent": int(fields[1]),
                "group": int(fields[2]), "session": int(fields[3]),
                "start": int(fields[19]),
                "command": text[text.find("(") + 1:text.rfind(")")]}
    except (OSError, ValueError, IndexError):
        return None


def session_processes(session_id):
    members = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            info = process_info(int(entry.name))
            if info and info["session"] == session_id and info["state"] != "Z":
                members.append(info)
    return members


def cleanup_session(session_id):
    """setpgrp() changes groups, not sessions: clean every group in this session."""
    if not sys.platform.startswith("linux"):
        return
    for unused in range(4):
        members = session_processes(session_id)
        if not members:
            return
        # Stop descendants before the root, reducing opportunities to spawn more.
        members.sort(key=lambda item: item["pid"] == session_id)
        for previous in members:
            current = process_info(previous["pid"])
            if not current or current["start"] != previous["start"] or current["session"] != session_id:
                continue
            try:
                os.kill(previous["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.025)


class Session:
    def __init__(self, binary, case, work, env):
        if not sys.platform.startswith("linux"):
            raise SuiteError("Executing smash tests requires Linux. Discovery and unit tests work elsewhere.")
        self.case = case
        self.deadline = time.monotonic() + case.timeout
        self.selector = selectors.DefaultSelector()
        self.buffers = {"stdout": bytearray(), "stderr": bytearray(), "transcript": bytearray()}
        self.cursors = {key: 0 for key in self.buffers}
        self.events = []
        self.returncode = None
        self.process = None
        self.input_fd = None
        self.master_fd = None
        self.input_closed = False
        self.pid = None
        try:
            self._start(binary, case, work, env)
        except BaseException:
            if self.pid is not None:
                self.close()
            else:
                self.selector.close()
            raise

    def _start(self, binary, case, work, env):
        if case.mode == "interactive":
            pid, master = pty.fork()
            if pid == 0:
                try:
                    os.chdir(str(work))
                    os.execve(str(binary), [str(binary)], env)
                except OSError as error:
                    os.write(2, ("test runner: exec failed: {}\n".format(error)).encode())
                    os._exit(127)
            self.pid = pid
            self.master_fd = master
            self.input_fd = master
            attrs = termios.tcgetattr(master)
            attrs[3] &= ~(termios.ECHO | termios.ECHONL)
            attrs[3] |= termios.ISIG
            attrs[6][termios.VINTR] = b"\x03"
            termios.tcsetattr(master, termios.TCSANOW, attrs)
            self._register(master, "transcript")
        else:
            self.process = subprocess.Popen([str(binary)], cwd=str(work), env=env,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, bufsize=0,
                                            start_new_session=True)
            self.pid = self.process.pid
            self.input_fd = self.process.stdin.fileno()
            self._register(self.process.stdout.fileno(), "stdout")
            self._register(self.process.stderr.fileno(), "stderr")
            self._nonblocking(self.input_fd)
        self.bindings = {"SHELL_PID": self.pid, "WORKDIR": str(work)}

    @staticmethod
    def _nonblocking(fd):
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)

    def _register(self, fd, name):
        self._nonblocking(fd)
        self.selector.register(fd, selectors.EVENT_READ, name)

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self.process is not None:
            self.returncode = self.process.poll()
        else:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.returncode = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status)
        return self.returncode

    def remaining(self, local_deadline=None):
        deadline = min(self.deadline, local_deadline) if local_deadline is not None else self.deadline
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SuiteError("Timed out after {:.2f}s".format(self.case.timeout))
        return remaining

    def pump(self, timeout=0.05):
        if not self.selector.get_map():
            time.sleep(min(timeout, 0.01))
            return
        for key, unused in self.selector.select(timeout):
            try:
                chunk = os.read(key.fd, 65536)
            except BlockingIOError:
                continue
            except OSError as error:
                if error.errno != errno.EIO or self.case.mode != "interactive":
                    raise
                chunk = b""
            if not chunk:
                self.selector.unregister(key.fd)
                continue
            self.buffers[key.data].extend(chunk)
            if sum(len(value) for value in self.buffers.values()) > self.case.max_output:
                raise SuiteError("Output exceeded max_output_bytes ({})".format(self.case.max_output))

    def text(self, stream):
        if stream not in self.buffers:
            raise SuiteError("Unknown stream: {}".format(stream))
        text = bytes(self.buffers[stream]).decode("utf-8", "surrogateescape")
        return text.replace("\r\n", "\n") if stream == "transcript" else text

    def send_bytes(self, data):
        position = 0
        while position < len(data):
            self.remaining()
            try:
                position += os.write(self.input_fd, data[position:])
            except BlockingIOError:
                self.pump(0.01)
            except OSError as error:
                raise SuiteError("Input closed before all commands were sent: {}".format(error))

    def send(self, text):
        text = expand(text, self.bindings)
        self.events.append({"send": text})
        self.send_bytes((text + "\n").encode("utf-8"))

    def expect(self, step):
        stream = step.get("stream", "transcript")
        if stream != "transcript":
            raise SuiteError("Interactive PTY steps use the merged transcript stream")
        pattern = re.compile(expand(step["expect"], self.bindings, regex=True), re.MULTILINE)
        local = time.monotonic() + positive_number(step.get("timeout", self.case.timeout), "step timeout")
        while True:
            match = pattern.search(self.text(stream), self.cursors[stream])
            if match:
                self.cursors[stream] = match.end()
                for name, group in step.get("capture", {}).items():
                    value = match.group(group)
                    if not value.isdigit() or int(value) <= 0:
                        raise SuiteError("PID capture {} must be a positive integer".format(name))
                    self._bind(name, int(value))
                self.events.append({"expect": step["expect"]})
                return
            if self.poll() is not None:
                self.pump(0)
                if not pattern.search(self.text(stream), self.cursors[stream]):
                    raise SuiteError("Shell exited before matching {!r}".format(step["expect"]))
                continue
            try:
                self.pump(min(0.05, self.remaining(local)))
            except SuiteError as error:
                raise SuiteError("Expected {!r}: {}".format(step["expect"], error))

    def _bind(self, name, pid):
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name) or name in ("SHELL_PID", "WORKDIR"):
            raise SuiteError("Invalid or reserved PID binding: {}".format(name))
        if name in self.bindings and self.bindings[name] != pid:
            raise SuiteError("PID binding {} changed within one test".format(name))
        self.bindings[name] = pid

    def wait_child(self, step):
        name = step["wait_child"]
        command = step.get("command")
        local = time.monotonic() + positive_number(step.get("timeout", 3), "step timeout")
        while True:
            used = {value for value in self.bindings.values() if isinstance(value, int)}
            parent = process_info(self.pid)
            if step.get("foreground") and (not parent or parent["state"] != "S"):
                self.pump(min(0.025, self.remaining(local)))
                continue
            members = sorted(session_processes(self.pid), key=lambda item: item["pid"])
            for member in members:
                if (member["pid"] in used and member["pid"] != self.bindings.get(name)) or member["parent"] != self.pid:
                    continue
                if command and member["command"] != command:
                    continue
                self._bind(name, member["pid"])
                self.events.append({"wait_child": name, "pid": member["pid"]})
                return
            if self.poll() is not None:
                raise SuiteError("Shell exited while waiting for child {}".format(name))
            self.pump(min(0.025, self.remaining(local)))

    def target_pid(self, target):
        pid = self.pid if target == "shell" else self.bindings.get(target)
        if not isinstance(pid, int) or pid <= 0:
            raise SuiteError("Unknown signal target: {}".format(target))
        info = process_info(pid)
        if not info or info["session"] != self.pid:
            raise SuiteError("Signal target is not a live member of this test session: {}".format(target))
        return pid

    def steps(self):
        for step in self.case.config["steps"]:
            actions = set(step) & {"send", "expect", "wait_child", "ctrl_c", "signal", "wait_exit", "delay"}
            if len(actions) != 1:
                raise SuiteError("Each step needs exactly one action: {}".format(step))
            action = next(iter(actions))
            self.remaining()
            if action == "send":
                self.send(step["send"])
            elif action == "expect":
                self.expect(step)
            elif action == "wait_child":
                self.wait_child(step)
            elif action == "ctrl_c":
                self.events.append({"ctrl_c": True})
                self.send_bytes(b"\x03")
            elif action == "signal":
                target = step["signal"].get("target", "shell")
                name = step["signal"]["name"]
                number = getattr(signal, name, None)
                if not isinstance(number, int) or not name.startswith("SIG"):
                    raise SuiteError("Invalid signal name: {}".format(name))
                os.kill(self.target_pid(target), number)
                self.events.append({"signal": name, "target": target})
            elif action == "wait_exit":
                target = step["wait_exit"]
                pid = self.pid if target == "shell" else self.bindings.get(target)
                if not isinstance(pid, int):
                    raise SuiteError("Unknown wait_exit target: {}".format(target))
                local = time.monotonic() + positive_number(step.get("timeout", 3), "step timeout")
                while True:
                    info = process_info(pid)
                    if not info or info["state"] == "Z":
                        break
                    self.pump(min(0.025, self.remaining(local)))
                self.events.append({"wait_exit": target})
            else:
                local = time.monotonic() + positive_number(step["delay"], "delay")
                while time.monotonic() < local:
                    self.pump(min(0.025, self.remaining(), local - time.monotonic()))

    def finish(self):
        while self.poll() is None:
            self.pump(min(0.05, self.remaining()))
        # Allow promptly terminating children to finish before checking leftovers.
        grace = min(self.deadline, time.monotonic() + 0.15)
        while time.monotonic() < grace:
            self.pump(0.01)
            if not session_processes(self.pid) and not self.selector.get_map():
                break
        leftovers = [entry for entry in session_processes(self.pid) if entry["pid"] != self.pid]
        if leftovers and not self.case.config.get("allow_leftovers", False):
            raise SuiteError("Shell left {} live child process(es) after exiting".format(len(leftovers)))
        return self.returncode

    def close(self):
        cleanup_session(self.pid)
        if self.poll() is None:
            if self.process is not None:
                self.returncode = self.process.wait(timeout=2)
            else:
                unused, status = os.waitpid(self.pid, 0)
                self.returncode = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status)
        self.selector.close()
        if self.master_fd is not None:
            os.close(self.master_fd)
        if self.process is not None:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


# REFERENCE




SCHEMA = 1
PID_PATTERNS = [
    r"signal number [0-9]+ was sent to pid (?P<pid>[0-9]+)",
    r"smash: process (?P<pid>[0-9]+) was killed",
    r"(?m)^(?P<pid>[0-9]+): (?=.)",
]


class Normalizer:
    def __init__(self, case, work, bindings):
        self.case = case
        self.work = work
        self.bindings = bindings
        self.names = {str(bindings["SHELL_PID"]): "{{SHELL_PID}}"}
        for name, value in sorted(bindings.items()):
            if isinstance(value, int) and name != "SHELL_PID":
                self.names.setdefault(str(value), "{{PID:" + name + "}}")
        self.next_pid = 1
        self.identity_count = 0
        self.values = []
        for value in case.config.get("values", []):
            name = value["name"]
            if not re.match(r"^[A-Z][A-Z0-9_]*$", name):
                raise SuiteError("Value names must use uppercase letters, digits, and underscores")
            path = work if value["path"] == "." else inside(work, value["path"])
            if value["kind"] == "size":
                expected = str(os.lstat(str(path)).st_size)
            elif value["kind"] == "du":
                try:
                    output = subprocess.check_output(["du", "-sk", "--", str(path)],
                                                     stderr=subprocess.STDOUT, timeout=3,
                                                     env=dict(os.environ, LC_ALL="C"))
                    expected = output.decode("ascii").split()[0]
                except (OSError, subprocess.SubprocessError, IndexError) as error:
                    raise SuiteError("Cannot obtain fixture disk usage: {}".format(error))
            else:
                raise SuiteError("Unknown value kind: {}".format(value["kind"]))
            pattern = re.compile(value["pattern"], re.MULTILINE)
            if "value" not in pattern.groupindex:
                raise SuiteError("Value patterns need (?P<value>...)")
            self.values.append({"name": name, "pattern": pattern, "expected": expected, "count": 0})
        self.identity = None
        if case.config.get("identity"):
            import pwd
            account = pwd.getpwuid(os.getuid())
            self.identity = "{}\n{}\n{}\n{}\n".format(account.pw_name, os.getuid(), os.getgid(), account.pw_dir)

    def pid(self, value):
        if not value.isdigit() or int(value) <= 0:
            raise SuiteError("Reported PID is not positive: {}".format(value))
        if value not in self.names:
            self.names[value] = "{{PID:" + str(self.next_pid) + "}}"
            self.next_pid += 1
        return self.names[value]

    def text(self, text):
        text = text.replace(str(self.work), "{{WORKDIR}}")
        if self.identity:
            self.identity_count += text.count(self.identity)
            text = text.replace(self.identity, "{{IDENTITY}}\n")

        def showpid(match):
            value = match.group("pid")
            if int(value) != self.bindings["SHELL_PID"] and not self.case.config.get("allow_child_showpid"):
                raise SuiteError("showpid reported {}, but smash PID is {}".format(value, self.bindings["SHELL_PID"]))
            return "smash pid is " + self.pid(value)
        text = re.sub(r"smash pid is (?P<pid>[0-9]+)", showpid, text)

        def replace_pid(match):
            start = match.start("pid") - match.start()
            end = match.end("pid") - match.start()
            return match.group(0)[:start] + self.pid(match.group("pid")) + match.group(0)[end:]
        for pattern in PID_PATTERNS + self.case.config.get("pid_patterns", []):
            text = re.sub(pattern, replace_pid, text)
        for entry in self.values:
            def replace_value(match):
                actual = match.group("value")
                if actual != entry["expected"]:
                    raise SuiteError("{}: reported {!r}, fixture requires {!r}".format(entry["name"], actual, entry["expected"]))
                entry["count"] += 1
                start = match.start("value") - match.start()
                end = match.end("value") - match.start()
                return match.group(0)[:start] + "{{" + entry["name"] + "}}" + match.group(0)[end:]
            text = entry["pattern"].sub(replace_value, text)
        return text

    def validate(self):
        if self.identity and not self.identity_count:
            raise SuiteError("Expected whoami identity does not match this machine's user/UID/GID/home")
        for entry in self.values:
            if not entry["count"]:
                raise SuiteError("No output matched value pattern for {}".format(entry["name"]))


def file_results(case, work, normalizer):
    result = {}
    for entry in case.config.get("files", []):
        entry = {"path": entry} if isinstance(entry, str) else entry
        name = Path(entry["path"]).as_posix()
        path = inside(work, name)
        if not path.exists():
            result[name] = {"kind": "missing"}
        elif not path.is_file():
            raise SuiteError("Expected an ordinary output file: {}".format(name))
        else:
            if path.stat().st_size > case.max_output:
                raise SuiteError("Output file too large: {}".format(name))
            raw = path.read_bytes()
            binary = entry.get("binary", False)
            content = raw if binary else normalizer.text(raw.decode("utf-8", "surrogateescape"))
            result[name] = {"kind": "binary" if binary else "text", "content": content}
    return result


def normalized_result(case, session, work, normalizer):
    # Interactive bindings are populated after the initial fixture metrics.
    for name, value in sorted(session.bindings.items()):
        if isinstance(value, int) and name != "SHELL_PID":
            normalizer.names.setdefault(str(value), "{{PID:" + name + "}}")
    names = ("transcript",) if case.mode == "interactive" else ("stdout", "stderr")
    streams = {name: normalizer.text(session.text(name)) for name in names}
    files = file_results(case, work, normalizer)
    normalizer.validate()
    return {"exit_code": session.returncode, "streams": streams, "files": files}


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8", "surrogateescape"))


def save_reference(case, result, provenance):
    destination = case.path / "expected"
    if destination.is_symlink():
        raise SuiteError("Refusing to replace a symbolic-link expected directory")
    staging = Path(tempfile.mkdtemp(prefix=".expected-new-", dir=str(case.path)))
    backup = case.path / (".expected-old-" + uuid.uuid4().hex)
    try:
        metadata = {"schema": SCHEMA, "test_sha256": case.fingerprint(),
                    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "reference": provenance, "exit_code": result["exit_code"],
                    "streams": list(result["streams"]), "files": {}}
        for name, text in result["streams"].items():
            write_text(staging / (name + ".txt"), text)
        for name, entry in result["files"].items():
            metadata["files"][name] = {"kind": entry["kind"]}
            if entry["kind"] != "missing":
                target = inside(staging / "files", name)
                target.parent.mkdir(parents=True, exist_ok=True)
                if entry["kind"] == "binary":
                    target.write_bytes(entry["content"])
                else:
                    write_text(target, entry["content"])
        write_text(staging / "result.json", json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        if destination.exists():
            destination.rename(backup)
        try:
            staging.rename(destination)
        except OSError:
            if backup.exists():
                backup.rename(destination)
            raise
        if backup.exists():
            shutil.rmtree(str(backup))
    finally:
        if staging.exists():
            shutil.rmtree(str(staging))


def load_reference(case):
    root = case.path / "expected"
    try:
        meta = json.loads((root / "result.json").read_text(encoding="utf-8"))
        if meta["schema"] != SCHEMA:
            raise SuiteError("Unsupported reference schema; regenerate the reference")
        if meta["test_sha256"] != case.fingerprint():
            raise SuiteError("STALE reference: test input, configuration, or fixtures changed; regenerate with --update")
        streams = {}
        for name in meta["streams"]:
            if name not in ("stdout", "stderr", "transcript"):
                raise SuiteError("Invalid reference stream")
            streams[name] = (root / (name + ".txt")).read_bytes().decode("utf-8", "surrogateescape")
        files = {}
        for name, entry in meta["files"].items():
            entry = dict(entry)
            if entry["kind"] != "missing":
                raw = inside(root / "files", name).read_bytes()
                entry["content"] = raw if entry["kind"] == "binary" else raw.decode("utf-8", "surrogateescape")
            files[name] = entry
        return {"exit_code": meta["exit_code"], "streams": streams, "files": files}
    except FileNotFoundError:
        raise SuiteError("MISSING reference: run test_suite.py generate-reference for this case")
    except (ValueError, KeyError, TypeError) as error:
        raise SuiteError("Invalid reference metadata: {}".format(error))


def differences(expected, actual):
    messages = []
    if expected["exit_code"] != actual["exit_code"]:
        messages.append("Exit code: expected {}, got {}".format(expected["exit_code"], actual["exit_code"]))
    for name in sorted(set(expected["streams"]) | set(actual["streams"])):
        before = expected["streams"].get(name, "")
        after = actual["streams"].get(name, "")
        if before != after:
            messages.append("".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                      fromfile="expected/" + name, tofile="actual/" + name)))
    for name in sorted(set(expected["files"]) | set(actual["files"])):
        before = expected["files"].get(name)
        after = actual["files"].get(name)
        if before == after:
            continue
        if before and after and before["kind"] == after["kind"] == "text":
            messages.append("".join(difflib.unified_diff(before["content"].splitlines(True), after["content"].splitlines(True),
                                                      fromfile="expected/files/" + name, tofile="actual/files/" + name)))
        else:
            messages.append("Output file differs (existence, kind, or binary content): {}".format(name))
    return messages


# CLI




ROOT = Path(__file__).resolve().parent


def build_source(source, temporary, log, timeout):
    source = Path(source).resolve()
    if not (source / "Makefile").is_file():
        raise SuiteError("No Makefile in solution directory: {}".format(source))
    target = temporary / "build"
    shutil.copytree(str(source), str(target), symlinks=True,
                    ignore=shutil.ignore_patterns(".git", ".idea", ".vscode", "__pycache__", "*.o", "*.a",
                                                  "*.so", "smash", "build", "cmake-build-*", "results", ".test-suite-work"))
    process = subprocess.Popen(["make", "smash"], cwd=str(target), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True,
                               env=dict(os.environ, LC_ALL="C", LANG="C"))
    try:
        output, unused = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        cleanup_session(process.pid)
        output, unused = process.communicate(timeout=2)
        log.write_bytes(output)
        raise SuiteError("Build timed out; see {}".format(log))
    except BaseException:
        cleanup_session(process.pid)
        process.wait(timeout=2)
        raise
    log.write_bytes(output)
    cleanup_session(process.pid)
    if process.returncode:
        raise SuiteError("make smash failed (exit {}); see {}".format(process.returncode, log))
    binary = target / "smash"
    if not binary.is_file() or not os.access(str(binary), os.X_OK):
        raise SuiteError("make smash did not produce an executable named smash")
    return binary


def execute_case(case, binary, log_dir):
    log_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="smash-case-") as directory:
        work = Path(directory) / "work"
        work.mkdir()
        session = None
        try:
            prepare(case, work)
            env = dict(os.environ, LC_ALL="C", LANG="C", TERM="dumb")
            env.update(case.config.get("env", {}))
            # Locale remains deterministic, even if the calling shell uses Hebrew.
            env.update(LC_ALL="C", LANG="C")
            session = Session(binary, case, work, env)
            normalizer = Normalizer(case, work, session.bindings)
            if case.mode == "interactive":
                session.steps()
            else:
                commands = (case.path / "input.txt").read_text(encoding="utf-8")
                session.send_bytes(expand(commands, session.bindings).encode("utf-8"))
                session.process.stdin.close()
                session.input_closed = True
            session.finish()
            wanted = case.config.get("exit_code", 0)
            if session.returncode != wanted:
                raise SuiteError("Shell exit code: expected {}, got {}".format(wanted, session.returncode))
            result = normalized_result(case, session, work, normalizer)
            for name, text in result["streams"].items():
                write_text(log_dir / ("normalized." + name + ".txt"), text)
            for name, entry in result["files"].items():
                if entry["kind"] != "missing":
                    path = log_dir / "files" / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if entry["kind"] == "binary":
                        path.write_bytes(entry["content"])
                    else:
                        write_text(path, entry["content"])
            return result
        finally:
            if session is not None:
                try:
                    session.close()
                finally:
                    for name in ("stdout", "stderr", "transcript"):
                        write_text(log_dir / ("raw." + name + ".txt"), session.text(name))
                    write_text(log_dir / "events.json", json.dumps(session.events, indent=2) + "\n")


def parser(generate):
    result = argparse.ArgumentParser(description=("Generate references from your chosen master build."
                                                 if generate else "Compile with make smash and run smash tests."))
    result.add_argument("--tests", type=Path, default=ROOT / "tests", help="Root of the test cases")
    result.add_argument("--suite", default="hw1", help="Suite directory (default: hw1)")
    result.add_argument("--case", action="append", help="Case ID, directory prefix, or quoted glob; repeatable")
    result.add_argument("--group", help="Select a directory group or a group named in case.json")
    result.add_argument("--list", action="store_true", help="List selected cases and reference status without running")
    result.add_argument("--results", type=Path, default=ROOT / "results", help="Root directory for local logs")
    result.add_argument("--build-timeout", type=float, default=60)
    if generate:
        source = result.add_mutually_exclusive_group()
        source.add_argument("--reference", "--reference-source", dest="reference_source", type=Path, help="Master source directory to compile with make smash")
        source.add_argument("--reference-exe", type=Path, help="An existing master Linux executable")
        result.add_argument("--update", action="store_true", help="Explicitly replace selected existing references")
        result.add_argument("--label", default="maintainer build", help="Public label recorded with the binary hash")
    else:
        result.add_argument("--solution", type=Path, default=ROOT.parent / "HW1" / "skeleton_smash",
                            help="Source directory containing the solution Makefile")
    return result


def run_command(argv=None, generate=False):
    options = parser(generate)
    args = options.parse_args(argv)
    try:
        positive_number(args.build_timeout, "build timeout")
        cases = discover(args.tests, args.case, args.group, args.suite)
        if args.list:
            for case in cases:
                try:
                    load_reference(case)
                    state = "ready"
                except SuiteError as error:
                    state = "stale" if str(error).startswith("STALE") else "missing/invalid"
                print("{:<52} {:<16} {}".format(case.id, state, case.config.get("description", "")))
            print("{} case(s)".format(len(cases)))
            return 0
        if generate:
            if not args.reference_source and not args.reference_exe:
                options.error("supply --reference-source or --reference-exe explicitly")
            pending = []
            for case in cases:
                if (case.path / "expected").exists() and not args.update:
                    print("SKIP {} (reference exists; use --update)".format(case.id))
                else:
                    pending.append(case)
            cases = pending
            if not cases:
                return 0
        if not sys.platform.startswith("linux"):
            raise SuiteError("Run execution/reference generation on Linux (the course VM). --list works on any platform.")
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        logs = args.results.resolve() / (stamp + ("-reference" if generate else "-tests"))
        logs.mkdir(parents=True)
        with tempfile.TemporaryDirectory(prefix="smash-build-") as directory:
            temporary = Path(directory)
            if generate and args.reference_exe:
                source = args.reference_exe.resolve()
                if not source.is_file() or not os.access(str(source), os.X_OK):
                    raise SuiteError("Reference executable does not exist or is not executable: {}".format(source))
                binary = temporary / "smash"
                shutil.copy2(str(source), str(binary))
            else:
                source = args.reference_source if generate else args.solution
                print("Building a fresh source copy with: make smash")
                binary = build_source(source, temporary, logs / "build.log", args.build_timeout)
            provenance = {"label": args.label if generate else "candidate",
                          "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
            failed = 0
            summary = []
            for case in cases:
                case_logs = logs / case.id
                case_logs.mkdir(parents=True, exist_ok=True)
                try:
                    expected = None if generate else load_reference(case)
                    actual = execute_case(case, binary, case_logs)
                    if generate:
                        save_reference(case, actual, provenance)
                        print("WROTE " + case.id)
                    else:
                        messages = differences(expected, actual)
                        if messages:
                            raise SuiteError("\n".join(messages))
                        print("PASS  " + case.id)
                    summary.append({"case": case.id, "status": "generated" if generate else "passed"})
                except (SuiteError, OSError, ValueError, KeyError, TypeError, re.error) as error:
                    failed += 1
                    message = str(error)
                    print("FAIL  {}\n{}".format(case.id, message))
                    write_text(case_logs / "failure.txt", message + "\n")
                    summary.append({"case": case.id, "status": "failed", "message": message})
            write_text(logs / "summary.json", json.dumps(summary, indent=2) + "\n")
            print("\n{} passed/generated, {} failed. Logs: {}".format(len(cases) - failed, failed, logs))
            return 1 if failed else 0
    except KeyboardInterrupt:
        print("\nInterrupted; cleaning up the active test session.", file=sys.stderr)
        return 130
    except (SuiteError, OSError, ValueError) as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        return 2


# RUNNER SELF-TESTS (the mock below is not an HW1 solution)




FAKE_PROGRAM = r'''#!/usr/bin/env python3
"""Small protocol fixture for runner self-tests, NOT a solution/reference for HW1."""
import os
import signal
import sys
import time

foreground = None
jobs = []


def interrupt(number, frame):
    print("smash: got ctrl-C", flush=True)
    if foreground:
        os.kill(foreground, signal.SIGKILL)
        print("smash: process {} was killed".format(foreground), flush=True)


signal.signal(signal.SIGINT, interrupt)
while True:
    sys.stdout.write("smash> ")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        break
    words = line.strip().split()
    if not words:
        continue
    if words[0] == "quit":
        if len(words) > 1 and words[1] == "kill":
            for pid in jobs:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
        break
    elif words[0] == "showpid":
        print("smash pid is {}".format(os.getpid()), flush=True)
    elif words[0] == "badpid":
        print("smash pid is {}".format(os.getpid() + 1000000), flush=True)
    elif words[0] == "pwd":
        print(os.getcwd(), flush=True)
    elif words[0] == "error":
        print("smash error: fixture error", file=sys.stderr, flush=True)
    elif words[0] == "write":
        with open(words[1], "w") as handle:
            handle.write(os.getcwd() + "\n")
    elif words[0] == "sleep":
        pid = os.fork()
        if pid == 0:
            os.setpgrp()
            os.execl("/bin/sleep", "sleep", words[1])
        if words[-1] == "&":
            jobs.append(pid)
        else:
            foreground = pid
            os.waitpid(pid, 0)
            foreground = None
    elif words[0] == "hang":
        while True:
            time.sleep(0.1)
    elif words[0] == "flood":
        while True:
            sys.stdout.write("x" * 4096)
            sys.stdout.flush()
    elif words[0] == "fail":
        sys.exit(7)
    else:
        print(" ".join(words), flush=True)
'''
LINUX = sys.platform.startswith("linux")


class CaseFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def case(self, text="showpid\npwd\nquit\n", config=None, name="hw1/basic"):
        folder = self.root / "tests" / name
        folder.mkdir(parents=True, exist_ok=True)
        if text is not None:
            (folder / "input.txt").write_text(text, encoding="utf-8")
        if config is not None:
            (folder / "case.json").write_text(json.dumps(config), encoding="utf-8")
        return Case(self.root / "tests", folder)


class FrameworkTests(CaseFixture):
    def test_discovery_excludes_expected_and_fixtures(self):
        case = self.case()
        for name in ("fixtures", "expected"):
            folder = case.path / name
            folder.mkdir()
            (folder / "input.txt").write_text("not a case")
        self.assertEqual([case.id], [item.id for item in discover(self.root / "tests")])

    def test_select_cases_groups_and_globs(self):
        self.case(name="hw1/aliases/one")
        self.case(name="hw1/signals/two")
        self.assertEqual(1, len(discover(self.root / "tests", ["hw1/aliases/*"])))
        self.assertEqual("hw1/signals/two", discover(self.root / "tests", group="signals")[0].id)

    def test_unknown_setting_rejected(self):
        with self.assertRaises(SuiteError):
            self.case(config={"timeuot": 2})

    def test_invalid_deadlines_rejected(self):
        for timeout in (0, -1, True, float("inf"), float("nan")):
            with self.assertRaises(SuiteError):
                self.case(config={"timeout": timeout})

    def test_fixture_paths_cannot_escape(self):
        for path in ("../secret", "a/../../secret", "."):
            with self.assertRaises(SuiteError):
                inside(self.root, path)
        self.assertEqual(self.root / "a" / "b", inside(self.root, "a/b"))

    @unittest.skipUnless(LINUX, "Linux symlinks")
    def test_fixture_symlink_cannot_escape(self):
        os.symlink("/etc", str(self.root / "outside"))
        with self.assertRaises(SuiteError):
            inside(self.root, "outside/passwd")

    def test_pid_tokens_preserve_identity_and_other_numbers(self):
        case = self.case()
        normalizer = Normalizer(case, self.root, {"SHELL_PID": 100, "WORKDIR": str(self.root)})
        actual = normalizer.text("smash pid is 100\nsignal number 9 was sent to pid 200\nsmash: process 200 was killed\nsize 100, job 1\n")
        self.assertEqual("smash pid is {{SHELL_PID}}\nsignal number 9 was sent to pid {{PID:1}}\nsmash: process {{PID:1}} was killed\nsize 100, job 1\n", actual)

    def test_distinct_live_pid_bindings_stay_distinct(self):
        normalizer = Normalizer(self.case(), self.root, {"SHELL_PID": 100, "one": 200, "two": 201})
        text = normalizer.text("signal number 9 was sent to pid 200\nsignal number 9 was sent to pid 201\n")
        self.assertIn("{{PID:one}}", text)
        self.assertIn("{{PID:two}}", text)

    def test_wrong_shell_pid_is_rejected(self):
        normalizer = Normalizer(self.case(), self.root, {"SHELL_PID": 100})
        with self.assertRaises(SuiteError):
            normalizer.text("smash pid is 101\n")

    def test_expansion_escapes_regex_values(self):
        self.assertEqual(r"/tmp/a\.b", expand("{{WORKDIR}}", {"WORKDIR": "/tmp/a.b"}, regex=True))
        with self.assertRaises(SuiteError):
            expand("{{unknown}}", {})

    def test_reference_roundtrip_and_staleness(self):
        case = self.case()
        expected = {"exit_code": 0, "streams": {"stdout": "hello\n", "stderr": ""},
                    "files": {"out.txt": {"kind": "text", "content": "hi\n"},
                              "raw.bin": {"kind": "binary", "content": b"\x00\xff"},
                              "absent": {"kind": "missing"}}}
        save_reference(case, expected, {"binary_sha256": "fixture"})
        self.assertEqual(expected, load_reference(case))
        (case.path / "input.txt").write_text("changed\n")
        with self.assertRaisesRegex(SuiteError, "STALE"):
            load_reference(case)

    def test_fixture_changes_invalidate_reference(self):
        case = self.case()
        fixtures = case.path / "fixtures"
        fixtures.mkdir()
        (fixtures / "a.txt").write_text("one")
        before = case.fingerprint()
        (fixtures / "a.txt").write_text("two")
        self.assertNotEqual(before, case.fingerprint())

    def test_failed_generation_preserves_existing_reference(self):
        case = self.case()
        result = {"exit_code": 0, "streams": {"stdout": "old"}, "files": {}}
        save_reference(case, result, {})
        with mock.patch(__name__ + ".write_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                save_reference(case, {"exit_code": 0, "streams": {"stdout": "new"}, "files": {}}, {})
        self.assertEqual(result, load_reference(case))

    def test_exact_comparison_checks_whitespace_and_streams(self):
        base = {"exit_code": 0, "streams": {"stdout": "hello\n", "stderr": ""}, "files": {}}
        other = {"exit_code": 0, "streams": {"stdout": "hello \n", "stderr": ""}, "files": {}}
        self.assertTrue(differences(base, other))
        other["streams"] = {"stdout": "", "stderr": "hello\n"}
        self.assertTrue(differences(base, other))


@unittest.skipUnless(LINUX, "Execution, PTYs, and process cleanup require Linux")
class LinuxIntegrationTests(CaseFixture):
    def setUp(self):
        super().setUp()
        self.binary = self.root / "fake-smash"
        self.binary.write_text(FAKE_PROGRAM, encoding="utf-8")
        self.binary.chmod(0o755)

    def run_case(self, case):
        return execute_case(case, self.binary, self.root / "logs")

    def test_generate_and_compare_across_real_pid_changes(self):
        case = self.case()
        reference = self.run_case(case)
        save_reference(case, reference, {})
        self.assertEqual([], differences(load_reference(case), self.run_case(case)))
        self.assertIn("{{SHELL_PID}}", reference["streams"]["stdout"])
        self.assertIn("{{WORKDIR}}", reference["streams"]["stdout"])

    def test_stdout_stderr_and_output_files(self):
        case = self.case("error\nwrite out.txt\nquit\n", {"files": ["out.txt"]})
        result = self.run_case(case)
        self.assertEqual("smash error: fixture error\n", result["streams"]["stderr"])
        self.assertEqual("{{WORKDIR}}\n", result["files"]["out.txt"]["content"])

    def test_actual_ctrl_c_uses_observed_child_pid(self):
        case = self.case(None, {"mode": "interactive", "timeout": 5, "steps": [
            {"expect": "smash> "}, {"send": "sleep 30"},
            {"wait_child": "foreground", "command": "sleep", "foreground": True}, {"ctrl_c": True},
            {"expect": "smash: process {{foreground}} was killed\n"},
            {"expect": "smash> "}, {"send": "quit"}]})
        one = self.run_case(case)
        two = self.run_case(case)
        self.assertEqual([], differences(one, two))
        self.assertIn("{{PID:foreground}}", one["streams"]["transcript"])

    def test_explicit_signal_steps(self):
        case = self.case(None, {"mode": "interactive", "steps": [
            {"expect": "smash> "}, {"signal": {"name": "SIGINT", "target": "shell"}},
            {"expect": "smash: got ctrl-C\n"}, {"send": "quit"}]})
        self.assertEqual(0, self.run_case(case)["exit_code"])

    def test_timeout_cleans_children_in_separate_process_groups(self):
        case = self.case(None, {"mode": "interactive", "timeout": 0.7, "steps": [
            {"expect": "smash> "}, {"send": "sleep 30 &"},
            {"wait_child": "background", "command": "sleep"}, {"expect": "smash> "},
            {"send": "hang"}, {"expect": "never printed"}]})
        with self.assertRaises(SuiteError):
            self.run_case(case)
        events = json.loads((self.root / "logs" / "events.json").read_text())
        child = next(event["pid"] for event in events if "wait_child" in event)
        info = process_info(child)
        self.assertTrue(info is None or info["state"] == "Z")

    def test_output_limit_stops_a_runaway_shell(self):
        with self.assertRaisesRegex(SuiteError, "Output exceeded"):
            self.run_case(self.case("flood\n", {"max_output_bytes": 1024}))

    def test_crash_or_unexpected_exit_cannot_become_a_reference(self):
        with self.assertRaisesRegex(SuiteError, "exit code"):
            self.run_case(self.case("fail\n"))

    def test_wrong_pid_is_not_normalized_away(self):
        with self.assertRaisesRegex(SuiteError, "showpid reported"):
            self.run_case(self.case("badpid\nquit\n"))

    def test_build_runs_make_smash_without_touching_original(self):
        source = self.root / "solution"
        source.mkdir()
        (source / "fake.py").write_text(FAKE_PROGRAM, encoding="utf-8")
        (source / "stale.o").write_text("old object")
        (source / "Makefile").write_text("smash: stale.o\n\tcp fake.py smash\n\tchmod +x smash\nstale.o:\n\tprintf fresh > stale.o\n")
        temporary = self.root / "build-temp"
        temporary.mkdir()
        binary = build_source(source, temporary, self.root / "build.log", 10)
        self.assertEqual("fresh", (binary.parent / "stale.o").read_text())
        self.assertEqual("old object", (source / "stale.o").read_text())
        self.assertFalse((source / "smash").exists())

    def test_cli_reference_update_is_explicit(self):
        case = self.case()
        args = ["--tests", str(self.root / "tests"), "--results", str(self.root / "results"),
                "--reference-exe", str(self.binary)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, run_command(args, generate=True))
            original = (case.path / "expected" / "result.json").read_bytes()
            self.assertEqual(0, run_command(args, generate=True))
            self.assertEqual(original, (case.path / "expected" / "result.json").read_bytes())
            self.assertEqual(0, run_command(args + ["--update"], generate=True))




def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print("Usage: python3 test_suite.py {run,generate-reference,list,self-test} [options]")
        print("  run                 Build with make smash and compare saved references")
        print("  generate-reference  Record your explicitly selected master build")
        print("  list                List cases and reference status without running")
        print("  self-test           Test the runner; Linux adds PTY/process checks")
        print("Use COMMAND --help for command options.")
        return 0
    command = args.pop(0)
    if command == "run":
        return run_command(args)
    if command == "generate-reference":
        return run_command(args, generate=True)
    if command == "list":
        return run_command(["--list"] + args)
    if command == "self-test":
        if args:
            print("Usage: python3 test_suite.py self-test", file=sys.stderr)
            return 2
        tests = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        result = unittest.TextTestRunner(verbosity=2).run(tests)
        return 0 if result.wasSuccessful() else 1
    print("Unknown command: " + command, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
