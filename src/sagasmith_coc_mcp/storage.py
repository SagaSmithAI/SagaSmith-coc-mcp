"""MCP-owned SQLite and managed module artifact boundary."""

from __future__ import annotations

import hashlib
import re
import shutil
from mimetypes import guess_type
from pathlib import Path
from typing import Any

from sagasmith_core import Database, file_sha256
from sagasmith_core.database import sqlite_database_url
from sagasmith_core.managed_artifacts import (
    read_content_archive as read_managed_content_archive,
)
from sagasmith_core.managed_artifacts import (
    write_content_archive as write_managed_content_archive,
)

from .config import McpConfig


class SagaSmithStorage:
    def __init__(self, config: McpConfig) -> None:
        self.config = config
        config.prepare()
        self.database = Database(config.database_url or sqlite_database_url(config.database_path))

    def migrate(self) -> None:
        self.database.upgrade_schema()

    def status(self) -> dict[str, Any]:
        return {
            "home": str(self.config.home),
            "database": {
                "url": self.database.url,
                "path": str(self.config.database_path),
                "exists": self.config.database_path.exists(),
            },
            "modules_dir": str(self.config.modules_dir),
            "rules_dir": str(self.config.rules_dir),
            "content_packages_dir": str(self.config.content_packages_dir),
        }

    def write_module(self, name: str, content: str) -> Path:
        if not name.strip():
            raise ValueError("module name must not be empty")
        if len(content.encode("utf-8")) > 20 * 1024 * 1024:
            raise ValueError("module artifact exceeds the 20 MiB safety limit")
        filename = name if name.casefold().endswith(".md") else f"{name}.md"
        target = (self.config.modules_dir / filename).resolve()
        if target.parent != self.config.modules_dir.resolve():
            raise ValueError("module name must not contain a path")
        target.write_text(content, encoding="utf-8")
        return target

    def stage_text_module(self, name: str, content: str) -> dict[str, Any]:
        """Store generated Markdown by content identity without overwriting another draft."""

        if not name.strip():
            raise ValueError("module name must not be empty")
        encoded = content.encode("utf-8")
        if not encoded:
            raise ValueError("module content must not be empty")
        if len(encoded) > 20 * 1024 * 1024:
            raise ValueError("module artifact exceeds the 20 MiB safety limit")
        checksum = hashlib.sha256(encoded).hexdigest()
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).name).strip("-.")
        if not safe_name.casefold().endswith(".md"):
            safe_name = f"{safe_name or 'module'}.md"
        artifact = f"{checksum[:12]}-{safe_name}"
        target = (self.config.modules_dir / artifact).resolve()
        if target.parent != self.config.modules_dir.resolve():
            raise ValueError("invalid module artifact")
        if not target.exists():
            target.write_bytes(encoded)
        elif target.read_bytes() != encoded:
            raise RuntimeError("managed module artifact checksum mismatch")
        return {"artifact": artifact, "path": str(target), "checksum": checksum}

    def stage_rule(self, source_path: str | Path) -> dict[str, Any]:
        """Copy one allowlisted rule source into content-addressed MCP storage."""

        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise LookupError(str(source))
        if source.suffix.casefold() not in {".pdf", ".md", ".markdown", ".txt"}:
            raise ValueError("rule source must be PDF, Markdown, or text")
        if not self.config.module_import_roots or not any(
            source.is_relative_to(root.resolve()) for root in self.config.module_import_roots
        ):
            raise PermissionError("rule source is outside configured import roots")
        if source.stat().st_size > 100 * 1024 * 1024:
            raise ValueError("rule source exceeds the 100 MiB safety limit")
        checksum = file_sha256(source)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip("-.")
        artifact = f"{checksum[:12]}-{safe_name or 'rules' + source.suffix.casefold()}"
        target = (self.config.rules_dir / artifact).resolve()
        if target.parent != self.config.rules_dir.resolve():
            raise ValueError("invalid rule artifact")
        if not target.exists():
            shutil.copy2(source, target)
        elif file_sha256(target) != checksum:
            raise RuntimeError("managed rule artifact checksum mismatch")
        return {"artifact": artifact, "path": str(target), "checksum": checksum}

    def artifact_rule_path(self, name: str) -> Path:
        target = (self.config.rules_dir / name).resolve()
        if target.parent != self.config.rules_dir.resolve() or target.suffix.casefold() not in {
            ".pdf",
            ".md",
            ".markdown",
            ".txt",
        }:
            raise ValueError("invalid managed rule artifact")
        if not target.is_file():
            raise LookupError(name)
        return target

    def stage_module(self, source_path: str | Path) -> dict[str, Any]:
        """Copy an allowlisted source into content-addressed MCP storage."""

        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise LookupError(str(source))
        if source.suffix.casefold() not in {".pdf", ".md", ".markdown", ".txt"}:
            raise ValueError("module must be PDF, Markdown, or text")
        if not self.config.module_import_roots or not any(
            source.is_relative_to(root.resolve()) for root in self.config.module_import_roots
        ):
            raise PermissionError("module source is outside configured import roots")
        if source.stat().st_size > 100 * 1024 * 1024:
            raise ValueError("module exceeds the 100 MiB safety limit")
        checksum = file_sha256(source)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip("-.")
        artifact = f"{checksum[:12]}-{safe_name or 'module' + source.suffix.casefold()}"
        target = (self.config.modules_dir / artifact).resolve()
        if target.parent != self.config.modules_dir.resolve():
            raise ValueError("invalid module artifact")
        if not target.exists():
            shutil.copy2(source, target)
        elif file_sha256(target) != checksum:
            raise RuntimeError("managed module artifact checksum mismatch")
        return {"artifact": artifact, "path": str(target), "checksum": checksum}

    def artifact_module_path(self, name: str) -> Path:
        target = (self.config.modules_dir / name).resolve()
        if target.parent != self.config.modules_dir.resolve() or target.suffix.casefold() not in {
            ".pdf",
            ".md",
            ".markdown",
            ".txt",
        }:
            raise ValueError("invalid managed module artifact")
        if not target.is_file():
            raise LookupError(name)
        return target

    def stage_module_asset(self, module_id: str, source_path: str | Path) -> dict[str, Any]:
        """Copy one allowlisted review asset into module-scoped managed storage."""

        if not re.fullmatch(r"[0-9a-fA-F-]{36}", module_id):
            raise ValueError("invalid module id for managed asset")
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise LookupError(str(source))
        allowed = {
            ".gif",
            ".htm",
            ".html",
            ".jpeg",
            ".jpg",
            ".pdf",
            ".png",
            ".svg",
            ".txt",
            ".webp",
        }
        if source.suffix.casefold() not in allowed:
            raise ValueError("module asset must be an image, PDF, HTML, SVG, or text document")
        if not self.config.module_import_roots or not any(
            source.is_relative_to(root.resolve()) for root in self.config.module_import_roots
        ):
            raise PermissionError("module asset source is outside configured import roots")
        if source.stat().st_size > 100 * 1024 * 1024:
            raise ValueError("module asset exceeds the 100 MiB safety limit")
        checksum = file_sha256(source)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip("-.")
        artifact = f"{checksum[:12]}-{safe_name or 'asset' + source.suffix.casefold()}"
        directory = (self.config.module_assets_dir / module_id).resolve()
        if directory.parent != self.config.module_assets_dir.resolve():
            raise ValueError("invalid managed module asset directory")
        directory.mkdir(parents=True, exist_ok=True)
        target = (directory / artifact).resolve()
        if target.parent != directory:
            raise ValueError("invalid managed module asset path")
        if not target.exists():
            shutil.copy2(source, target)
        elif file_sha256(target) != checksum:
            raise RuntimeError("managed module asset checksum mismatch")
        return {
            "artifact": artifact,
            "path": str(target),
            "checksum": checksum,
            "size": source.stat().st_size,
            "media_type": guess_type(source.name)[0] or "application/octet-stream",
        }

    def store_rendered_module_page(
        self,
        *,
        module_id: str,
        source_checksum: str,
        page_number: int,
        scale: float,
        checksum: str,
        content: bytes,
    ) -> Path:
        """Persist one content-addressed rendered page beneath MCP-owned storage."""

        if not re.fullmatch(r"[0-9a-fA-F-]{36}", module_id):
            raise ValueError("invalid module id for rendered asset")
        directory = (self.config.module_assets_dir / module_id).resolve()
        if directory.parent != self.config.module_assets_dir.resolve():
            raise ValueError("invalid rendered module asset directory")
        directory.mkdir(parents=True, exist_ok=True)
        scale_key = f"{scale:.2f}".replace(".", "-")
        filename = f"{source_checksum[:12]}-page-{page_number:04d}-x{scale_key}-{checksum[:12]}.png"
        target = (directory / filename).resolve()
        if target.parent != directory:
            raise ValueError("invalid rendered module asset path")
        if not target.exists():
            target.write_bytes(content)
        elif file_sha256(target) != checksum:
            raise RuntimeError("managed rendered page checksum mismatch")
        return target

    def write_content_archive(
        self, package: dict[str, Any], blobs: dict[str, bytes]
    ) -> dict[str, Any]:
        return write_managed_content_archive(
            self.config.content_packages_dir,
            package,
            blobs,
        )

    def read_content_archive(
        self, *, artifact: str | None = None, source_path: str | Path | None = None
    ) -> tuple[dict[str, Any], dict[str, bytes]]:
        return read_managed_content_archive(
            self.config.content_packages_dir,
            artifact=artifact,
            source_path=source_path,
            allowed_roots=self.config.module_import_roots,
        )

    def store_content_module_asset(
        self, module_id: str, asset: dict[str, Any], content: bytes
    ) -> str:
        checksum = hashlib.sha256(content).hexdigest()
        if checksum != asset.get("checksum") or len(content) != asset.get("size"):
            raise ValueError("content module asset checksum or size mismatch")
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(asset.get("name") or "asset")).strip("-.")
        directory = (self.config.module_assets_dir / module_id).resolve()
        if directory.parent != self.config.module_assets_dir.resolve():
            raise ValueError("invalid module asset directory")
        directory.mkdir(parents=True, exist_ok=True)
        target = (directory / f"{checksum[:12]}-{safe_name}").resolve()
        if target.parent != directory:
            raise ValueError("invalid module asset path")
        if not target.exists():
            target.write_bytes(content)
        elif file_sha256(target) != checksum:
            raise RuntimeError("managed module asset checksum mismatch")
        return str(target)

    def read_managed_asset(self, source_path: str | Path) -> bytes:
        """Read bytes only from MCP-owned artifact directories."""

        target = Path(source_path).expanduser().resolve()
        roots = (
            self.config.modules_dir.resolve(),
            self.config.rules_dir.resolve(),
            self.config.module_assets_dir.resolve(),
            self.config.content_packages_dir.resolve(),
            self.config.normalized_modules_dir.resolve(),
            self.config.normalized_rules_dir.resolve(),
        )
        if not any(target.is_relative_to(root) for root in roots):
            raise PermissionError("module asset is outside MCP-managed storage")
        if not target.is_file():
            raise LookupError(str(target))
        return target.read_bytes()
