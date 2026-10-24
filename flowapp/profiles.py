from __future__ import annotations

from pathlib import Path

from agentkit.comfy import ProfileStore

from flowapp.schemas import ComfyWorkflowProfile

# Os arquivos continuam em <coleção>/workflows/<kind>.json e data/workflows/<kind>.json.
PROFILE_KINDS = ("generation", "upscale", "inpainting")


def _store(data_root: Path) -> ProfileStore:
    return ProfileStore(data_root / "workflows", model=ComfyWorkflowProfile)


def _data_root_for(collection_root: Path) -> Path:
    # data/collections/<id> -> data
    return collection_root.parent.parent


def profile_path(collection_root: Path, kind: str) -> Path:
    return ProfileStore.path(collection_root / "workflows", kind)


def default_profile_path(data_root: Path, kind: str) -> Path:
    """Workflow padrão usado por coleções que não importaram o seu próprio."""
    return ProfileStore.path(data_root / "workflows", kind)


def profile_source(collection_root: Path, kind: str) -> str | None:
    source = _store(_data_root_for(collection_root)).source(collection_root / "workflows", kind)
    return {"own": "collection", "default": "default"}.get(source or "")


def load_profile(
    collection_root: Path, kind: str, use_default: bool = True,
) -> ComfyWorkflowProfile | None:
    return _store(_data_root_for(collection_root)).load(
        collection_root / "workflows", kind, use_default
    )


def load_default_profile(data_root: Path, kind: str) -> ComfyWorkflowProfile | None:
    return _store(data_root).load_default(kind)


def save_profile(collection_root: Path, profile: ComfyWorkflowProfile) -> Path:
    return _store(_data_root_for(collection_root)).save(collection_root / "workflows", profile)


def save_default_profile(data_root: Path, profile: ComfyWorkflowProfile) -> Path:
    return _store(data_root).save_default(profile)
