"""
Collect real subscription usage for Claude Code and Codex (wisen/usage.py) and
write it as a usage packet through the egress gate. `wisen worker` does this
itself on a paired laptop; this writes the file for an unpaired setup.

Usage: python3 scripts/collect_usage.py [output.json]
       (default: ~/.wisen/exports/reflections/usage.json)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wisen import egress
from wisen.packets import usage_packet
from wisen.usage import collect


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "~/.wisen/exports/reflections/usage.json").expanduser()
    egress.write_file(usage_packet(collect()), out)
    print(f"usage written to {out}")


if __name__ == "__main__":
    main()
