"""python3 -m qscopr --help"""

import argparse
import json
import sys
from pathlib import Path

from .gate import check, load_state, promote, publication
from .source import GateError


def main():
    parser = argparse.ArgumentParser(description="Quickshell Fedora 44 release gate")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    checker = commands.add_parser("check", help="Check release dependencies and build its RPM offline")
    checker.add_argument("--tag", help="Stable release tag (default: upstream latest)")
    checker.add_argument("--output", type=Path, default=Path("artifacts/latest"))
    checker.add_argument("--force", action="store_true", help="Rebuild even if this release was validated")
    checker.add_argument("--verify-tracked", action="store_true", help="Rebuild the maintained release for publication")
    checker.add_argument("--promote", action="store_true", help="Promote the spec only after successful validation")
    for command in ["promote", "publication"]:
        sub = commands.add_parser(command)
        sub.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project = args.project.resolve()
    output = args.output.resolve()
    try:
        if args.command == "check":
            if args.verify_tracked and args.promote:
                parser.error("--verify-tracked cannot be combined with --promote")
            tag = args.tag
            if args.verify_tracked and not tag:
                state = load_state(project)
                if not state:
                    raise GateError("discovery-blocked", "No maintained validated release exists")
                tag = state["release"]["tag"]
            report = check(project, output, tag, args.force, args.verify_tracked)
            print(json.dumps(report, indent=4, sort_keys=True))
            if report["status"] == "validated" and args.promote:
                promote(project, output)
            if report["status"] in {"build-failed", "infrastructure-failed"}:
                return 1
            if report["status"] in {"dependency-blocked", "discovery-blocked"}:
                return 2
        elif args.command == "promote":
            promote(project, output)
            print("Promoted the validated spec and release manifest.")
        else:
            publication(project, output)
            print("Verified publication manifest written.")
    except (GateError, OSError, ValueError, KeyError) as error:
        print(f"Gate refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
