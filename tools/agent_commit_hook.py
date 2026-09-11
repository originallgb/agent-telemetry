import argparse
import fnmatch
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_CONFIG = ".agent-commit-hook.json"
SECRET_PATTERNS = [
    (
        re.compile(
            r"([\"']?[A-Za-z0-9_.-]*(?:token|secret|password|passwd|authorization|"
            r"cookie|api[_-]?key)[\"']?\s*[:=]\s*)([^\s,;}\]]+)",
            re.I,
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(https?://)([^/@\s]+)@", re.I),
        r"\1[REDACTED]@",
    ),
    (
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I),
        "Bearer [REDACTED]",
    ),
    (
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
            r"AIza[0-9A-Za-z_-]{20,}|ya29\.[0-9A-Za-z_-]{20,})\b"
        ),
        "[REDACTED]",
    ),
]


def utc_now():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def run(cmd, cwd, timeout=12):
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "cmd": cmd,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": redact(proc.stdout.strip()),
            "stderr": redact(proc.stderr.strip()),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        return {
            "cmd": cmd,
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": redact(str(exc)),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }


def redact(value):
    if not value:
        return value
    redacted = value
    for pattern, replacement in SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def load_config(repo):
    path = repo / DEFAULT_CONFIG
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def resolve_repo_root():
    result = run(["git", "rev-parse", "--show-toplevel"], Path.cwd(), timeout=5)
    if result["ok"] and result["stdout"]:
        return Path(result["stdout"]).resolve()
    return Path.cwd().resolve()


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def safe_rel(path, root):
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def matches_any(rel_path, patterns):
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in patterns)


def iter_included_files(repo, config):
    include_globs = config.get("include_globs") or [
        "README.md",
        "*.py",
        "tools/*.py",
        ".githooks/*",
    ]
    exclude_globs = config.get("exclude_globs") or [
        ".git/**",
        "__pycache__/**",
        ".agent/**",
        "*.pyc",
    ]

    candidates = []
    for pattern in include_globs:
        candidates.extend(repo.glob(pattern))

    seen = set()
    for path in sorted(candidates):
        if not path.is_file():
            continue
        rel = safe_rel(path, repo)
        if rel in seen or matches_any(rel, exclude_globs):
            continue
        seen.add(rel)
        yield rel, path


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_file_snapshots(repo, config):
    max_bytes = int(config.get("max_file_bytes", 120000))
    snapshots = []
    for rel, path in iter_included_files(repo, config):
        stat = path.stat()
        item = {
            "path": rel,
            "size_bytes": stat.st_size,
            "sha256": sha256_file(path),
            "captured_text": stat.st_size <= max_bytes,
        }
        if item["captured_text"]:
            try:
                item["text"] = redact(
                    path.read_text(encoding="utf-8", errors="replace")
                )
            except OSError as exc:
                item["captured_text"] = False
                item["read_error"] = str(exc)
        snapshots.append(item)
    return snapshots


def capture_globbed_text(repo, globs, max_bytes):
    captured = []
    seen = set()
    for pattern in globs:
        for path in sorted(repo.glob(pattern)):
            if not path.is_file():
                continue
            rel = safe_rel(path, repo)
            if rel in seen:
                continue
            seen.add(rel)
            stat = path.stat()
            item = {
                "path": rel,
                "size_bytes": stat.st_size,
                "sha256": sha256_file(path),
                "captured_text": stat.st_size <= max_bytes,
            }
            if item["captured_text"]:
                item["text"] = redact(
                    path.read_text(encoding="utf-8", errors="replace")
                )
            else:
                with path.open("rb") as file:
                    file.seek(max(0, stat.st_size - max_bytes))
                    item["tail_text"] = redact(
                        file.read().decode("utf-8", errors="replace")
                    )
                item["captured_tail_bytes"] = max_bytes
            captured.append(item)
    return captured


def parse_numstat(numstat):
    added = 0
    deleted = 0
    files = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        if parts[0].isdigit():
            added += int(parts[0])
        if parts[1].isdigit():
            deleted += int(parts[1])
    return {"files_changed": files, "lines_added": added, "lines_deleted": deleted}


