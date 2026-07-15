"""Read-only adapters for CoC and module-generation skill repositories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillDocument:
    id: str
    title: str
    source: str
    path: Path


class SkillCatalog:
    def __init__(self, *, coc_root: Path, modulegen_root: Path) -> None:
        self._roots = {"coc": coc_root, "modulegen": modulegen_root}

    def list(self) -> list[SkillDocument]:
        values: list[SkillDocument] = []
        for source, root in self._roots.items():
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("SKILL.md")):
                relative = path.relative_to(root).parent
                suffix = "root" if relative == Path(".") else ".".join(relative.parts)
                values.append(
                    SkillDocument(
                        id=f"{source}.{suffix}",
                        title=self._title(path, suffix),
                        source=source,
                        path=path,
                    )
                )
        return values

    def read(self, skill_id: str) -> str:
        for document in self.list():
            if document.id == skill_id:
                return document.path.read_text(encoding="utf-8")
        raise LookupError(skill_id)

    @staticmethod
    def _title(path: Path, fallback: str) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return fallback
