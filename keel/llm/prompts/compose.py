"""
Modular prompt composition for Keel decision policies.

Loads versioned text modules from the package (default) or an optional
filesystem override directory. Assembles system/user prompts and applies
length + safety basics (inspired by R20 Prompt Studio, without the admin UI).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# Defaults aligned with keel.llm.prompts composition (legacy scripts/prompt_library removed).
MAX_MODULE_CHARS = 12_000
MAX_SYSTEM_CHARS = 24_000
MAX_USER_CHARS = 24_000
MAX_TOTAL_CHARS = 40_000
MAX_MODULES = 40

DEFAULT_SYSTEM_PIPELINE: tuple[str, ...] = (
    "system_role.v1",
    "system_rules.v1",
    "system_output.v1",
)
DEFAULT_USER_PIPELINE: tuple[str, ...] = (
    "user_header.v1",
    "user_market.v1",
    "user_task.v1",
)

_PACKAGE_MODULES = "keel.llm.prompts.modules"

_FORBIDDEN = (
    (
        re.compile(r"(?is)(忽略|绕过|取消|覆盖).{0,24}(P0|硬风控|风险门禁|OCO|JSON|止损|保证金上限)"),
        "不得要求忽略或覆盖 P0 与执行层硬约束",
    ),
    (
        re.compile(r"(?is)(允许|可以).{0,20}(逆势补仓|无止损|跳过OCO|突破持仓上限)"),
        "不得放宽逆势补仓、OCO、止损或持仓上限",
    ),
    (
        re.compile(r"(?is)ignore.{0,30}(system|risk|safety|json|oco)"),
        "不得要求忽略系统、风险、安全或 JSON 契约",
    ),
    (
        re.compile(r"(?i)(sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{16,}|api[_ -]?key\s*[:=]\s*\S+)"),
        "提示词中禁止写入 API Key 或密钥",
    ),
)

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
ALLOWED_VARIABLES = {
    "strategy_version",
    "timezone",
    "active_instruments",
    "timestamp",
    "market_block",
    "profile_name",
}


@dataclass(frozen=True)
class PromptModule:
    """One versioned prompt fragment."""

    id: str
    content: str
    source: str = "package"  # package | file | inline
    enabled: bool = True


@dataclass
class AssembledPrompt:
    """Composed system + user prompts with validation metadata."""

    system: str
    user: str
    modules_used: list[str] = field(default_factory=list)
    characters: int = 0
    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.valid and not self.errors


def render_variables(text: str, variables: Mapping[str, Any] | None = None) -> str:
    """Replace ``{{var}}`` placeholders; unknown vars become empty string."""
    values = dict(variables or {})

    def _repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            return ""
        return str(values[key])

    return _VAR_RE.sub(_repl, text)


def validate_assembled(
    system: str,
    user: str,
    *,
    max_system: int = MAX_SYSTEM_CHARS,
    max_user: int = MAX_USER_CHARS,
    max_total: int = MAX_TOTAL_CHARS,
) -> dict[str, Any]:
    """Length + safety basics for an assembled prompt pair."""
    errors: list[str] = []
    warnings: list[str] = []

    if len(system) > max_system:
        errors.append(f"system prompt exceeds {max_system} characters ({len(system)})")
    if len(user) > max_user:
        errors.append(f"user prompt exceeds {max_user} characters ({len(user)})")
    total = len(system) + len(user)
    if total > max_total:
        errors.append(f"combined prompt exceeds {max_total} characters ({total})")
    if not system.strip():
        errors.append("system prompt is empty")
    if not user.strip():
        warnings.append("user prompt is empty")

    for blob, label in ((system, "system"), (user, "user")):
        for pattern, message in _FORBIDDEN:
            if pattern.search(blob):
                errors.append(f"{label}: {message}")
                break

    # Flag unknown template vars left unresolved (still containing {{...}})
    leftover = set(_VAR_RE.findall(system + user))
    unknown = leftover - ALLOWED_VARIABLES
    if leftover:
        # leftover means render didn't substitute — usually missing variables
        warnings.append(f"unresolved template variables: {sorted(leftover)}")
    if unknown:
        errors.append(f"unknown template variables: {sorted(unknown)}")

    return {
        "valid": not errors,
        "errors": list(dict.fromkeys(errors)),
        "warnings": warnings,
        "characters": total,
    }


class PromptComposer:
    """
    Compose system/user prompts from ordered module ids.

    Resolution order for each module id ``name`` (with or without ``.txt``):
    1. ``override_dir / f\"{name}.txt\"`` if override_dir is set
    2. package resource ``keel.llm.prompts.modules.{name}.txt``
    3. inline modules registered via ``register_inline``
    """

    def __init__(
        self,
        *,
        override_dir: Path | str | None = None,
        system_pipeline: Sequence[str] | None = None,
        user_pipeline: Sequence[str] | None = None,
        hot_reload: bool = False,
    ):
        self._override_dir = Path(override_dir) if override_dir else None
        self._system_pipeline = tuple(system_pipeline or DEFAULT_SYSTEM_PIPELINE)
        self._user_pipeline = tuple(user_pipeline or DEFAULT_USER_PIPELINE)
        self._hot_reload = hot_reload
        self._inline: dict[str, str] = {}
        self._cache: dict[str, PromptModule] = {}

    def register_inline(self, module_id: str, content: str) -> None:
        """Register / replace an inline module (tests / dynamic overrides)."""
        mid = _normalize_id(module_id)
        self._inline[mid] = content[:MAX_MODULE_CHARS]
        self._cache.pop(mid, None)

    def load_module(self, module_id: str) -> PromptModule:
        mid = _normalize_id(module_id)
        if not self._hot_reload and mid in self._cache:
            return self._cache[mid]

        content: str | None = None
        source = "package"

        if self._override_dir is not None:
            path = self._override_dir / f"{mid}.txt"
            if path.is_file():
                content = path.read_text(encoding="utf-8")
                source = "file"

        if content is None and mid in self._inline:
            content = self._inline[mid]
            source = "inline"

        if content is None:
            content = _read_package_module(mid)
            source = "package"

        if content is None:
            raise FileNotFoundError(f"prompt module not found: {mid}")

        module = PromptModule(id=mid, content=content.strip()[:MAX_MODULE_CHARS], source=source)
        self._cache[mid] = module
        return module

    def list_available(self) -> list[str]:
        """Module ids visible from package + override dir + inline."""
        found: set[str] = set(self._inline)
        try:
            root = resources.files(_PACKAGE_MODULES)
            for item in root.iterdir():
                name = getattr(item, "name", "")
                if name.endswith(".txt"):
                    found.add(name[:-4])
        except (TypeError, OSError, ModuleNotFoundError):
            pass
        if self._override_dir and self._override_dir.is_dir():
            for path in self._override_dir.glob("*.txt"):
                found.add(path.stem)
        return sorted(found)

    def compose(
        self,
        *,
        variables: Mapping[str, Any] | None = None,
        system_pipeline: Sequence[str] | None = None,
        user_pipeline: Sequence[str] | None = None,
    ) -> AssembledPrompt:
        """Load pipelines, render variables, validate, return AssembledPrompt."""
        sys_ids = list(system_pipeline or self._system_pipeline)
        usr_ids = list(user_pipeline or self._user_pipeline)
        if len(sys_ids) + len(usr_ids) > MAX_MODULES:
            return AssembledPrompt(
                system="",
                user="",
                valid=False,
                errors=[f"total modules exceed {MAX_MODULES}"],
            )

        used: list[str] = []
        system_parts: list[str] = []
        user_parts: list[str] = []
        load_errors: list[str] = []

        for mid in sys_ids:
            try:
                mod = self.load_module(mid)
            except FileNotFoundError as exc:
                load_errors.append(str(exc))
                continue
            if mod.enabled and mod.content:
                system_parts.append(render_variables(mod.content, variables))
                used.append(mod.id)

        for mid in usr_ids:
            try:
                mod = self.load_module(mid)
            except FileNotFoundError as exc:
                load_errors.append(str(exc))
                continue
            if mod.enabled and mod.content:
                user_parts.append(render_variables(mod.content, variables))
                used.append(mod.id)

        system = "\n\n".join(system_parts).strip()
        user = "\n\n".join(user_parts).strip()
        check = validate_assembled(system, user)
        errors = load_errors + list(check["errors"])
        return AssembledPrompt(
            system=system,
            user=user,
            modules_used=used,
            characters=int(check["characters"]),
            valid=not errors,
            errors=list(dict.fromkeys(errors)),
            warnings=list(check["warnings"]),
        )


def format_market_block(snapshots: Mapping[str, Any]) -> str:
    """
    Render a compact market/factor block for the user prompt.

    Accepts MarketSnapshot-like objects (attributes) or plain dicts.
    """
    lines: list[str] = []
    for inst_id, snap in snapshots.items():
        if isinstance(snap, Mapping):
            price = float(snap.get("price") or 0)
            rsi = float(snap.get("rsi_14") or 0)
            atr = float(snap.get("atr_14") or 0)
            trend = str(snap.get("trend_15m") or "")
            macd_h = float(snap.get("macd_histogram") or 0)
            name = str(snap.get("name") or inst_id)
        else:
            price = float(getattr(snap, "price", 0) or 0)
            rsi = float(getattr(snap, "rsi_14", 0) or 0)
            atr = float(getattr(snap, "atr_14", 0) or 0)
            trend = str(getattr(snap, "trend_15m", "") or "")
            macd_h = float(getattr(snap, "macd_histogram", 0) or 0)
            name = str(getattr(snap, "name", inst_id) or inst_id)
        lines.append(
            f"- {inst_id} ({name}): price={price:.6g} rsi14={rsi:.2f} "
            f"atr14={atr:.6g} trend15m={trend} macd_hist={macd_h:.6g}"
        )
    return "\n".join(lines) if lines else "(no market data)"


def _normalize_id(module_id: str) -> str:
    mid = str(module_id).strip()
    if mid.endswith(".txt"):
        mid = mid[:-4]
    return mid


def _read_package_module(module_id: str) -> str | None:
    try:
        root = resources.files(_PACKAGE_MODULES)
        path = root.joinpath(f"{module_id}.txt")
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except (TypeError, OSError, ModuleNotFoundError, AttributeError):
        # Fallback for older importlib or zipimport edge cases
        pkg_dir = Path(__file__).resolve().parent / "modules"
        path = pkg_dir / f"{module_id}.txt"
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return None
