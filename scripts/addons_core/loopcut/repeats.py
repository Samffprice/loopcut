"""What the model already tried this turn, so that repeating itself costs a line, not a step.

Eval transcripts show turns spending their steps three ways: the same failing call again and
again, the same successful call again (one model sent 8 identical Poly Haven searches a step for
11 steps), and duplicates within one reply. The Ledger remembers failures by what went wrong and
reads by what they asked, and says so in the result the model gets. A failure is only annotated,
never refused, and its full error stays: the fix is usually in it. Only a read that gave the same
result twice, with nothing changed in the scene since, is answered without running it again.

A render job's status is never refused (it moves on by itself), but polling one: an eval turn spent
38 of its 60 steps on status calls, each followed by run_python sleeping or listing the output
folder. Status of a running job without `wait`, from the POLLS-th time in a turn, and code that
sleeps right after a render_job call, get a line pointing to status with wait.

Pure Python and locked: one Ledger per turn now, one shared by a job's workers later.
NoProgress separately counts failures and refusals per call since the last scene change in every loop,
including workers, whose dollars and time are also budgeted.
"""

import hashlib
import json
import re
import threading

FAILING_RUNS = 4   # run_python failures in a row, whatever the errors, before the model is told to step back.
SAME_READS = 2     # The same read with the same result this many times; the next one is not run.
SHOWN_TRIES = 4    # Earlier attempts listed by step; any older ones are counted.
GIST = 60          # Characters of an attempt's gist.
POLLS = 3          # Status calls without wait that find a job running, in a turn, before the model is told to wait.
NO_PROGRESS_FAILURES = 8  # Allow several retries after the repeat notes before stopping an unchanged failure.

_HEX = re.compile(r"0x[0-9a-fA-F]+")
# Names: anything in double quotes, and single-quoted words. A single-quoted phrase with spaces is
# more often the message itself (KeyError: 'bpy_prop_collection[key]: key "Cube" not found').
_QUOTED = re.compile(r"\"[^\"\n]*\"|'[^'\s]*'")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_RUNNING = re.compile(r'"state": "(?:queued|running)"')  # In render_job status's JSON.
_SLEEP = re.compile(r"\bsleep\s*\(")


def call_key(name: str, arguments: str) -> tuple[str, str]:
    """Identical calls share a key: the same tool with the same arguments, in any order or spacing."""
    try:
        parsed = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        return name, arguments.strip()
    return name, json.dumps(parsed, sort_keys=True, separators=(",", ":"))


def error_line(text: str) -> str:
    """What went wrong, from a failed call's result: the exception a traceback ends with, run_python's
    timeout, or else the first line the tool said."""
    output = text.split("\n\nScene changes", 1)[0]
    _, marker, trace = output.rpartition("Traceback (most recent call last):")
    if marker:  # The exception is the unindented lines after the frames; its message may span several.
        return " ".join(line.strip() for line in trace.splitlines() if line.strip() and not line[0].isspace())
    if re.match(r"exit \d+ in ", output):
        # shell's timing is not the error; SyntaxError has no Traceback header.
        lines = output.splitlines()[1:]
        return next((line.strip() for line in lines if re.match(r"\w*(?:Error|Exception):", line)),
                    next((line.strip() for line in lines if line.strip()), ""))
    stopped = output.find("Stopped: the code ran for more than")
    if stopped >= 0:
        return output[stopped:].split(",", 1)[0]
    return next((line.strip() for line in output.splitlines() if line.strip()), "")


def normalised(error: str) -> str:
    """An error with what varies between tries blanked: addresses, quoted names, numbers (line
    numbers among them). The same mistake then keys the same whatever it was called on."""
    error = _HEX.sub("0x?", error)
    error = _QUOTED.sub(lambda m: m.group(0)[0] * 2, error)
    error = _NUMBER.sub("N", error)
    return " ".join(error.split())[:300]


def gist(key: tuple[str, str]) -> str:
    """A few words that tell attempts apart: run_python's own summary, else the arguments."""
    name, arguments = key
    try:
        parsed = json.loads(arguments) if arguments else {}
    except ValueError:
        parsed = {}
    if name == "run_python" and isinstance(parsed, dict):
        text = f"\"{parsed.get('summary') or 'code'}\""
    else:
        text = arguments or "{}"
    return text if len(text) <= GIST else text[:GIST - 1] + "…"


def _arguments(key: tuple[str, str]) -> dict:
    try:
        parsed = json.loads(key[1]) if key[1] else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _listed(tries: list[str]) -> str:
    shown = tries[-SHOWN_TRIES:]
    older = len(tries) - len(shown)
    return (f"{older} earlier, then " if older else "") + ", ".join(shown)


def _steps(steps: list[int]) -> str:
    return ", ".join(map(str, steps[:-1])) + f" and {steps[-1]}" if len(steps) > 1 else str(steps[0])


