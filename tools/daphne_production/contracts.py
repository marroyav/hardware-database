"""Dependency-free validation of the versioned production contracts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import ProductionError


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionError(f"cannot read JSON contract {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProductionError(f"expected a JSON object in {path}")
    return value


def load_qa_recipe(path: Path) -> dict[str, Any]:
    recipe = load_json(path)
    if recipe.get("contract") != "daphne.qa-recipe" or recipe.get("version") != 1:
        raise ProductionError(f"unsupported QA recipe contract in {path}")
    if not isinstance(recipe.get("recipe_id"), str) or not recipe["recipe_id"]:
        raise ProductionError("QA recipe requires a non-empty recipe_id")
    if not isinstance(recipe.get("recipe_version"), int) or recipe["recipe_version"] < 1:
        raise ProductionError("QA recipe requires recipe_version >= 1")
    release_pattern = recipe.get("release_pattern")
    if not isinstance(release_pattern, str) or not release_pattern:
        raise ProductionError("QA recipe requires a non-empty release_pattern")
    try:
        re.compile(release_pattern)
    except re.error as exc:
        raise ProductionError(f"invalid release_pattern in QA recipe: {exc}") from exc
    tests = recipe.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ProductionError("QA recipe requires at least one test")
    seen: set[str] = set()
    for test in tests:
        if not isinstance(test, dict):
            raise ProductionError("each QA recipe test must be an object")
        test_id = test.get("test_id")
        if not isinstance(test_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", test_id):
            raise ProductionError(f"invalid QA test_id: {test_id!r}")
        if test_id in seen:
            raise ProductionError(f"duplicate QA test_id: {test_id}")
        seen.add(test_id)
        if not isinstance(test.get("required"), bool):
            raise ProductionError(f"test {test_id} requires a boolean 'required'")
        if not isinstance(test.get("description"), str) or not test["description"]:
            raise ProductionError(f"test {test_id} requires a description")
        minimum = test.get("minimum_passes", 1)
        if not isinstance(minimum, int) or minimum < 1:
            raise ProductionError(f"test {test_id} requires minimum_passes >= 1")
    return recipe


def validate_board_config(config: dict[str, Any]) -> None:
    if config.get("contract") != "daphne.board-config" or config.get("version") != 1:
        raise ProductionError("unsupported board configuration contract")
    for section in ("asset", "som", "network", "runtime", "source"):
        if not isinstance(config.get(section), dict):
            raise ProductionError(f"board configuration is missing section: {section}")
