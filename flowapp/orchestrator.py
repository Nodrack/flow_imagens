from __future__ import annotations

import hashlib
import io
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable

from agentkit import GpuCoordinator, RunStore
from agentkit.comfy import WorkflowInputRef
from PIL import Image, UnidentifiedImageError

from flowapp.agents import INSTRUCTION_VERSION, AgentError, LMStudioClient
from flowapp.comfy import ComfyError, ComfyUIClient
from flowapp.config import AppConfig
from flowapp.database import Database
from flowapp.images import ImageStore, ImageValidationError
from flowapp.profiles import load_profile
from flowapp.schemas import ComfyWorkflowProfile, ConceptPlan, WorkflowState


Progress = Callable[[str], None]
PROJECT = "flow_imagens"


class Orchestrator:
    def __init__(self, root: Path, config: AppConfig) -> None:
        self.root = root
        self.config = config
        self.data_root = root / "data"
        self.database = Database(self.data_root / "flow.sqlite3")
        self.images = ImageStore(self.data_root, self.database)
        # Banco central de execuções (lido pelo painel) e trava de GPU entre processos.
        self.runs = RunStore()
        self.gpu = GpuCoordinator(
            release={
                "llm": lambda: self.lmstudio.unload_all_models(),
                "comfy": lambda: self.comfy.free_memory(),
            },
            free_vram=config.free_vram_between_phases,
        )
        self.comfy = ComfyUIClient(
            config.comfy_base_url, config.request_timeout_seconds, gpu=self.gpu,
        )
        self.lmstudio = LMStudioClient(
            config.lmstudio_base_url,
            config.request_timeout_seconds,
            config.agent_timeout_seconds,
            config.model_load_timeout_seconds,
            gpu=self.gpu,
        )

    def close(self) -> None:
        self.comfy.close()
        self.lmstudio.close()

    def ensure_normal(self, collection_id: str) -> None:
        """Recusa operações com agentes/ComfyUI na coleção das imagens enviadas fora do fluxo."""
        collection = self.database.get_collection(collection_id)
        if collection is not None and collection.get("kind") == "external":
            raise ValueError(
                "Esta é a coleção das imagens enviadas fora do fluxo: os agentes e o ComfyUI não rodam nela."
            )

    def delete_collection(self, collection_id: str) -> None:
        try:
            normalized_id = str(uuid.UUID(collection_id))
        except ValueError as error:
            raise ValueError("Identificador de coleção inválido.") from error
        if normalized_id != collection_id:
            raise ValueError("Identificador de coleção inválido.")
        collection = self.database.get_collection(collection_id)
        if collection is None:
            raise KeyError("Coleção não encontrada.")
        if collection.get("kind") == "external":
            raise ValueError("A coleção das imagens enviadas fora do fluxo não pode ser removida.")
        if self.runs.list_runs(PROJECT, collection_id, limit=1, active_only=True):
            raise ValueError("Não é possível remover uma coleção com agentes em execução.")

        collections_root = (self.data_root / "collections").resolve()
        collection_path = collections_root / collection_id
        if collection_path.exists() and collection_path.resolve().parent != collections_root:
            raise ValueError("O caminho da coleção não está isolado na pasta local de coleções.")
        staged_path = collections_root / f".{collection_id}.deleting-{uuid.uuid4().hex}"
        moved = False
        if collection_path.exists():
            collection_path.rename(staged_path)
            moved = True
        try:
            self.database.delete_collection_records(collection_id)
        except Exception:
            if moved and staged_path.exists():
                staged_path.rename(collection_path)
            raise
        self.runs.delete_runs(PROJECT, collection_id)
        if moved:
            try:
                shutil.rmtree(staged_path)
            except OSError as error:
                raise OSError(
                    "Os registros foram removidos, mas não foi possível apagar a pasta temporária "
                    f"{staged_path}. Remova-a manualmente após fechar arquivos em uso."
                ) from error

    def _execute_job(self, job: dict, progress: Progress | None = None) -> list[dict]:
        payload = job["payload"]
        profile = ComfyWorkflowProfile.model_validate(payload["profile"])
        prompt_id = job["comfy_prompt_id"]
        started = time.perf_counter()
        if progress:
            progress(f"Conferindo fila/histórico do ComfyUI ({prompt_id[:8]}).")
        with self.gpu.use("comfy"):
            try:
                # Dentro do try: um workflow rejeitado no envio também marca o trabalho como falho,
                # em vez de deixá-lo "queued" para sempre.
                self.comfy.submit_or_reconcile(prompt_id, profile, payload["values"])
                self.database.update_job(job["id"], "running")
                images = self.comfy.wait_for_outputs(
                    prompt_id,
                    profile.output_node_ids,
                    self.config.generation_timeout_seconds,
                    self.config.poll_interval_seconds,
                )
                registered = []
                for image in images:
                    if progress:
                        progress(f"Recuperando imagem {image.get('filename', '')}.")
                    raw = self.comfy.fetch_output(image)
                    asset_id, asset = self.images._register(
                        payload["collection_id"],
                        payload["concept_id"],
                        payload["folder"],
                        payload["role"],
                        raw,
                        image.get("filename", "comfy-output.png"),
                    )
                    if payload["role"] in {"upscaled", "correction"} and asset.get("role") != payload["role"]:
                        raise ImageValidationError(
                            "A saída é idêntica a uma imagem existente; ela não foi registrada como nova versão."
                        )
                    registered.append({"id": asset_id, **asset})
                self.database.update_job(
                    job["id"], "completed", duration_ms=int((time.perf_counter() - started) * 1000)
                )
                return registered
            except ComfyError as error:
                current_job = self.database.get_job(job["id"])
                if not error.transient:
                    if current_job and current_job["status"] != "cancelled":
                        self.database.update_job(
                            job["id"], "failed", str(error),
                            int((time.perf_counter() - started) * 1000),
                        )
                raise
            except Exception as error:
                current_job = self.database.get_job(job["id"])
                if current_job and current_job["status"] != "cancelled":
                    self.database.update_job(
                        job["id"], "failed", str(error),
                        int((time.perf_counter() - started) * 1000),
                    )
                raise

    def generate_candidates(
        self,
        collection_id: str,
        concept: dict,
        amount: int,
        width: int,
        height: int,
        progress: Progress | None = None,
    ) -> list[dict]:
        self.ensure_normal(collection_id)
        if not 1 <= amount <= 8:
            raise ValueError("Gere entre 1 e 8 candidatos por lote.")
        if not 256 <= width <= 8192 or not 256 <= height <= 8192:
            raise ValueError("As dimensões devem estar entre 256 e 8192 pixels.")
        if concept["state"] != WorkflowState.PROMPTS_APPROVED.value and concept["state"] != WorkflowState.CANDIDATES_READY.value:
            raise ValueError("Aprove o prompt antes de gerar candidatos.")
        collection = self.database.get_collection(collection_id)
        root = self.images.ensure_collection_dirs(collection_id)
        profile = load_profile(root, "generation")
        if profile is None:
            raise ValueError("Importe e valide um workflow API de geração primeiro.")
        errors = self.comfy.validate_profile(profile)
        if errors:
            raise ValueError("Mapeamento do workflow inválido: " + " ".join(errors))
        batch_id = str(uuid.uuid4())
        jobs_to_create = []
        plan = ConceptPlan.model_validate(concept["data"])
        for index in range(amount):
            seed = secrets.randbelow(2**32)
            values = {
                "prompt": plan.prompt_en,
                "negative_prompt": plan.negative_prompt,
                "seed": seed,
                "width": width,
                "height": height,
                "filename_prefix": f"flow/{concept['id'][:8]}-{batch_id[:8]}-{index + 1}",
            }
            key = hashlib.sha256(f"{concept['id']}:{batch_id}:{index}".encode()).hexdigest()
            payload = {
                "collection_id": collection_id,
                "concept_id": concept["id"],
                "profile": profile.model_dump(mode="json"),
                "values": values,
                "role": "candidate",
                "folder": "originals",
            }
            jobs_to_create.append((key, payload))
        job_ids = self.database.create_job_batch(
            collection_id, concept["id"], "generation", jobs_to_create
        )
        results = []
        for job_id in job_ids:
            job = self.database.get_job(job_id)
            if job is None:
                raise RuntimeError("Não foi possível recuperar o trabalho criado.")
            if job["status"] == "cancelled":
                break
            try:
                results.extend(self._execute_job(job, progress))
            except ComfyError as error:
                current_concept = self.database.get_concept(concept["id"])
                if not error.transient and current_concept and current_concept["state"] == WorkflowState.GENERATING.value:
                    self.database.transition_concept(
                        concept["id"], WorkflowState.FAILED,
                        f"Falha de configuração/execução: {error}",
                    )
                raise
            current_concept = self.database.get_concept(concept["id"])
            if current_concept and current_concept["state"] == WorkflowState.CANCELLED.value:
                return results
        current_concept = self.database.get_concept(concept["id"])
        if current_concept and current_concept["state"] == WorkflowState.CANCELLED.value:
            return results
        self.database.transition_concept(
            concept["id"], WorkflowState.CANDIDATES_READY, "Candidatos gerados e salvos"
        )
        return results

    def resume_jobs(
        self, collection_id: str, progress: Progress | None = None,
        concept_id: str | None = None,
    ) -> list[dict]:
        self.ensure_normal(collection_id)
        results = []
        for job in self.database.list_recoverable_jobs(collection_id):
            if concept_id and job["concept_id"] != concept_id:
                continue
            if progress:
                progress(f"Retomando {job['kind']} {job['id'][:8]} com o mesmo ID ComfyUI.")
            results.extend(self._execute_job(job, progress))
        for concept in self.database.list_concepts(collection_id):
            if concept_id and concept["id"] != concept_id:
                continue
            concept_jobs = self.database.list_concept_jobs(concept["id"])
            if (
                concept["state"] == WorkflowState.GENERATING.value
                and concept_jobs
                and all(job["status"] == "completed" for job in concept_jobs)
                and self.database.list_assets(concept["id"])
            ):
                self.database.transition_concept(
                    concept["id"], WorkflowState.CANDIDATES_READY,
                    "Trabalho recuperado após reinício do aplicativo",
                )
            elif (
                concept["state"] == WorkflowState.UPSCALING.value
                and concept_jobs
                and all(job["status"] == "completed" for job in concept_jobs)
                and any(asset["role"] == "upscaled" for asset in self.database.list_assets(concept["id"]))
            ):
                self.database.transition_concept(
                    concept["id"], WorkflowState.FINAL_REVIEW,
                    "Upscale recuperado após reinício do aplicativo",
                )
        return results

    def _correction_instruction(
        self, collection_id: str, concept: dict, feedback: str, model_id: str,
        progress: Progress | None,
    ) -> str:
        """Ordem curta de edição para o workflow de correção; sem modelo de texto (ou se o
        agente falhar), envia o feedback como foi escrito."""
        if not model_id:
            return feedback
        plan = ConceptPlan.model_validate(concept["data"])
        try:
            result, duration, attempts = self.lmstudio.write_correction(
                model_id, plan, feedback, self.config.max_agent_attempts, progress,
            )
        except AgentError as error:
            self.database.record_agent_run(
                collection_id, concept["id"], "correction_writer", model_id, INSTRUCTION_VERSION,
                "failed", None, str(error), error.duration_ms, error.attempts,
            )
            if progress:
                progress(f"Instrução de edição não gerada ({error}); usando o texto original.")
            return feedback
        self.database.record_agent_run(
            collection_id, concept["id"], "correction_writer", model_id, INSTRUCTION_VERSION,
            "completed", {**result.model_dump(mode="json"), "feedback": feedback}, "",
            duration, attempts,
        )
        if progress:
            progress(f"Instrução de edição: {result.instruction_en}")
        return result.instruction_en

    def correct_candidate(
        self,
        collection_id: str,
        concept: dict,
        source_asset: dict,
        correction_prompt: str,
        progress: Progress | None = None,
        *,
        model_id: str = "",
    ) -> dict:
        self.ensure_normal(collection_id)
        if concept["state"] != WorkflowState.CANDIDATES_READY.value:
            raise ValueError("A correção só está disponível antes da seleção humana.")
        if source_asset["role"] not in {"candidate", "correction"} or source_asset["status"] != "candidate":
            raise ValueError("Escolha um candidato ainda não decidido.")
        correction_prompt = correction_prompt.strip()
        if len(correction_prompt) < 12:
            raise ValueError("Descreva uma correção específica com pelo menos 12 caracteres.")

        root_source_id = source_asset.get("metadata", {}).get(
            "root_source_asset_id", source_asset["id"]
        )
        previous_corrections = [
            asset for asset in self.database.list_assets(concept["id"])
            if asset.get("metadata", {}).get("root_source_asset_id") == root_source_id
        ]
        if len(previous_corrections) >= 2:
            raise ValueError("O limite de duas correções para este candidato foi atingido.")
        iteration = len(previous_corrections) + 1

        root = self.images.ensure_collection_dirs(collection_id)
        profile = load_profile(root, "inpainting")
        if profile is None:
            raise ValueError("Importe um workflow API de inpainting/correção primeiro.")
        issues = self.comfy.validate_profile(profile)
        if issues:
            raise ValueError("Mapeamento do workflow inválido: " + " ".join(issues))

        uploaded_path = Path(source_asset["path"])
        if not uploaded_path.is_file():
            raise ImageValidationError("A imagem escolhida para correção não existe mais.")
        edit_instruction = self._correction_instruction(
            collection_id, concept, correction_prompt, model_id, progress,
        )
        uploaded = self.comfy.upload_image(uploaded_path)
        values = {
            "image": uploaded,
            "prompt": edit_instruction,
            "negative_prompt": "",
            "seed": secrets.randbelow(2**32),
            "width": source_asset["width"],
            "height": source_asset["height"],
            "filename_prefix": f"flow/correction-{root_source_id[:8]}-{iteration}",
        }
        key = hashlib.sha256(
            f"correction:{root_source_id}:{iteration}:{edit_instruction}".encode()
        ).hexdigest()
        payload = {
            "collection_id": collection_id,
            "concept_id": concept["id"],
            "profile": profile.model_dump(mode="json"),
            "values": values,
            "role": "correction",
            "folder": "corrections",
        }
        job_ids = self.database.create_job_batch(
            collection_id, concept["id"], "correction", [(key, payload)]
        )
        job = self.database.get_job(job_ids[0])
        if job is None:
            raise RuntimeError("Não foi possível recuperar o trabalho de correção criado.")

        try:
            outputs = self._execute_job(job, progress)
            if not outputs:
                raise ImageValidationError("O workflow de correção não retornou uma nova imagem.")
            corrected = outputs[0]
            if corrected.get("role") != "correction":
                raise ImageValidationError(
                    "A saída é idêntica a uma imagem já registrada; ela não foi adicionada como correção."
                )
            correction_metadata = {
                "root_source_asset_id": root_source_id,
                "parent_asset_id": source_asset["id"],
                "correction_iteration": iteration,
                "correction_feedback": correction_prompt,
                "correction_instruction": edit_instruction,
            }
            self.database.update_asset_metadata(corrected["id"], correction_metadata)
            corrected["metadata"] = correction_metadata
            self.database.transition_concept(
                concept["id"], WorkflowState.CANDIDATES_READY,
                f"Correção {iteration}/2 gerada com aprovação humana",
            )
            return corrected
        except ComfyError as error:
            current = self.database.get_concept(concept["id"])
            if not error.transient and current and current["state"] == WorkflowState.GENERATING.value:
                # A falha da correção não invalida os candidatos existentes.
                self.database.transition_concept(
                    concept["id"], WorkflowState.CANDIDATES_READY, f"Falha na correção: {error}"
                )
            raise
        except Exception as error:
            current = self.database.get_concept(concept["id"])
            if current and current["state"] == WorkflowState.GENERATING.value:
                self.database.transition_concept(
                    concept["id"], WorkflowState.CANDIDATES_READY, f"Falha na correção: {error}"
                )
            raise

    def cancel_jobs(self, collection_id: str, concept_id: str) -> None:
        cancelled = False
        for job in self.database.list_recoverable_jobs(collection_id):
            if job["concept_id"] != concept_id:
                continue
            cancel_result = self.comfy.cancel(job["comfy_prompt_id"])
            if cancel_result == "requested" or (
                cancel_result == "not_found" and job["status"] == "queued"
            ):
                self.database.update_job(job["id"], "cancelled", "Cancelamento solicitado pelo usuário")
                cancelled = True
        concept = self.database.get_concept(concept_id)
        pending_jobs = [
            job for job in self.database.list_recoverable_jobs(collection_id)
            if job["concept_id"] == concept_id
        ]
        if cancelled and not pending_jobs and concept and concept["state"] in {
            WorkflowState.GENERATING.value, WorkflowState.UPSCALING.value,
        }:
            self.database.transition_concept(
                concept_id, WorkflowState.CANCELLED, "Cancelamento explícito pelo usuário"
            )

    def prepare_package(self, concept: dict) -> tuple[Path, dict]:
        """Exporta o JPEG final com metadados e registra o pacote como pronto para transferência."""
        if concept["state"] != WorkflowState.METADATA_READY.value:
            raise ValueError("Aprove os metadados antes de preparar o pacote.")
        upscaled = [asset for asset in self.database.list_assets(concept["id"]) if asset["role"] == "upscaled"]
        if not upscaled:
            raise ValueError("O conceito não tem imagem ampliada.")
        asset = upscaled[-1]
        metadata = asset.get("metadata") or {}
        if not metadata.get("title_en") or not metadata.get("keywords"):
            raise ValueError("O conceito não tem título e palavras-chave salvos.")
        output, report = self.images.export_jpeg(
            asset, metadata["title_en"], metadata["keywords"], self.config.export_rules,
        )
        self.database.transition_concept(concept["id"], WorkflowState.LOCALLY_APPROVED, "Aprovação local do pacote")
        self.database.transition_concept(
            concept["id"], WorkflowState.PREPARED_FOR_TRANSFER, "JPEG e metadados auxiliares preparados localmente",
        )
        return output, report

    def upscale_selected(
        self, collection_id: str, concept: dict, asset: dict, progress: Progress | None = None,
    ) -> dict:
        self.ensure_normal(collection_id)
        if concept["state"] != WorkflowState.SELECTED.value:
            raise ValueError("Selecione um candidato antes do upscale.")
        profile = load_profile(self.images.collection_path(collection_id), "upscale")
        if profile is None:
            raise ValueError("Importe e valide um workflow API de upscale primeiro.")
        if "multiplier" not in profile.mappings:
            # Resize em "scale by multiplier": o fator vem da configuração do app, não do valor
            # salvo no workflow importado (um 4× estoura a VRAM com SeedVR2 em GPUs de 8 GB).
            multiplier = next((
                (node_id, name) for node_id, node in profile.workflow.items()
                for name, value in node.get("inputs", {}).items()
                if name.rsplit(".", 1)[-1] == "multiplier" and isinstance(value, (int, float))
            ), None)
            if multiplier:
                profile = profile.model_copy(update={"mappings": {
                    **profile.mappings,
                    "multiplier": WorkflowInputRef(node_id=multiplier[0], input_name=multiplier[1]),
                }})
        errors = self.comfy.validate_profile(profile)
        if errors:
            raise ValueError("Mapeamento do workflow inválido: " + " ".join(errors))
        uploaded = self.comfy.upload_image(Path(asset["path"]))
        values = {
            "image": uploaded, "filename_prefix": f"flow/upscale-{concept['id'][:8]}",
            "multiplier": self.config.upscale_factor,
        }
        # Tamanho alvo proporcional à imagem escolhida (múltiplo de 8): evita que o resize do
        # workflow force o tamanho fixo do nó (ex.: 1920×1080) e corte imagens verticais.
        factor = self.config.upscale_factor
        values["width"] = max(8, round(asset["width"] * factor / 8) * 8)
        values["height"] = max(8, round(asset["height"] * factor / 8) * 8)
        key = hashlib.sha256(f"upscale:{asset['sha256']}:{uuid.uuid4()}".encode()).hexdigest()
        payload = {
            "collection_id": collection_id,
            "concept_id": concept["id"],
            "profile": profile.model_dump(mode="json"),
            "values": values,
            "role": "upscaled",
            "folder": "upscaled",
        }
        job_ids = self.database.create_job_batch(
            collection_id, concept["id"], "upscale", [(key, payload)]
        )
        job_id = job_ids[0]
        job = self.database.get_job(job_id)
        if job is None:
            raise RuntimeError("Não foi possível recuperar o trabalho de upscale.")
        try:
            outputs = self._execute_job(job, progress)
            if not outputs:
                raise ImageValidationError("O workflow de upscale não retornou imagem.")
        except Exception:
            # Falha definitiva (não está mais na fila do ComfyUI): libera a imagem selecionada
            # para nova tentativa em vez de deixar o conceito preso em UPSCALING.
            current_job = self.database.get_job(job_id)
            if current_job is None or current_job["status"] not in {"queued", "running"}:
                self.database.transition_concept(
                    concept["id"], WorkflowState.SELECTED, "Upscale falhou; seleção liberada para nova tentativa"
                )
            raise
        self.database.transition_concept(concept["id"], WorkflowState.FINAL_REVIEW, "Upscale salvo sem sobrescrever origem")
        return outputs[0]
