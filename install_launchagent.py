#!/usr/bin/env python3
"""Install (or reinstall) the meeting-notifier LaunchAgent.

Renders launchagent.plist.template with real paths, writes it to
~/Library/LaunchAgents/, and loads it via `launchctl bootstrap`. Idempotent:
running again re-renders the plist and reloads.

Usage:
    python3 install_launchagent.py [--label LABEL] [--venv-python PATH]
                                   [--poller PATH] [--logdir PATH]

Defaults:
    --label         net.ryland.meeting-notifier
    --venv-python   ~/Library/Application Support/meeting-notifier/venv/bin/python3
    --poller        <directory of this script>/poller.py
    --logdir        ~/Library/Logs/meeting-notifier/

Run without arguments for the standard dev install.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_LABEL = "net.ryland.meeting-notifier"
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_VENV_PYTHON = Path.home() / "Library" / "Application Support" \
                                  / "meeting-notifier" / "venv" / "bin" / "python3"
DEFAULT_LOGDIR = Path.home() / "Library" / "Logs" / "meeting-notifier"
LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


def render(template: str, **fills) -> str:
    out = template
    for key, val in fills.items():
        out = out.replace("@" + key + "@", str(val))
    return out


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(repr(c) if " " in c else c for c in cmd))
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label", default=DEFAULT_LABEL,
                   help=f"LaunchAgent label (default: {DEFAULT_LABEL})")
    p.add_argument("--app", type=Path, default=None,
                   help="Path to MeetingNotifier.app — when set, the LaunchAgent "
                        "uses the .app's internal binary instead of the venv-python "
                        "+ poller.py source path. Mutually exclusive with --poller / "
                        "--venv-python.")
    p.add_argument("--venv-python", type=Path, default=DEFAULT_VENV_PYTHON,
                   help=f"Path to the venv's python3 (default: {DEFAULT_VENV_PYTHON})")
    p.add_argument("--poller", type=Path, default=PROJECT_DIR / "poller.py",
                   help=f"Path to poller.py (default: {PROJECT_DIR / 'poller.py'})")
    p.add_argument("--logdir", type=Path, default=DEFAULT_LOGDIR,
                   help=f"Directory for stdout/stderr logs (default: {DEFAULT_LOGDIR})")
    args = p.parse_args()

    # Validate inputs depending on mode.
    if args.app:
        binary = args.app / "Contents" / "MacOS" / "MeetingNotifier"
        if not binary.exists():
            sys.exit(f".app binary not found at {binary}")
        # The bundled binary routes on argv (see main.py): no args → setup GUI,
        # "alert" → alert subprocess, anything else → poller daemon. The
        # LaunchAgent MUST pass --daemon, or launchd launches the GUI on a
        # KeepAlive loop instead of the poller. (Source mode below runs
        # poller.py directly, which is already the daemon, so it needs no flag.)
        program_args = [str(binary), "--daemon"]
        working_dir = args.app.parent     # cwd = directory containing the .app
    else:
        if not args.venv_python.exists():
            sys.exit(f"venv python not found at {args.venv_python}. "
                     f"Set up the venv first (see README) or pass --venv-python.")
        if not args.poller.exists():
            sys.exit(f"poller.py not found at {args.poller}.")
        program_args = [str(args.venv_python), str(args.poller)]
        working_dir = args.poller.parent

    template_path = PROJECT_DIR / "launchagent.plist.template"
    if not template_path.exists():
        sys.exit(f"template missing: {template_path}")

    # Ensure target dirs exist.
    LAUNCHAGENTS_DIR.mkdir(parents=True, exist_ok=True)
    args.logdir.mkdir(parents=True, exist_ok=True)
    # Tighten log dir to owner-only so meeting titles + Zoom URLs (which
    # often contain ?pwd= tokens) aren't world-readable on shared Macs.
    try:
        os.chmod(args.logdir, 0o700)
    except OSError:
        pass

    plist_path = LAUNCHAGENTS_DIR / f"{args.label}.plist"
    stdout_log = args.logdir / "stdout.log"
    stderr_log = args.logdir / "stderr.log"

    program_args_xml = "\n".join(
        f"        <string>{p}</string>" for p in program_args
    )
    rendered = render(
        template_path.read_text(),
        LABEL=args.label,
        PROGRAM_ARGS=program_args_xml,
        WORKING_DIR=working_dir,
        STDOUT_LOG=stdout_log,
        STDERR_LOG=stderr_log,
    )

    # If already loaded, bootout first (idempotent reinstall).
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{args.label}"
    print(f"=== Installing LaunchAgent {args.label} ===")
    print(f"  plist:   {plist_path}")
    print(f"  program: {' '.join(program_args)}")
    print(f"  cwd:     {working_dir}")
    print(f"  logs:    {stdout_log}, {stderr_log}")
    print()

    # bootout if running
    r = run(["launchctl", "print", target], check=False)
    if r.returncode == 0:
        print("  (already loaded; unloading first)")
        run(["launchctl", "bootout", target], check=False)
        # launchd needs a moment to fully release the label before bootstrap will
        # accept it again — without this sleep, bootstrap returns exit 5.
        import time as _time
        _time.sleep(1)

    # Atomic plist write that refuses to follow a symlink. ~/Library/
    # LaunchAgents/ is user-writable, so without this a local malware
    # process could plant a symlink at our path and redirect the rendered
    # plist somewhere else; the resulting launchd-loaded plist would then
    # run code at every login.
    import tempfile as _tempfile, stat as _stat
    try:
        st = os.lstat(plist_path)
    except FileNotFoundError:
        st = None
    if st is not None and _stat.S_ISLNK(st.st_mode):
        sys.exit(f"refusing to write through symlink at {plist_path}; "
                 "remove and re-run if this was intentional.")
    fd, tmp = _tempfile.mkstemp(dir=str(plist_path.parent),
                                prefix=".tmp-", suffix=".plist")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(rendered)
        os.chmod(tmp, 0o600)
        os.replace(tmp, plist_path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    print(f"  wrote {plist_path}")

    # bootstrap (load)
    run(["launchctl", "bootstrap", domain, str(plist_path)])
    print(f"  bootstrapped (loaded)")

    # kickstart so it starts immediately (without waiting for next trigger)
    run(["launchctl", "kickstart", "-k", target], check=False)
    print(f"  kickstarted (now running)")

    print()
    print("Status check:")
    r = run(["launchctl", "print", target], check=False)
    print(r.stdout[:500] if r.stdout else r.stderr[:500])


if __name__ == "__main__":
    main()
