#!/usr/bin/env python3
"""Utilities for parametric LQN model generation.

Provides functions to modify LQN model parameters (e.g., client multiplicity)
and write the result to a temporary file for lqsim evaluation.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path


def set_client_multiplicity(
    lqn_path: str | Path,
    task_name: str,
    new_m: int,
) -> Path:
    """Read a .lqn file, replace multiplicity on a task line, write to tempfile.

    Modifies the `m <N>` token on the line matching `t <task_name> ...`.
    The caller is responsible for cleaning up the returned temporary file.

    Args:
        lqn_path: Path to the source .lqn model file.
        task_name: Name of the task whose multiplicity to change.
        new_m: New multiplicity value.

    Returns:
        Path to the temporary .lqn file with modified multiplicity.

    Raises:
        ValueError: If the task line is not found in the model.
    """
    content = Path(lqn_path).read_text()

    # Match: t <task_name> ... m <N> — replace N with new_m
    pattern = rf"(t\s+{re.escape(task_name)}\s+.*\bm\s+)\d+"
    new_content, count = re.subn(pattern, rf"\g<1>{new_m}", content)

    if count == 0:
        raise ValueError(
            f"Task '{task_name}' with multiplicity field not found in {lqn_path}"
        )

    # Write to a named tempfile with .lqn extension (lqsim requires it)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".lqn", delete=False, prefix=f"lqn_{task_name}_m{new_m}_"
    )
    tmp.write(new_content)
    tmp.close()

    return Path(tmp.name)
