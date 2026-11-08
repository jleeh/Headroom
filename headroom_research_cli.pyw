"""Run by the one-shot 'Headroom Research' scheduled task as the LOGGED-ON USER with limited rights (never elevated):
drives the user's own `claude -p` for headroom/research_cli.py. Argument: the run folder.
Everything it writes there is treated as untrusted by the elevated side."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from headroom import research_cli  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) == 2 and os.path.isfile(os.path.join(sys.argv[1], "request.json")):
        research_cli.runner_main(sys.argv[1])