def derive_decision_report(commands, file_snapshots, session_files, log_files):
    changed = commands["name_status"]["stdout"].splitlines()
    changed_paths = [line.split("\t")[-1] for line in changed if line.strip()]
    tracked_context = [item["path"] for item in file_snapshots]

    decisions = [
        "Persist commit-time agent memory outside the repository while keeping a small local report pointer.",
        "Redact common secret shapes before writing command output, environment, config, or captured file text.",
        "Capture bounded file snapshots by configured globs instead of archiving the whole working tree.",
        "Make the hook non-blocking: post-commit reports failures but does not reject a finished commit.",
    ]

    learnings = [
        "Future agents should inspect the generated memory JSON and markdown report before changing project workflow.",
        "The hook cannot capture private chat transcripts unless they are explicitly exported into configured files.",
        "Large generated artifacts are excluded by default; hashes and Git metadata carry the durable audit trail.",
    ]

    outcomes = [
        f"Captured commit metadata for {commands['commit']['stdout'] or 'unknown commit'}.",
        f"Observed {len(changed_paths)} changed path entries from Git.",
        f"Captured {len(tracked_context)} configured context files for onboarding and meta-analysis.",
        f"Captured {len(session_files)} session note files and {len(log_files)} local log files.",
    ]

    return {
        "decisions": decisions,
        "learnings": learnings,
        "outcomes": outcomes,
        "changed_paths": changed_paths,
        "captured_context_paths": tracked_context,
        "captured_session_paths": [item["path"] for item in session_files],
        "captured_log_paths": [item["path"] for item in log_files],
    }


def capture_environment():
    allowlist = [
        "COMPUTERNAME",
        "OS",
        "PROCESSOR_ARCHITECTURE",
        "SHELL",
        "TERM",
        "USERNAME",
        "USERDOMAIN",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PATH",
    ]
    return {
        key: redact(os.environ.get(key, "")) for key in allowlist if os.environ.get(key)
    }


def write_json(path, payload):
    with private_text_file(path, append=False) as file:
        json.dump(payload, file, indent=2, sort_keys=True)
        file.write("\n")


def append_jsonl(path, payload):
    with private_text_file(path, append=True) as file:
        file.write(json.dumps(payload, sort_keys=True) + "\n")


def private_text_file(path, append):
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    os.chmod(path, 0o600)
    return os.fdopen(descriptor, "a" if append else "w", encoding="utf-8")


def write_markdown(path, payload):
    report = payload["report"]
    metrics = payload["metrics"]
    lines = [
        f"# Commit Agent Report: {payload['commit']['short']}",
        "",
        f"- Event: `{payload['event']}`",
        f"- Timestamp: `{payload['timestamp_utc']}`",
        f"- Branch: `{payload['git']['branch']}`",
        f"- Commit: `{payload['commit']['hash']}`",
        f"- Subject: {payload['commit']['subject']}",
        "",
        "## Outcomes",
        *[f"- {item}" for item in report["outcomes"]],
        "",
        "## Decisions",
        *[f"- {item}" for item in report["decisions"]],
        "",
        "## Learnings",
        *[f"- {item}" for item in report["learnings"]],
        "",
        "## Metrics",
        f"- Files changed: {metrics['files_changed']}",
        f"- Lines added: {metrics['lines_added']}",
        f"- Lines deleted: {metrics['lines_deleted']}",
        f"- Captured context files: {metrics['captured_context_files']}",
        f"- Captured session files: {metrics['captured_session_files']}",
        f"- Captured log files: {metrics['captured_log_files']}",
        f"- Hook runtime: {metrics['hook_duration_ms']} ms",
        "",
        "## Changed Paths",
        *[f"- `{path}`" for path in report["changed_paths"]],
        "",
        "## Captured Context",
        *[f"- `{path}`" for path in report["captured_context_paths"]],
        "",
        "## Captured Sessions",
        *[f"- `{path}`" for path in report["captured_session_paths"]],
        "",
        "## Captured Logs",
        *[f"- `{path}`" for path in report["captured_log_paths"]],
        "",
        "Full structured payload is stored next to this report as JSON.",
        "",
    ]
    with private_text_file(path, append=False) as file:
        file.write("\n".join(lines))


