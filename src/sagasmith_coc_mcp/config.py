"""Configuration and local paths owned by the CoC MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class McpConfig:
    home: Path
    database_url: str | None
    coc_skills_dir: Path
    modulegen_skills_dir: Path
    bound_principal_id: str | None = None

    @classmethod
    def from_environment(cls) -> "McpConfig":
        root = _workspace_root()
        return cls(
            home=Path(
                os.environ.get("SAGASMITH_COC_MCP_HOME", root / ".sagasmith-coc-mcp")
            ).expanduser().resolve(),
            database_url=os.environ.get("SAGASMITH_COC_DATABASE_URL"),
            coc_skills_dir=Path(
                os.environ.get("SAGASMITH_COC_SKILLS_DIR", root / "SagaSmith-coc-skills")
            ).expanduser().resolve(),
            modulegen_skills_dir=Path(
                os.environ.get(
                    "SAGASMITH_MODULEGEN_SKILLS_DIR", root / "SagaSmith-module-gen-skills"
                )
            ).expanduser().resolve(),
            bound_principal_id=(
                value.strip()
                if (value := os.environ.get("SAGASMITH_COC_MCP_BOUND_PRINCIPAL_ID"))
                and value.strip()
                else None
            ),
        )

    @property
    def database_path(self) -> Path:
        return self.home / "data" / "ttrpgbase.db"

    @property
    def modules_dir(self) -> Path:
        return self.home / "artifacts" / "modules"

    def prepare(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.modules_dir.mkdir(parents=True, exist_ok=True)
