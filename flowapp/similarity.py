"""CPU-only visual checks. Palette overlap is an advisory signal, not semantics."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from pydantic import BaseModel, Field


class SimilarityRules(BaseModel):
    max_phash_distance: int = Field(default=10, ge=0, le=63)
    compare_palette: bool = True
    min_palette_overlap: float = Field(default=0.8, ge=0, le=1)
    one_per_concept: bool = True
    thumbnail_size: int = Field(default=64, ge=16, le=256)
    palette_bins: int = Field(default=8, ge=2, le=16)


@lru_cache(maxsize=2048)
def _fingerprint(path: str, mtime_ns: int, size: int, thumbnail_size: int, bins: int):
    # Stat values are part of the cache key; no full-resolution image is retained.
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened)
        gray = np.asarray(image.convert("L").resize((32, 32), Image.Resampling.LANCZOS), dtype=float)
        rgb = np.asarray(image.convert("RGB").resize(
            (thumbnail_size, thumbnail_size), Image.Resampling.LANCZOS), dtype=float) / 255
    # Fixed 63-bit pHash: low 8x8 DCT frequencies, excluding DC luminance.
    basis = np.cos(np.pi * (2*np.arange(32)[None, :]+1) * np.arange(8)[:, None] / 64)
    frequencies = (basis @ gray @ basis.T).ravel()[1:]
    bits = frequencies > np.median(frequencies)
    phash = sum(int(bit) << index for index, bit in enumerate(bits))
    histogram = np.histogramdd(rgb.reshape(-1, 3), bins=(bins,)*3, range=((0, 1),)*3)[0].ravel()
    palette = np.sqrt(histogram / histogram.sum())
    return phash, palette


def fingerprint(path: str | Path, rules: SimilarityRules):
    path = Path(path).resolve()
    stat = path.stat()
    return _fingerprint(str(path), stat.st_mtime_ns, stat.st_size, rules.thumbnail_size, rules.palette_bins)


def compare_items(left: dict, right: dict, rules: SimilarityRules, *, enforce_concept=False) -> dict | None:
    if left["id"] == right["id"]:
        return None
    a, ac = fingerprint(left["path"], rules)
    b, bc = fingerprint(right["path"], rules)
    distance = (a ^ b).bit_count()
    overlap = float(np.clip(ac @ bc, 0, 1))
    reasons = []
    blocked = bool(enforce_concept and rules.one_per_concept and left.get("concept_id")
                   and left["concept_id"] == right.get("concept_id"))
    if blocked:
        reasons.append("Uma imagem por conceito: já existe outra imagem deste conceito no pacote ou histórico.")
    if distance <= rules.max_phash_distance:
        reasons.append(f"Hash perceptual próximo: distância {distance}/63.")
    if rules.compare_palette and overlap >= rules.min_palette_overlap:
        reasons.append(f"Paleta semelhante: sobreposição {overlap:.1%}. Revise assunto e composição.")
    if not reasons:
        return None
    return {"left": left, "right": right, "phash_distance": distance,
            "palette_overlap": overlap, "blocked": blocked, "reasons": reasons}


EXPORT_STATES = {"metadata_ready", "locally_approved", "prepared_for_transfer", "transferred"}


class SimilarityChecker:
    def __init__(self, database, data_root: Path, rules: SimilarityRules):
        self.database, self.data_root, self.rules = database, data_root, rules

    @staticmethod
    def asset_item(asset, title, source, record=None):
        package = asset.get("export") or {}
        exported = package.get("path")
        path = exported if exported and Path(exported).is_file() else asset["path"]
        return {"id": asset["id"], "concept_id": asset.get("concept_id"), "path": str(path),
                "title": title, "source": source, "record_id": (record or {}).get("id"),
                "moderation": (record or {}).get("status", "")}

    def collection_items(self, collection_id, stage):
        result = []
        for concept in self.database.list_concepts(collection_id):
            if stage == "candidates" and concept["state"] == "cancelled":
                continue
            assets = self.database.list_assets(concept["id"])
            if stage == "candidates":
                assets = [a for a in assets if a["role"] in {"candidate", "correction"}
                          and a.get("status") != "rejected"]
            else:
                if concept["state"] not in EXPORT_STATES:
                    continue
                assets = [a for a in assets if a["role"] == "upscaled"][-1:]
            for asset in assets:
                result.append(self.asset_item(asset, concept["data"]["concept"], stage))
        return result

    def history_items(self):
        items, missing = [], []
        attached = self.database.list_similarity_references()
        for record in self.database.list_moderation():
            assets = self.database.list_assets(record["concept_id"]) if record.get("concept_id") else []
            exported = [a for a in assets if a.get("export")]
            # Legacy records may predate the export log. Use their final upscale as a fallback.
            references = exported or [a for a in assets if a["role"] == "upscaled"][-1:]
            found = []
            for asset in references:
                item = self.asset_item(asset, record["title"], "history", record)
                if Path(item["path"]).is_file():
                    found.append(item)
            for reference in attached:
                if reference["moderation_record_id"] == record["id"] and Path(reference["path"]).is_file():
                    found.append({"id": f"reference:{reference['sha256']}", "concept_id": record.get("concept_id"),
                                  "path": reference["path"], "title": record["title"], "source": "history",
                                  "record_id": record["id"], "moderation": record["status"]})
            if found:
                items.extend(found)
            else:
                missing.append({"id": record["id"], "title": record["title"]})
        return items, missing

    def report(self, collection_id, stage="export", *, asset=None, title=None):
        subjects = self.collection_items(collection_id, stage)
        if asset is not None:
            # Include the caller's exact final, rather than trusting the screen's old selection.
            subjects = [s for s in subjects if s.get("concept_id") != asset.get("concept_id")]
            subjects.append(self.asset_item(asset, title or asset["id"], stage))
        history, missing = self.history_items()
        unreadable, valid_subjects, valid_history = [], [], []
        for source, target in ((subjects, valid_subjects), (history, valid_history)):
            for item in source:
                try:
                    fingerprint(item["path"], self.rules)
                    target.append(item)
                except (OSError, ValueError, Image.DecompressionBombError):
                    unreadable.append(item)
        matches = []
        for index, left in enumerate(valid_subjects):
            for right in valid_subjects[index+1:] + valid_history:
                if (stage == "candidates" and right["source"] == "history" and left.get("concept_id")
                        and left["concept_id"] == right.get("concept_id")):
                    # Old originals and their own submitted upscale are one production lineage.
                    continue
                # A package already in the history is not a new image of itself.
                match = compare_items(left, right, self.rules, enforce_concept=stage == "export")
                if match:
                    matches.append(match)
        return {"matches": matches, "missing_history": missing, "unreadable": unreadable,
                "compared_images": len(valid_subjects), "history_images": len(valid_history)}

    def attach_reference(self, record_id: str, raw: bytes):
        if record_id not in {r["id"] for r in self.database.list_moderation()}:
            raise ValueError("Registro de moderação não encontrado.")
        with Image.open(BytesIO(raw)) as image:
            image.verify()
        with Image.open(BytesIO(raw)) as image:
            image.load()
        digest = hashlib.sha256(raw).hexdigest()
        folder = self.data_root / "similarity" / "references"
        folder.mkdir(parents=True, exist_ok=True)
        # Do not interpolate user-controlled filenames or record IDs into paths.
        path = folder / digest
        if not path.exists():
            path.write_bytes(raw)
        self.database.add_similarity_reference(record_id, path, digest)
        return path


def matches_for_asset(report: dict, asset_id: str) -> list[dict]:
    return [m for m in report["matches"] if asset_id in {m["left"]["id"], m["right"]["id"]}]
