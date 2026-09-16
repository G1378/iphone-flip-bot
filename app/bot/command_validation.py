"""Validate every slash command/group/option/choice against Discord's
documented limits, entirely locally (no network / bot token needed).

Used by both `scripts/validate_commands.py` (manual pre-deploy check) and
`tests/test_command_tree.py` (so a future command that would trigger a
CommandSyncFailure, HTTP 400 error code 50035, fails the test suite
instead of only surfacing when the bot tries to start on the Pi).
"""
from __future__ import annotations

import re

from discord import app_commands

NAME_RE = re.compile(r"^[-_\w]{1,32}$", re.UNICODE)
MAX_DESC = 100
MAX_NAME = 32
MAX_OPTIONS = 25
MAX_CHOICES = 25
MAX_TOP_LEVEL = 100
MAX_SUBCOMMANDS_PER_GROUP = 25


def _check_name(kind: str, path: str, name: str, errors: list[str]) -> None:
    if not (1 <= len(name) <= MAX_NAME):
        errors.append(f"{kind} '{path}': name '{name}' is {len(name)} chars (must be 1-{MAX_NAME}).")
    if not NAME_RE.match(name):
        errors.append(f"{kind} '{path}': name '{name}' contains invalid characters (or uppercase) for Discord's naming rules.")
    if name.lower() != name:
        errors.append(f"{kind} '{path}': name '{name}' must be lowercase.")


def _check_description(kind: str, path: str, description: str, errors: list[str]) -> None:
    if not (1 <= len(description) <= MAX_DESC):
        errors.append(f"{kind} '{path}': description is {len(description)} chars (must be 1-{MAX_DESC}): {description!r}")


def _check_choices(path: str, param, errors: list[str]) -> None:
    choices = getattr(param, "choices", None) or []
    if len(choices) > MAX_CHOICES:
        errors.append(f"parameter '{path}': {len(choices)} choices (max {MAX_CHOICES}).")
    seen_names = set()
    for choice in choices:
        if not (1 <= len(choice.name) <= MAX_DESC):
            errors.append(f"parameter '{path}': choice name '{choice.name}' is {len(choice.name)} chars (must be 1-{MAX_DESC}).")
        if choice.name in seen_names:
            errors.append(f"parameter '{path}': duplicate choice name '{choice.name}'.")
        seen_names.add(choice.name)


def _check_command(cmd: app_commands.Command, path: str, errors: list[str]) -> None:
    _check_name("command", path, cmd.name, errors)
    _check_description("command", path, cmd.description, errors)
    params = list(cmd.parameters)
    if len(params) > MAX_OPTIONS:
        errors.append(f"command '{path}': {len(params)} parameters (max {MAX_OPTIONS}).")
    for param in params:
        param_path = f"{path} {param.name}"
        _check_name("parameter", param_path, param.name, errors)
        _check_description("parameter", param_path, param.description or "", errors)
        _check_choices(param_path, param, errors)


def _check_group(group: app_commands.Group, path: str, errors: list[str], depth: int = 0) -> None:
    _check_name("group", path, group.name, errors)
    _check_description("group", path, group.description, errors)
    children = list(group.commands)
    if len(children) > MAX_SUBCOMMANDS_PER_GROUP:
        errors.append(f"group '{path}': {len(children)} subcommands (max {MAX_SUBCOMMANDS_PER_GROUP}).")
    if depth >= 2:
        errors.append(f"group '{path}': nested more than 2 levels deep - Discord only allows command -> group -> subcommand.")
    for child in children:
        child_path = f"{path} {child.name}"
        if isinstance(child, app_commands.Group):
            _check_group(child, child_path, errors, depth + 1)
        else:
            _check_command(child, child_path, errors)


def collect_command_tree_errors(top_level: list) -> list[str]:
    """Given `bot.tree.get_commands()`, return a list of human-readable
    problems that would cause Discord to reject `tree.sync()` with a
    CommandSyncFailure. Empty list means the tree is clean."""
    errors: list[str] = []

    if len(top_level) > MAX_TOP_LEVEL:
        errors.append(f"top level: {len(top_level)} commands (max {MAX_TOP_LEVEL}).")

    seen_top_names = set()
    for cmd in top_level:
        if cmd.name in seen_top_names:
            errors.append(f"top level: duplicate command name '{cmd.name}'.")
        seen_top_names.add(cmd.name)

        path = f"/{cmd.name}"
        if isinstance(cmd, app_commands.Group):
            _check_group(cmd, path, errors)
        else:
            _check_command(cmd, path, errors)

    return errors
