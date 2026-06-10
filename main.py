#!/usr/bin/env python3
"""Single entry point for the bundled .app.

Three roles, routed by argv:

    MeetingNotifier              -> foreground app: setup/settings GUI
                                    (first run shows wizard; later launches show
                                     settings with current values pre-loaded)
    MeetingNotifier --daemon     -> background poller daemon (LaunchAgent target)
    MeetingNotifier alert ARGS   -> alert subprocess for one meeting

The Finder/dock launch case (no args) routes to the GUI so the user gets a
proper Calendar TCC prompt and a friendly settings panel instead of having to
edit TOML by hand. The GUI handles install/restart of the LaunchAgent on Save.
"""
import sys


def main():
    args = sys.argv[1:]
    if args and args[0] == "alert":
        sys.argv = [sys.argv[0]] + args[1:]
        from alert_runner import main as alert_main
        sys.exit(alert_main())
    elif args:
        # Any other CLI args route to the poller, which owns --daemon (the
        # LaunchAgent-invoked mode) and the operator/debug flags --list,
        # --once, --init-config, --config. The GUI takes no args.
        from poller import main as poller_main
        poller_main()
    else:
        from setup_gui import main as gui_main
        sys.exit(gui_main())


if __name__ == "__main__":
    main()
