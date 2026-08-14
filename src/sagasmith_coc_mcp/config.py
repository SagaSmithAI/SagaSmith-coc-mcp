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
    npc_host_token: str | None = None
    module_import_roots: tuple[Path, ...] = ()
    http_host: str = "127.0.0.1"
    http_port: int = 8769
    http_path: str = "/mcp"

    @classmethod
    def from_environment(cls) -> "McpConfig":
        root = _workspace_root()
        raw_module_roots = os.environ.get("SAGASMITH_COC_MCP_MODULE_IMPORT_ROOTS")
        module_roots = (
            tuple(
                Path(value.strip()).expanduser().resolve()
                for value in raw_module_roots.split(os.pathsep)
                if value.strip()
            )
            if raw_module_roots is not None
            else (root / "test_pdfs",)
        )
        return cls(
            home=Path(os.environ.get("SAGASMITH_COC_MCP_HOME", root / ".sagasmith-coc-mcp"))
            .expanduser()
            .resolve(),
            database_url=os.environ.get("SAGASMITH_COC_DATABASE_URL"),
            coc_skills_dir=Path(
                os.environ.get("SAGASMITH_COC_SKILLS_DIR", root / "SagaSmith-coc-skills")
            )
            .expanduser()
            .resolve(),
            modulegen_skills_dir=Path(
                os.environ.get(
                    "SAGASMITH_MODULEGEN_SKILLS_DIR", root / "SagaSmith-module-gen-skills"
                )
            )
            .expanduser()
            .resolve(),
            bound_principal_id=(
                value.strip()
                if (value := os.environ.get("SAGASMITH_COC_MCP_BOUND_PRINCIPAL_ID"))
                and value.strip()
                else None
            ),
            npc_host_token=(
                value.strip()
                if (value := os.environ.get("SAGASMITH_NPC_HOST_TOKEN"))
                and value.strip()
                else None
            ),
            module_import_roots=tuple(path.resolve() for path in module_roots),
            http_host=os.environ.get("SAGASMITH_COC_MCP_HTTP_HOST", "127.0.0.1"),
            http_port=int(os.environ.get("SAGASMITH_COC_MCP_HTTP_PORT", "8769")),
            http_path=os.environ.get("SAGASMITH_COC_MCP_HTTP_PATH", "/mcp"),
        )

    @property
    def database_path(self) -> Path:
        return self.home / "data" / "ttrpgbase.db"

    @property
    def modules_dir(self) -> Path:
        return self.home / "artifacts" / "modules"

    @property
    def rules_dir(self) -> Path:
        return self.home / "artifacts" / "rules"

    @property
    def content_packages_dir(self) -> Path:
        return self.home / "artifacts" / "content-packages"

    @property
    def module_assets_dir(self) -> Path:
        return self.home / "artifacts" / "module-assets"

    @property
    def normalized_modules_dir(self) -> Path:
        return self.home / "artifacts" / "normalized-modules"

    @property
    def normalized_rules_dir(self) -> Path:
        return self.home / "artifacts" / "normalized-rules"

    @property
    def npc_conversations_dir(self) -> Path:
        return self.home / "runtime" / "npc-conversations"

    def prepare(self) -> None:
        for directory in (
            self.database_path.parent,
            self.modules_dir,
            self.rules_dir,
            self.content_packages_dir,
            self.module_assets_dir,
            self.normalized_modules_dir,
            self.normalized_rules_dir,
            self.npc_conversations_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
