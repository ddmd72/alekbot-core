"""A drive parser child that prints document text to stderr and then fails, for the log-redaction test (R3 F3-5):
`python -m tests.unit.adapters.drive_parser_stderr_stub`. Never imported by production code."""
import os
import sys

from src.adapters.drive_parser.child import serve

SECRET = "Confidential: the tenant owes the landlord a penalty"


def _handle(request):
    sys.stderr.write(f"Traceback: parser choked near '{SECRET}'\n")
    sys.stderr.flush()
    os._exit(7)


if __name__ == "__main__":
    sys.exit(serve(_handle))
