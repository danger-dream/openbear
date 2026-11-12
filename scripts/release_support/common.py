"""Durable state and bounded local execution for the release controller."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path


class ReleaseError(RuntimeError):
    def __init__(self, message: str, *, code: int = 1):
        super().__init__(message)
        self.code = code


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(value) -> str:
    return sha(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def process_ticks(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rfind(")") + 2:].split()[19]
    except (OSError, IndexError):
        return None


def clean_environment() -> dict[str, str]:
    # Docker/test children never receive the publisher's GitHub credential.
    return {k: v for k, v in os.environ.items()
            if k not in {"GH_TOKEN", "GITHUB_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS"}
            and not k.startswith(("GIT_CONFIG_", "GIT_TRACE"))}


class Commands:
    def __init__(self, directory: Path, token: str = "", timeout: int = 1800):
        self.directory = directory
        self.timeout = timeout
        self.secrets = [x for x in (token, base64.b64encode(("x-access-token:" + token).encode()).decode() if token else "") if x]
        self.lock = threading.Lock()
        self.processes: dict[int, subprocess.Popen] = {}

    def redact(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "<redacted>")
        return value

    def __call__(self, args, label: str, *, cwd=None, env=None, timeout=None, check=True) -> str:
        logs = self.directory / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        identifier = f"{time.time_ns()}-{label}"
        log = logs / (identifier + ".log")
        receipt = logs / (identifier + ".json")
        started = time.time()
        process = subprocess.Popen([str(x) for x in args], cwd=cwd, env=clean_environment() if env is None else env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        metadata = {"argv": [self.redact(str(x)) for x in args], "pid": process.pid,
                    "startTicks": process_ticks(process.pid), "startedAt": started, "label": label}
        atomic_json(receipt, metadata)
        with self.lock:
            self.processes[process.pid] = process
        try:
            output, _ = process.communicate(timeout=timeout or self.timeout)
        except BaseException:
            self.stop(process)
            output, _ = process.communicate()
            log.write_text(self.redact(output.decode("utf-8", "replace")), encoding="utf-8")
            metadata.update(finishedAt=time.time(), returncode=process.returncode, interrupted=True)
            atomic_json(receipt, metadata)
            raise
        finally:
            with self.lock:
                self.processes.pop(process.pid, None)
        text = self.redact(output.decode("utf-8", "replace"))
        log.write_text(text, encoding="utf-8")
        metadata.update(finishedAt=time.time(), returncode=process.returncode)
        atomic_json(receipt, metadata)
        if check and process.returncode:
            error = ReleaseError(f"{label}: exit {process.returncode}; log: {log}")
            error.output = text
            raise error
        return text.rstrip("\n")

    @staticmethod
    def stop(process):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
        except ProcessLookupError:
            pass

    def cancel_all(self):
        with self.lock:
            processes = list(self.processes.values())
        for process in processes:
            self.stop(process)

    def recover_children(self):
        """Only terminate exact, still-live processes recorded by this run."""
        for path in (self.directory / "logs").glob("*.json"):
            receipt = load(path)
            if receipt.get("finishedAt") or not receipt.get("startTicks"):
                continue
            pid = receipt["pid"]
            if process_ticks(pid) != receipt["startTicks"]:
                continue
            if os.getpgid(pid) != pid:
                raise ReleaseError(f"Saved process {pid} no longer owns its process group", code=2)
            os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + 10
            while process_ticks(pid) == receipt["startTicks"] and time.monotonic() < deadline:
                time.sleep(0.1)
            if process_ticks(pid) == receipt["startTicks"]:
                os.killpg(pid, signal.SIGKILL)
            receipt.update(finishedAt=time.time(), recovered=True)
            atomic_json(path, receipt)


class State:
    def __init__(self, directory: Path, value: dict):
        self.directory, self.value = directory, value
        self.lock = threading.RLock()

    def save(self):
        with self.lock:
            atomic_json(self.directory / "state.json", self.value)

    def execute(self, name: str, inputs, operation, *, valid=None):
        key = fingerprint(inputs)
        with self.lock:
            stages = self.value.setdefault("stages", {})
            prior = stages.get(name, {})
            if prior.get("status") == "passed" and prior.get("fingerprint") == key:
                result = prior["result"]
                if valid is None or valid(result):
                    prior["lastReusedAt"] = time.time()
                    self.save()
                    print(f"[{name}] reuse verified result", flush=True)
                    return result
            attempt = {"status": "running", "fingerprint": key, "startedAt": time.time()}
            if prior:
                self.value.setdefault("attempts", {}).setdefault(name, []).append(prior)
            stages[name] = attempt
            self.save()
        print(f"[{name}] running", flush=True)
        try:
            result = operation()
            if isinstance(result, dict) and result.get("ok") is False:
                raise ReleaseError(f"{name} returned a failed result")
        except BaseException as exc:
            with self.lock:
                attempt.update(status="paused" if getattr(exc, "code", 1) == 2 else "failed", finishedAt=time.time(),
                               error=f"{type(exc).__name__}: {exc}")
                self.save()
            raise
        with self.lock:
            attempt.update(status="passed", finishedAt=time.time(), result=result)
            self.save()
        print(f"[{name}] passed ({attempt['finishedAt'] - attempt['startedAt']:.2f}s)", flush=True)
        return result
