import argparse
from importlib.metadata import version


class Options(argparse.Namespace):
    command: str | None
    text: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bunny",
        description="Bunny.ai experimental CLI scaffold",
    )
    parser.add_argument(
        "--version", action="version", version=f"Bunny.ai {version('bunny-ai')}"
    )
    commands = parser.add_subparsers(dest="command")
    demo = commands.add_parser("demo", help="Run one simulated offline turn")
    demo.add_argument("text", help="Text for the simulated turn")
    args = parser.parse_args(argv, namespace=Options())

    if args.command is None:
        parser.print_help()
        parser.exit()

    text = args.text.strip()
    if not text:
        demo.error("demo text must contain a non-whitespace character")

    print(f"[Offline demo, simulated] Received {len(text)} characters.")
    return 0
