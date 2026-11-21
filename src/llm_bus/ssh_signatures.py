"""Detached SSH verification arguments, independent of signer trust and process policy."""

from pathlib import Path


def verification_command(
    executable: str, allowed: Path, principal: str, namespace: str, signature: Path
) -> list[str]:
    """Build the SSH protocol command; callers retain their own trust and execution checks."""
    return [
        executable,
        "-Y",
        "verify",
        "-f",
        str(allowed),
        "-I",
        principal,
        "-n",
        namespace,
        "-s",
        str(signature),
    ]