def main():
    started = time.perf_counter()
    parser = argparse.ArgumentParser(description="Capture commit-time agent memory.")
    parser.add_argument("--event", default="manual")
    args = parser.parse_args()

    repo = resolve_repo_root()
    config = load_config(repo)

    memory_dir_str = config.get("memory_dir", str(repo / ".agent-memory"))
    memory_dir = ensure_dir(Path(memory_dir_str).expanduser())
    repo_report_dir = ensure_dir(
        repo / config.get("repo_report_dir", ".agent/commit-reports")
    )

    commands = {
        "commit": run(["git", "rev-parse", "HEAD"], repo),
        "short": run(["git", "rev-parse", "--short", "HEAD"], repo),
        "branch": run(["git", "branch", "--show-current"], repo),
        "subject": run(["git", "log", "-1", "--pretty=%s"], repo),
        "author": run(["git", "log", "-1", "--pretty=%an <%ae>"], repo),
        "status": run(["git", "status", "--short"], repo),
        "name_status": run(
            [
                "git",
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-status",
                "-r",
                "HEAD",
            ],
            repo,
        ),
        "numstat": run(
            ["git", "diff-tree", "--root", "--no-commit-id", "--numstat", "-r", "HEAD"],
            repo,
        ),
        "shortstat": run(
            ["git", "show", "--stat", "--oneline", "--no-renames", "HEAD"], repo
        ),
        "remotes": run(["git", "remote", "-v"], repo),
    }

    if config.get("capture_git_config", True):
        commands["git_config"] = run(["git", "config", "--list", "--show-origin"], repo)
    if config.get("capture_gh_status", True):
        commands["gh_auth_status"] = run(["gh", "auth", "status"], repo, timeout=8)

    file_snapshots = (
        capture_file_snapshots(repo, config)
        if config.get("capture_repo_snapshot", True)
        else []
    )
    session_files = (
        capture_globbed_text(
            repo,
            config.get("session_globs", []),
            int(config.get("max_file_bytes", 120000)),
        )
        if config.get("capture_sessions", True)
        else []
    )
    log_files = (
        capture_globbed_text(
            repo, config.get("log_globs", []), int(config.get("max_log_bytes", 120000))
        )
        if config.get("capture_logs", True)
        else []
    )
    report = derive_decision_report(commands, file_snapshots, session_files, log_files)
    metrics = parse_numstat(commands["numstat"]["stdout"])
    metrics["captured_context_files"] = len(file_snapshots)
    metrics["captured_session_files"] = len(session_files)
    metrics["captured_log_files"] = len(log_files)
    metrics["hook_duration_ms"] = int((time.perf_counter() - started) * 1000)

    commit_hash = commands["commit"]["stdout"] or "unknown"
    short_hash = commands["short"]["stdout"] or commit_hash[:12]
    payload = {
        "schema_version": 1,
        "event": args.event,
        "timestamp_utc": utc_now(),
        "repo": {
            "path": str(repo),
            "name": repo.name,
        },
        "commit": {
            "hash": commit_hash,
            "short": short_hash,
            "subject": commands["subject"]["stdout"],
            "author": commands["author"]["stdout"],
        },
        "git": {
            "branch": commands["branch"]["stdout"],
            "status_short": commands["status"]["stdout"],
            "commands": commands,
        },
        "runtime": {
            "python": sys.version,
            "platform": platform.platform(),
            "argv": sys.argv,
        },
        "environment": capture_environment()
        if config.get("capture_environment", True)
        else {},
        "context_files": file_snapshots,
        "session_files": session_files,
        "log_files": log_files,
        "report": report,
        "metrics": metrics,
    }

    json_name = f"{utc_now().replace(':', '').replace('-', '')}-{short_hash}.json"
    md_name = json_name.replace(".json", ".md")
    write_json(memory_dir / json_name, payload)
    write_markdown(memory_dir / md_name, payload)
    append_jsonl(memory_dir / "events.jsonl", payload)

    local_pointer = {
        "timestamp_utc": payload["timestamp_utc"],
        "commit": payload["commit"],
        "memory_json": str(memory_dir / json_name),
        "memory_markdown": str(memory_dir / md_name),
        "metrics": metrics,
    }
    write_json(repo_report_dir / f"{short_hash}.json", local_pointer)

    print(f"agent commit report: {memory_dir / md_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
