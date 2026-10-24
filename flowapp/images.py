from __future__ import annotations

import hashlib
import io
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

from flowapp.adobe import build_iptc_app13, build_xmp, category_number, insert_app13
from flowapp.database import Database, utc_now
from flowapp.schemas import ExportRules


class ImageValidationError(ValueError):
    pass


class ImageStore:
    def __init__(self, data_root: Path, database: Database) -> None:
        self.data_root = data_root
        self.database = database

    def collection_path(self, collection_id: str) -> Path:
        return self.data_root / "collections" / collection_id

    def ensure_collection_dirs(self, collection_id: str) -> Path:
        root = self.collection_path(collection_id)
        for name in ("originals", "selected", "upscaled", "corrections", "exports", "metadata", "workflows"):
            (root / name).mkdir(parents=True, exist_ok=True)
        return root

    def register_candidate(
        self, collection_id: str, concept_id: str, raw: bytes, source_name: str,
    ) -> tuple[str, dict[str, Any]]:
        return self._register(collection_id, concept_id, "originals", "candidate", raw, source_name)

    def register_upscaled(
        self, collection_id: str, concept_id: str, raw: bytes, source_name: str,
    ) -> tuple[str, dict[str, Any]]:
        return self._register(collection_id, concept_id, "upscaled", "upscaled", raw, source_name)

    def register_correction(
        self, collection_id: str, concept_id: str, raw: bytes, source_name: str,
    ) -> tuple[str, dict[str, Any]]:
        return self._register(collection_id, concept_id, "corrections", "correction", raw, source_name)

    def promote_selected(self, asset: dict[str, Any]) -> Path:
        source = Path(asset["path"])
        if not source.is_file():
            raise ImageValidationError("O candidato original não existe mais.")
        destination_dir = self.collection_path(asset["collection_id"]) / "selected"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"selected-{asset['id'][:8]}-{source.name}"
        with destination.open("xb") as selected_file:
            selected_file.write(source.read_bytes())
        self.database.update_asset_path(asset["id"], destination)
        return destination

    def _register(
        self,
        collection_id: str,
        concept_id: str,
        folder: str,
        role: str,
        raw: bytes,
        source_name: str,
    ) -> tuple[str, dict[str, Any]]:
        if not raw:
            raise ImageValidationError("O arquivo de imagem está vazio.")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                width, height = image.size
                image_format = (image.format or "png").lower()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise ImageValidationError("A saída não é uma imagem válida ou excede o limite do Pillow.") from error
        digest = hashlib.sha256(raw).hexdigest()
        for existing in self.database.list_assets(concept_id):
            if existing["sha256"] == digest:
                return existing["id"], existing
        extension = ".jpg" if image_format in {"jpeg", "jpg"} else ".png"
        root = self.ensure_collection_dirs(collection_id)
        filename = f"{role}-{concept_id[:8]}-{digest[:12]}{extension}"
        path = root / folder / filename
        if path.exists():
            filename = f"{role}-{concept_id[:8]}-{digest[:8]}-{uuid.uuid4().hex[:6]}{extension}"
            path = root / folder / filename
        with path.open("xb") as image_file:
            image_file.write(raw)
        try:
            asset_id = self.database.add_asset(
                collection_id, concept_id, role, path, digest, width, height, len(raw),
            )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return asset_id, {
            "id": asset_id,
            "role": role,
            "path": str(path),
            "sha256": digest,
            "width": width,
            "height": height,
            "size_bytes": len(raw),
            "source_name": source_name,
        }

    def export_jpeg(
        self,
        asset: dict[str, Any],
        title: str,
        keywords: list[str],
        rules: ExportRules,
    ) -> tuple[Path, dict[str, Any]]:
        source_path = Path(asset["path"])
        if not source_path.is_file():
            raise ImageValidationError("O arquivo final selecionado não existe mais.")
        with Image.open(source_path) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened)
            width, height = image.size
            megapixels = width * height / 1_000_000
            warnings: list[str] = []
            if rules.min_megapixels is not None and megapixels < rules.min_megapixels:
                warnings.append(f"Abaixo do mínimo configurado ({rules.min_megapixels:g} MP).")
            if rules.max_megapixels is not None and megapixels > rules.max_megapixels:
                warnings.append(f"Acima do máximo configurado ({rules.max_megapixels:g} MP).")
            if rules.technical_rules_verified is not True:
                warnings.append("Requisitos técnicos não verificados com a fonte oficial atual.")
            source_icc = opened.info.get("icc_profile")
            srgb = ImageCms.createProfile("sRGB")
            if source_icc:
                try:
                    image = ImageCms.profileToProfile(
                        image,
                        ImageCms.ImageCmsProfile(io.BytesIO(source_icc)),
                        srgb,
                        outputMode="RGB",
                    )
                except (OSError, ImageCms.PyCMSError) as error:
                    raise ImageValidationError("Não foi possível converter o perfil ICC de origem para sRGB.") from error
            else:
                image = image.convert("RGB")
            srgb_bytes = ImageCms.ImageCmsProfile(srgb).tobytes()
            output = io.BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=rules.jpeg_quality,
                optimize=True,
                icc_profile=srgb_bytes,
                xmp=build_xmp(title, keywords),
            )
        content = insert_app13(output.getvalue(), build_iptc_app13(title, keywords))
        if rules.max_file_bytes is not None and len(content) > rules.max_file_bytes:
            warnings.append(f"Arquivo excede o limite configurado ({rules.max_file_bytes} bytes).")
        digest = hashlib.sha256(content).hexdigest()
        root = self.collection_path(asset["collection_id"])
        exports = root / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        stem = f"{asset['id'][:8]}-{digest[:12]}"
        destination = exports / f"{stem}.jpg"
        with destination.open("xb") as output_file:
            output_file.write(content)
        metadata = asset.get("metadata", {})
        # Registro do pacote no banco: a pasta exports fica só com o que vai para a Adobe.
        result = {
            "path": str(destination),
            "filename": destination.name,
            "width": width,
            "height": height,
            "megapixels": megapixels,
            "size_bytes": len(content),
            "sha256": digest,
            "warnings": warnings,
            "title": title,
            "keywords": keywords,
            "category_suggestion": metadata.get("category_suggestion", ""),
            "adobe_category": category_number(metadata.get("category_suggestion")),
            "source_sha256": asset["sha256"],
            "technical_rules_verified": rules.technical_rules_verified,
            "exported_at": utc_now(),
        }
        self.database.update_asset_export(asset["id"], result)
        return destination, result
