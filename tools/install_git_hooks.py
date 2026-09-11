import subprocess
from pathlib import Path


def main():
    repo = Path(__file__).resolve().parents[1]
    if not (repo / ".git").exists():
        print("This directory is not a Git repository yet. Run git init first.")
        return 1

    hooks_dir = repo / ".githooks"
    hook = hooks_dir / "post-commit"
    if not hook.exists():
        print(f"Missing hook file: {hook}")
        return 1

    subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"],
        cwd=repo,
        check=True,
    )

    try:
        current_mode = hook.stat().st_mode
        hook.chmod(current_mode | 0o111)
    except OSError:
        # chmod is best-effort on Windows.
        pass

    print("Installed versioned Git hooks from .githooks")
    print("Active hook: post-commit -> tools/agent_commit_hook.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
