# Copyright 2026 The Harness2 Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Google Antigravity CLI (agy) editable surface, native project files and stream-json renderer."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Mapping

from .. import providers
from ..core import runmark as RM
from ..core import tree as HT
from ..core.tree import SurfaceSpec
from ..settings import ConfigError
from ._native import check_cli, checked_path, cli_argv, export_files, native_tree, surface_files
from .base import Install
from .codex import _TRUNC_FMT, _clip, _text_of

ANTIGRAVITY_SURFACE = SurfaceSpec(
    name="antigravity",
    always_on=("AGENTS.md",),
    always_on_max=24_000,
    always_on_cap_reason="AGENTS.md is loaded whole into every session, and Antigravity caps "
    "each rule file at 24,000 characters",
    skill_dir=".agents/skills",
    skill_name_rule="overrides_dir",
    trigger_hint="",
    autotrigger_file="AGENTS.md",
    trigger_line="",
    subagent_requires_mode=False,
    plugin_dir=None,
    hooks_file=".agents/hooks.json",
    hook_events=frozenset(
        {
            "SessionStart",
            "PreToolUse",
            "PostToolUse",
            "PreInvocation",
            "PostInvocation",
            "Stop",
        }
    ),
    compile_skill_scripts=True,
    reject_empty_always_on=False,
    script_dir=".agents/scripts",
    skills_need_trigger=False,
    stubs=(),
)

NATIVE_PATHS = (
    "AGENTS.md",
    ".agents/skills",
    ".agents/agents",
    ".agents/scripts",
    ".agents/hooks.json",
)

_PROMPT_ARG_MAX = 120_000


def _agent_body(name: str, body: str) -> str:
    frontmatter = re.match(r"\A---\r?\n(.*?)^---(?:\r?\n|$)", body, re.S | re.M)
    if frontmatter:
        fm = frontmatter.group(1)
        name_line = re.search(r"^name:[ \t]*(.*)$", fm, re.M)
        if not name_line:
            fm = f"name: {name}\n" + fm
        elif not name_line.group(1).strip():
            fm = fm[: name_line.start()] + f"name: {name}" + fm[name_line.end() :]
        sub_line = re.search(r"^subagent:[ \t]*(.*)$", fm, re.M)
        if not sub_line:
            if not fm.endswith("\n"):
                fm += "\n"
            fm += "subagent: true\n"
        return body[: frontmatter.start(1)] + fm + body[frontmatter.end(1) :]
    return f"---\nname: {name}\nsubagent: true\n---\n{body}"


