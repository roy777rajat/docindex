"""Offline structural checks for generated definitions (AWS validates syntax; this catches wiring mistakes)."""
from __future__ import annotations

import json
import re
from typing import Any

PLACEHOLDER = re.compile(r"\$\{(\w+)\}")
TERMINAL = {"Succeed", "Fail"}


def _targets(state: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if "Next" in state:
        out.append(state["Next"])
    for c in state.get("Choices", []):
        out.append(c["Next"])
    if "Default" in state:
        out.append(state["Default"])
    for c in state.get("Catch", []):
        out.append(c["Next"])
    return out


def validate(defn: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    states = defn["States"]
    if defn["StartAt"] not in states:
        problems.append(f"StartAt {defn['StartAt']} missing")
    for name, st in states.items():
        t = st["Type"]
        if t not in TERMINAL and t != "Choice" and "Next" not in st and not st.get("End"):
            problems.append(f"{name}: no Next/End")
        for tgt in _targets(st):
            if tgt not in states:
                problems.append(f"{name}: unknown target {tgt}")
        if t == "Task" and st["Resource"].endswith("http:invoke") and st.get("TimeoutSeconds", 99) > 60:
            problems.append(f"{name}: HTTP task timeout above 60 s")
        if t == "Map":
            inner = st["ItemProcessor"]
            sub = {"StartAt": inner["StartAt"], "States": inner["States"]}
            problems += [f"{name}/{p}" for p in validate(sub)]
    seen, stack = set(), [defn["StartAt"]]
    while stack:
        n = stack.pop()
        if n in seen or n not in states:
            continue
        seen.add(n)
        stack.extend(_targets(states[n]))
    unreachable = set(states) - seen
    if unreachable:
        problems.append("unreachable states: " + ", ".join(sorted(unreachable)))
    if len(json.dumps(defn)) > 1_000_000:
        problems.append("definition larger than 1 MB")
    return problems


def placeholders(defn: dict[str, Any]) -> set[str]:
    return set(PLACEHOLDER.findall(json.dumps(defn)))
