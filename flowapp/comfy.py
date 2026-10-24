from __future__ import annotations

from collections.abc import Iterable

from agentkit.comfy import ComfyError, WorkflowProfile
from agentkit.comfy import ComfyUIClient as _BaseComfyUIClient

__all__ = ["ComfyError", "ComfyUIClient"]


class ComfyUIClient(_BaseComfyUIClient):
    """Cliente do agentkit com a regra do flow: geração sempre com seed mapeada."""

    def __init__(self, base_url: str, timeout: float = 20.0, **options) -> None:
        options.setdefault("client_id", "flow-imagens-local")
        super().__init__(base_url, timeout, **options)

    def validate_profile(
        self, profile: WorkflowProfile, required_roles: Iterable[str] = (),
        require_output: bool = True,
    ) -> list[str]:
        errors: list[str] = []
        if profile.kind == "generation" and "seed" not in profile.mappings:
            errors.append(
                "Mapeie uma entrada seed/noise_seed do workflow para que cada lote use a seed aleatória registrada."
            )
        if profile.kind in {"generation", "inpainting"}:
            missing = [role for role in ("width", "height") if role not in profile.mappings]
            if profile.kind == "inpainting" and "seed" not in profile.mappings:
                missing.append("seed")
            if missing:
                target = (
                    "a orientação da coleção" if profile.kind == "generation"
                    else "o tamanho da imagem original"
                )
                errors.append(
                    "Mapeie " + ", ".join(missing) + f" no workflow: sem width/height o ComfyUI usa o "
                    f"tamanho fixo do nó (ex.: 1024×1024) em vez de {target}."
                )
        if profile.kind == "upscale":
            fixed = [
                f"{node_id}.{name}" for node_id, node in profile.workflow.items()
                for name in node.get("inputs", {})
                if name.rsplit(".", 1)[-1] in {"width", "height"} and isinstance(node["inputs"][name], (int, float))
            ]
            mapped = {f"{ref.node_id}.{ref.input_name}" for ref in profile.mappings.values()}
            unmapped = [entry for entry in fixed if entry not in mapped]
            if unmapped:
                errors.append(
                    "Mapeie width/height no workflow de upscale (" + ", ".join(unmapped) + "): sem isso o "
                    "ComfyUI usa o tamanho fixo do nó e distorce/corta imagens de outra proporção."
                )
        return errors + super().validate_profile(profile, required_roles, require_output)