def _validate_hooks_doc(rel: str, content: str) -> str:
    try:
        doc = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise ConfigError(f"{rel} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict) or not doc:
        raise ConfigError(f"{rel} must contain a non-empty JSON object")
    if "hooks" in doc:
        if not isinstance(doc["hooks"], dict):
            raise ConfigError(f'{rel} needs a top-level "hooks" object')
    elif not all(isinstance(v, dict) for v in doc.values()):
        raise ConfigError(f'{rel} needs a top-level "hooks" object or named hook objects')
    return json.dumps(doc, indent=2) + "\n"


def _native_files(tree: HT.Tree) -> HT.Tree:
    files = {}
    for rel, content in surface_files(ANTIGRAVITY_SURFACE, tree).items():
        if rel == ANTIGRAVITY_SURFACE.hooks_file:
            files[rel] = _validate_hooks_doc(rel, content)
        elif rel.startswith("sub_agents/"):
            name = rel.split("/")[1]
            files[f".agents/agents/{name}.md"] = _agent_body(name, content)
        else:
            files[rel] = content
    return files


class AntigravitySubstrate:
    """Google Antigravity CLI (agy) behind the shared Substrate interface."""

    name = "antigravity"
    NATIVE_PATHS = NATIVE_PATHS

    def __init__(self, bin: str = ""):
        self.bin = bin

    def surface(self) -> SurfaceSpec:
        return ANTIGRAVITY_SURFACE

    def materialise(self, tree_dir: Path, spec, ctx: Mapping[str, str]) -> Install:
        if spec.install_mode != "antigravity_root":
            raise ValueError(
                f"AntigravitySubstrate needs install_mode 'antigravity_root', got {spec.install_mode!r}"
            )
        staged = Path(tempfile.mkdtemp(prefix="harness2-antigravity-"))
        try:
            self.export_tree(HT.read_tree(ANTIGRAVITY_SURFACE, tree_dir), staged, base={})
            return Install(staged, RM.fingerprint(tree_dir), {"install_mode": spec.install_mode})
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise

    def import_tree(
        self,
        root: Path,
        *,
        rejected: list[str] | None = None,
        fallback_sibling: bool = False,
    ) -> HT.Tree:
        tree = native_tree(
            ANTIGRAVITY_SURFACE,
            root,
            ("AGENTS.md", ".agents/skills", ".agents/scripts"),
            rejected,
        )
        agents = checked_path(root, ".agents/agents")
        for path in sorted(agents.rglob("*")) if agents.is_dir() else []:
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            canonical = f"sub_agents/{path.stem}/AGENT.md"
            if (
                path.parent == agents
                and path.suffix == ".md"
                and HT.is_allowed(ANTIGRAVITY_SURFACE, canonical)
            ):
                tree[canonical] = checked_path(root, rel).read_text(
                    encoding="utf-8", errors="replace"
                )
            elif rejected is not None:
                rejected.append(rel)
        hooks_path = checked_path(root, ANTIGRAVITY_SURFACE.hooks_file)
        if hooks_path.exists():
            raw = hooks_path.read_text(encoding="utf-8", errors="replace")
            tree[ANTIGRAVITY_SURFACE.hooks_file] = _validate_hooks_doc(
                ANTIGRAVITY_SURFACE.hooks_file, raw
            )
        if not tree and fallback_sibling:
            for alt_prompt in ("GEMINI.md", "CLAUDE.md"):
                alt_path = checked_path(root, alt_prompt)
                if alt_path.is_file():
                    tree["AGENTS.md"] = alt_path.read_text(encoding="utf-8", errors="replace")
                    break
            claude_skills = checked_path(root, ".claude/skills")
            for path in sorted(claude_skills.rglob("*")) if claude_skills.is_dir() else []:
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                sub_rel = path.relative_to(claude_skills).as_posix()
                mapped = f".agents/skills/{sub_rel}"
                if HT.is_allowed(ANTIGRAVITY_SURFACE, mapped):
                    tree[mapped] = path.read_text(encoding="utf-8", errors="replace")
        return tree

    def export_tree(self, tree: HT.Tree, root: Path, *, base: HT.Tree) -> dict[str, list[str]]:
        files = _native_files(tree)
        old = _native_files(base)
        return export_files(root, files, old)

    def strip_native(self, workspace: Path) -> None:
        self.export_tree({}, workspace, base=self.import_tree(workspace))

    def install_into(self, install: Install, workspace: Path) -> None:
        self.export_tree(self.import_tree(install.staging_dir), workspace, base={})

    def argv(
        self,
        prompt_file: Path,
        workspace: Path,
        model: str,
        effort: str,
        *,
        provider: providers.Provider | None = None,
    ) -> list[str]:
        try:
            prompt_text = prompt_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            prompt_text = ""
        if not prompt_text.strip() or len(prompt_text) > _PROMPT_ARG_MAX:
            prompt_text = f"Read the task instructions in {prompt_file} and complete the task."
        return [
            *cli_argv(self.bin, "agy"),
            "-p",
            prompt_text,
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
            "--model",
            model,
            "--effort",
            effort,
        ]

    def env(
        self,
        base_env: Mapping[str, str],
        provider: providers.Provider,
        config_dir: Path | None = None,
    ) -> dict[str, str]:
        return {
            **{k: v for k, v in base_env.items() if k not in providers.STRIP_ENV},
            **providers.antigravity_env(provider),
        }

    def read_trajectory(
        self, stream_path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 2000
    ) -> str:
        return render_events(
            stream_path, obs_trunc=obs_trunc, max_chars=max_chars, args_trunc=args_trunc
        )

    def final_message(self, stream_path: Path) -> str:
        return final_message(stream_path)

    def cost_usd(self, stream_path: Path) -> float:
        return cost_usd(stream_path)

    def preflight(self) -> None:
        check_cli(self.bin, "agy")


def parse_events(path: Path) -> list[dict]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").replace("\x00", "")
    except OSError as exc:
        return [{"type": "_read_error", "message": str(exc)}]
    events = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def render_events(path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 2000) -> str:
    lines: list[str] = []
    step_buffers: dict[int, tuple[str, list[str]]] = {}
    emitted_steps: set[int] = set()
    emitted_assistant_texts: list[str] = []

    def flush_step(idx: int) -> None:
        if idx in emitted_steps or idx not in step_buffers:
            return
        stype, parts = step_buffers[idx]
        text = "".join(parts).strip()
        if not text:
            return
        emitted_steps.add(idx)
        if stype == "agent_response":
            lines.append(f"[ASSISTANT] {_clip(text, obs_trunc)}")
            emitted_assistant_texts.append(text)
        elif stype == "thinking":
            lines.append(f"[REASONING] {_clip(text, obs_trunc)}")

    for event in parse_events(path):
        if event.get("type") == "_read_error":
            return f"(failed to read trajectory: {event.get('message')})"
        ev_kind = event.get("event") or event.get("type")
        if ev_kind == "init" or (ev_kind == "system" and event.get("subtype") == "init"):
            init = event.get("init") if isinstance(event.get("init"), dict) else event
            lines.append(f"[HARNESS] init | model={init.get('model')} cwd={init.get('cwd')}")
            continue
        if ev_kind == "step_update" and isinstance(event.get("step_update"), dict):
            su = event["step_update"]
            idx = su.get("step_index")
            stype = su.get("step_type", "")
            state = su.get("state", "")
            if stype in ("agent_response", "thinking") and isinstance(idx, int):
                _, parts = step_buffers.setdefault(idx, (stype, []))
                delta = su.get("text_delta") or su.get("text") or ""
                if isinstance(delta, str) and delta:
                    parts.append(delta)
                if state in ("DONE", "ERROR"):
                    flush_step(idx)
            elif stype == "tool":
                for prev_idx in sorted(step_buffers):
                    flush_step(prev_idx)
                tinfo = su.get("tool_info") if isinstance(su.get("tool_info"), dict) else {}
                tname = tinfo.get("name") or su.get("tool_name") or "tool"
                if state in ("DONE", "ERROR") or (
                    isinstance(idx, int) and idx not in emitted_steps and tinfo
                ):
                    if isinstance(idx, int):
                        emitted_steps.add(idx)
                    params = json.dumps(tinfo.get("parameters", {}), ensure_ascii=False)
                    lines.append(f"[ACTION] {tname}: {_clip(params, args_trunc)}")
                    output = _text_of(tinfo.get("output")) or str(tinfo.get("output") or "")
                    if state == "ERROR":
                        lines.append(
                            f"[OBSERVATION ERROR] {_clip(output or su.get('error') or 'tool error', obs_trunc)}"
                        )
                    elif output:
                        lines.append(f"[OBSERVATION] {_clip(output, obs_trunc)}")
            continue
        if ev_kind == "result" or ("status" in event and ("response" in event or "error" in event)):
            for prev_idx in sorted(step_buffers):
                flush_step(prev_idx)
            res = event.get("result") if isinstance(event.get("result"), dict) else event
            status = res.get("status")
            is_err = bool(res.get("is_error") or (status is not None and status != "SUCCESS"))
            if is_err:
                text = (
                    res.get("error")
                    or res.get("response")
                    or (res.get("result") if isinstance(res.get("result"), str) else "")
                    or _text_of(res.get("errors"))
                    or status
                    or "error"
                )
                lines.append(f"[ERROR] {_clip(text, obs_trunc)}")
            else:
                resp = res.get("response") or (
                    res.get("result") if isinstance(res.get("result"), str) else ""
                )
                if (
                    isinstance(resp, str)
                    and resp.strip()
                    and resp.strip() not in emitted_assistant_texts
                ):
                    lines.append(f"[ASSISTANT] {_clip(resp, obs_trunc)}")
            continue

    for prev_idx in sorted(step_buffers):
        flush_step(prev_idx)

    rendered = "\n".join(lines).strip()
    if not rendered:
        return "(no parseable events in trajectory)"
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "\n" + _TRUNC_FMT.format(n=len(rendered) - max_chars)
    return rendered


def final_message(path: Path) -> str:
    last = ""
    result = None
    step_buffers: dict[int, list[str]] = {}
    for event in parse_events(path):
        ev_kind = event.get("event") or event.get("type")
        if ev_kind == "step_update" and isinstance(event.get("step_update"), dict):
            su = event["step_update"]
            if su.get("step_type") == "agent_response":
                idx = su.get("step_index")
                delta = su.get("text_delta") or su.get("text") or ""
                if isinstance(idx, int) and isinstance(delta, str) and delta:
                    step_buffers.setdefault(idx, []).append(delta)
                    combined = "".join(step_buffers[idx]).strip()
                    if combined:
                        last = combined
        elif ev_kind == "result" or ("status" in event and "response" in event):
            res = event.get("result") if isinstance(event.get("result"), dict) else event
            resp = res.get("response") if isinstance(res.get("response"), str) else res.get("result")
            if isinstance(resp, str) and resp.strip():
                result = resp
    return result if result is not None else last


def cost_usd(path: Path) -> float:
    for event in reversed(parse_events(path)):
        ev_kind = event.get("event") or event.get("type")
        if ev_kind == "result" or "status" in event:
            res = event.get("result") if isinstance(event.get("result"), dict) else event
            value = res.get("total_cost_usd") if "total_cost_usd" in res else res.get("cost_usd")
            return float(value) if isinstance(value, (int, float)) else 0.0
    return 0.0