class NoProgress:
    """Failures and refusals per call_key since the last scene change, consecutive or not.
    Success clears only its key; scene changes, user notes and approval rejections clear all keys.
    Successful reads of other keys preserve counts because they do not change the scene."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._counts: dict[tuple[str, str], int] = {}

    def skip(self, key: tuple[str, str]) -> bool:
        """Count an automatic refusal as no progress, just like an executed failure."""
        return self.record(key, ok=False, changed=False)

    def record(self, key: tuple[str, str], ok: bool, changed: bool) -> bool:
        """Whether this call reaches the limit; a scene change resets even if the call failed."""
        if changed:
            self.reset()
            return False
        if ok:
            self._counts.pop(key, None)
            return False
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key] >= NO_PROGRESS_FAILURES


class Ledger:
    def __init__(self):
        self._lock = threading.Lock()
        self._revision = 0  # Bumped by anything that may have changed the scene.
        self._failures: dict[tuple, list[str]] = {}  # (tool, normalised error) -> "step N gist" per try.
        self._shell_failures: dict[tuple, int] = {}  # Same command and error, ignoring its display summary.
        self._reads: dict[tuple, dict] = {}  # Call key -> revision, digest, steps and where of its latest result.
        self._failing_runs = 0
        self._polls = 0  # Status calls without wait that found a render job running.
        self._last = ""  # The tool of the call recorded before this one.

    def scene_changed(self) -> None:
        with self._lock:
            self._revision += 1

    @property
    def revision(self) -> int:
        """How many times something may have changed the scene this turn."""
        with self._lock:
            return self._revision

    def refusal(self, key: tuple[str, str], in_view=lambda where: True) -> str:
        """Why a read is not run again, or "" to run it: it gave the same result SAME_READS times,
        nothing has changed the scene since, and the latest result is still in view (in_view is
        asked with the `where` it was recorded with; a folded step keeps only a line of it)."""
        with self._lock:
            read = self._reads.get(key)
            if not read or read["revision"] != self._revision or len(read["steps"]) < SAME_READS:
                return ""
            steps, where = list(read["steps"]), read["where"]
        if not in_view(where):
            return ""
        return (f"Not run: you made this same call at steps {_steps(steps)} and got the same result, and nothing "
                f"has changed the scene since, so the result has not changed: see step {steps[-1]}. Go on with "
                f"it, or change something first.")

    def record(self, step: int, key: tuple[str, str], text: str, ok: bool, read: bool = False,
               where=None) -> str:
        """Remember a call that ran and what it returned. Returns the note for its result, or "".
        `read`: the call returns the same while the scene is unchanged; `where` finds its result
        again for refusal()."""
        notes = []
        with self._lock:
            repeated = False
            if not ok:
                error = normalised(error_line(text))
                tries = self._failures.setdefault((key[0], error), [])
                earlier = _listed(tries)
                tries.append(f"step {step} {gist(key)}")
                repeated = len(tries) >= 3
                shell_count = 0
                if key[0] == "shell":
                    command = _arguments(key).get("command")
                    if isinstance(command, str):
                        shell_key = (command, error)
                        shell_count = self._shell_failures.get(shell_key, 0) + 1
                        self._shell_failures[shell_key] = shell_count
                if shell_count >= 2:
                    notes.append(f"(This same shell command has failed the same way {shell_count} times this turn. "
                                 "Do not repeat it unchanged. For Python, write a .py file with write_file and run python3 file.py; "
                                 'for bpy use run_blender(script="script.py", blend="") or run_python for the live scene.)')
                elif repeated:
                    notes.append(f"(This error has now come up {len(tries)} times. Already tried: {earlier}. "
                                 f"This approach is not working: read the error above and check the names in it "
                                 f"with inspect_api or get_object_info, then try a different approach, or stop "
                                 f"and tell the user what is blocking you.)")
                elif len(tries) == 2:
                    notes.append(f"(Same error as before. Already tried: {earlier}.)")
            if key[0] == "run_python":
                self._failing_runs = 0 if ok else self._failing_runs + 1
                if self._failing_runs >= FAILING_RUNS and not repeated:
                    notes.append(f"({self._failing_runs} run_python calls in a row have failed. Step back before "
                                 f"the next one: check the API with inspect_api, read what is there with "
                                 f"get_object_info, try a smaller step, or tell the user what is blocking you.)")
            arguments = _arguments(key) if key[0] in ("render_job", "run_python") else {}
            if (key[0] == "render_job" and arguments.get("action") == "status" and not arguments.get("wait")
                    and _RUNNING.search(text)):
                self._polls += 1
                if self._polls >= POLLS:
                    notes.append(f"(Status checked {self._polls} times this turn while the job runs. Checking "
                                 f"does not speed it up: call status with wait=<seconds> if you need its output "
                                 f"this turn, else finish the turn; the job goes on without you.)")
            if key[0] == "run_python" and self._last == "render_job" and _SLEEP.search(str(arguments.get("code", ""))):
                notes.append("(Sleeping in run_python holds Blender's main thread and does not speed up the job: "
                             "call render_job status with wait=<seconds> instead.)")
            self._last = key[0]
            if read:
                digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
                last = self._reads.get(key)
                if last and last["revision"] == self._revision and last["digest"] == digest:
                    last["steps"].append(step)
                    last["where"] = where
                    if ok:  # A failure has its own note above.
                        notes.append(f"(Same result as your identical call at step {last['steps'][-2]}, and "
                                     f"nothing has changed the scene since: use the result you have. The same "
                                     f"call again will not be run until something changes.)")
                else:
                    self._reads[key] = {"revision": self._revision, "digest": digest, "steps": [step], "where": where}
        return "".join(f"\n\n{note}" for note in notes)
