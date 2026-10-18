"""Evaluation and training jobs at the site (design §28.3, §28.4).

The control plane asks; the agent runs the job here, on the site's own data and hardware:

- the models are revisions of the signed manifest, verified as for prepare (digests, a trusted
  publisher), and their weights come from the site's artifact store, verified;
- recorded episodes are snapshotted into a dataset (so a job names exactly what it used), and
  hard-linked into the job's directory with the site's labels;
- the job runs ``neura-learn`` in the worker sandbox (no network, inputs read-only, one output
  directory), one job at a time, on a device no worker serves on: a job never slows a robot;
- an evaluation's result is its report; a training job's result is a new artifact, signed with
  the site's training key, with its lineage, whose weights go to the site's artifact store and
  never leave it;
- a shadow evaluation runs in the gateway instead (``neura_server.shadow``): for its duration
  the candidate sees the observations of the robots running the baseline, its actions compared
  with theirs and never sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from neura.v1 import site_pb2
from neura_agent import trust
from neura_agent.identity import atomic_write
from neura_contracts import ModelArtifact, ReasonCode, parse_document, signing
from neura_runtime.binding import ExecutionBinding
from neura_server.capture.store import EpisodeFilter, StoreError
from neura_server.sandbox import Invocation, launcher_for
from neura_server.shadow import ShadowSuite, comparable, shadow_verdict

if TYPE_CHECKING:
    from neura_agent.core import AgentCore

log = logging.getLogger("neura.agent.jobs")
PROGRESS_INTERVAL_S = 2.0
TRAINING_KEY = "training-key.pem"


class JobError(Exception):
    def __init__(self, code: ReasonCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class _Model:
    revision_id: str
    documents: dict[str, Any]
    binding: ExecutionBinding
    checkpoint: Path


def _version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unknown"


def weights_manifest(directory: Path) -> list[dict[str, Any]]:
    """``spec.weights`` for every file of a checkpoint directory."""
    entries = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        entries.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "digest": f"sha256:{digest.hexdigest()}",
                "size_bytes": path.stat().st_size,
            }
        )
    return entries


def link_tree(source: Path, target: Path) -> None:
    """Hard links where the filesystem allows them (episodes are read-only), copies otherwise."""
    target.mkdir(parents=True)
    for path in source.iterdir():
        try:
            os.link(path, target / path.name)
        except OSError:
            shutil.copy2(path, target / path.name)


class SiteJobs:
    def __init__(self, core: AgentCore) -> None:
        self.core = core
        self.settings = core.settings
        self.directory = core.settings.state_dir / "jobs"
        self._turn = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self.counters: dict[str, int] = {}
        # Candidates shadowing a served revision now: revision -> the gateway's shadow entry.
        self.shadows: dict[str, dict[str, Any]] = {}

    # --- commands -----------------------------------------------------------------------------

    def start_evaluation(
        self, command_id: str, operation_id: str, message: site_pb2.RunEvaluation
    ) -> None:
        self._start(
            command_id, operation_id, lambda: self._evaluate(command_id, operation_id, message)
        )

    def start_training(
        self, command_id: str, operation_id: str, message: site_pb2.RunTraining
    ) -> None:
        self._start(
            command_id, operation_id, lambda: self._train(command_id, operation_id, message)
        )

    def running(self) -> list[str]:
        return sorted(self._tasks)

    def local_status(self) -> dict[str, Any]:
        """What runs at this site for the control plane, for its operator (the local status
        API and ``neura-agent status``): never sent to the cloud."""
        return {
            "running": self.running(),
            "queued": max(0, len(self._tasks) - 1) if self._turn.locked() else 0,
            "shadows": [
                {"candidate": revision, "of": entry["of_revision_id"]}
                for revision, entry in sorted(self.shadows.items())
            ],
            "counters": dict(self.counters),
        }

    async def cancel(self, operation_id: str) -> bool:
        task = self._tasks.get(operation_id)
        if task is None:
            return False
        process = self._processes.get(operation_id)
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return True

    async def stop(self) -> None:
        for operation_id in list(self._tasks):
            await self.cancel(operation_id)

    def _start(
        self, command_id: str, operation_id: str, work: Callable[[], Awaitable[None]]
    ) -> None:
        async def run() -> None:
            if self._turn.locked():
                self.core._report(
                    command_id, operation_id, "Running", message="queued behind another job"
                )
            try:
                async with self._turn:
                    await work()
            except JobError as exc:
                self.counters["failed"] = self.counters.get("failed", 0) + 1
                self.core._report(
                    command_id, operation_id, "Failed", reason=exc.code, message=str(exc)
                )
            except asyncio.CancelledError:
                self.core._report(
                    command_id,
                    operation_id,
                    "Cancelled",
                    message="cancelled from the control plane",
                )
                raise
            except Exception as exc:
                log.exception("job %s failed", operation_id)
                self.counters["failed"] = self.counters.get("failed", 0) + 1
                self.core._report(
                    command_id,
                    operation_id,
                    "Failed",
                    reason=ReasonCode.AGENT_JOB_FAILED,
                    message=f"{type(exc).__name__}: {exc}"[:2000],
                )
            finally:
                self._tasks.pop(operation_id, None)
                self._processes.pop(operation_id, None)

        self._tasks[operation_id] = asyncio.create_task(run(), name=f"job-{operation_id}")

    # --- the jobs -------------------------------------------------------------------------------

    async def _evaluate(
        self, command_id: str, operation_id: str, message: site_pb2.RunEvaluation
    ) -> None:
        report = self.core._report
        report(command_id, operation_id, "Running", message="verifying the models")
        try:
            suite = json.loads(message.suite_json or "{}")
        except ValueError as exc:
            raise JobError(ReasonCode.EVALUATION_INVALID, f"the suite is not JSON: {exc}") from exc
        if suite.get("kind") not in ("simulation", "replay", "shadow"):
            raise JobError(
                ReasonCode.EVALUATION_INVALID, "the suite's kind is simulation, replay or shadow"
            )
        if suite["kind"] == "shadow":
            await self._shadow(command_id, operation_id, message, suite)
            return
        candidate = await self._model(message.candidate_revision_id)
        baseline = (
            await self._model(message.baseline_revision_id)
            if message.baseline_revision_id
            else None
        )
        if suite["kind"] == "simulation" and baseline is None:
            raise JobError(
                ReasonCode.EVALUATION_INVALID, "a simulation compares the candidate with a baseline"
            )
        work = self._workdir(message.evaluation_id or operation_id)
        spec: dict[str, Any] = {
            "kind": "evaluate",
            "device": self.device(),
            "candidate": {"documents": candidate.documents},
            "suite": {k: v for k, v in suite.items() if k != "filter"},
        }
        inputs = [("--candidate", candidate.checkpoint)]
        if baseline is not None:
            spec["baseline"] = {"documents": baseline.documents}
            inputs.append(("--baseline", baseline.checkpoint))
        dataset: dict[str, Any] | None = None
        if suite["kind"] == "replay":
            dataset = await self._episodes(
                work, f"evaluation {message.evaluation_id}", suite.get("filter") or {}
            )
            inputs.append(("--episodes", work / "episodes"))
        try:
            result = await self._run(
                command_id, operation_id, "evaluate", spec, inputs, work, candidate
            )
        finally:
            if dataset is not None:  # an evaluation does not keep its episodes from retention
                with contextlib.suppress(StoreError):
                    await asyncio.to_thread(
                        self.core.capture.store.release_dataset, dataset["dataset_id"]
                    )
        if dataset is not None:
            result["dataset_digest"] = dataset["digest"]
        self.counters["evaluations"] = self.counters.get("evaluations", 0) + 1
        self.core.audit.write(
            "evaluation.finished",
            evaluation=message.evaluation_id,
            candidate=candidate.revision_id,
            verdict=result.get("verdict"),
        )
        shutil.rmtree(work, ignore_errors=True)
        report(command_id, operation_id, "Succeeded", result={"evaluation": result})

    async def _train(
        self, command_id: str, operation_id: str, message: site_pb2.RunTraining
    ) -> None:
        report = self.core._report
        report(command_id, operation_id, "Running", message="verifying the base model")
        try:
            recipe = json.loads(message.recipe_json or "{}")
            selection = json.loads(message.dataset_filter_json or "{}")
        except ValueError as exc:
            raise JobError(ReasonCode.TRAINING_INVALID, f"not JSON: {exc}") from exc
        if not isinstance(recipe, dict) or not isinstance(selection, dict):
            raise JobError(ReasonCode.TRAINING_INVALID, "the recipe and filter are JSON objects")
        base = await self._model(message.base_revision_id)
        job_id = message.training_job_id or operation_id
        work = self._workdir(job_id)
        dataset = await self._episodes(work, f"training {job_id}", selection)
        spec = {
            "kind": "train",
            "device": self.device(),
            "base": {"documents": base.documents},
            "recipe": recipe,
        }
        inputs = [("--base", base.checkpoint), ("--episodes", work / "episodes")]
        result = await self._run(command_id, operation_id, "train", spec, inputs, work, base)
        report(command_id, operation_id, "Running", message="signing the new model")
        artifact, signature = await asyncio.to_thread(
            self._register,
            base,
            work / "out" / "checkpoint",
            name=message.artifact_name or base.binding.artifact.metadata.name,
            version=message.artifact_version or f"{base.binding.artifact.metadata.version}-ft",
            dataset=dataset,
            job_id=job_id,
            result=result,
        )
        shutil.rmtree(work, ignore_errors=True)
        self.counters["trainings"] = self.counters.get("trainings", 0) + 1
        digest = artifact.digest()
        profile = execution_profile_for(base, digest, artifact.metadata.version)
        self.core.audit.write(
            "training.finished", job=job_id, artifact=digest, dataset=dataset["digest"]
        )
        report(
            command_id,
            operation_id,
            "Succeeded",
            result={
                "training": {
                    k: v for k, v in result.items() if k in ("recipe", "dataset", "training")
                },
                "artifact": artifact.canonical_data(),
                "artifact_digest": digest,
                "artifact_signature": signature,
                "execution_profile": profile,
                "dataset_digest": dataset["digest"],
                "dataset_episodes": dataset["episodes"],
            },
        )

    async def _shadow(
        self,
        command_id: str,
        operation_id: str,
        message: site_pb2.RunEvaluation,
        suite_json: dict[str, Any],
    ) -> None:
        report = self.core._report
        try:
            suite = ShadowSuite.from_json(suite_json)
        except (TypeError, ValueError) as exc:
            raise JobError(ReasonCode.EVALUATION_INVALID, f"the shadow suite: {exc}") from exc
        if not message.baseline_revision_id:
            raise JobError(
                ReasonCode.EVALUATION_INVALID, "a shadow compares with a revision robots run"
            )
        # Only real workers load the candidate's weights (the simulated backend, for development,
        # serves a mock, as it does for robots); the baseline is served already.
        workers = self.settings.gateway_backend == "workers"
        candidate_id, baseline_id = message.candidate_revision_id, message.baseline_revision_id
        checkpoint: Path | None = None
        if workers:
            model = await self._model(candidate_id)
            documents, candidate_binding, checkpoint = (
                model.documents,
                model.binding,
                model.checkpoint,
            )
        else:
            documents, candidate_binding = self._verified(candidate_id)
        _, baseline_binding = self._verified(baseline_id)
        why = comparable(candidate_binding, baseline_binding)
        if why is not None:
            raise JobError(ReasonCode.EVALUATION_INVALID, why)
        served = {d["revision_id"]: d for d in self.core.gateway_config().get("deployments", [])}
        if baseline_id not in served:
            raise JobError(
                ReasonCode.EVALUATION_INVALID,
                f"no robot at this site runs {baseline_id}: prepare and activate it",
            )
        from neura_agent.gateway import write_revision_documents

        paths = write_revision_documents(
            self.core.gateway_dir,
            candidate_id,
            {role: {"document": doc} for role, doc in documents.items()},
        )
        entry: dict[str, Any] = {
            "revision_id": candidate_id,
            "of_revision_id": baseline_id,
            "artifact": str(paths["model_artifact"]),
            "robot_profile": str(paths["robot_profile"]),
            "execution_profile": str(paths["execution_profile"]),
            "every_nth": suite.every_nth,
        }
        if workers:
            entry["device"] = self.device()
            entry["checkpoint"] = str(checkpoint)
            adapter = served[baseline_id].get("adapter")
            if adapter is not None:  # a development override applies to both models
                entry["adapter"] = adapter
        self.shadows[candidate_id] = entry
        started = time.monotonic()
        stats: dict[str, Any] = {}
        try:
            await self.core.reconcile()
            report(command_id, operation_id, "Running", message="shadowing live observations")
            while time.monotonic() - started < suite.duration_s:
                await asyncio.sleep(min(PROGRESS_INTERVAL_S, suite.duration_s))
                status = await self.core.gateway.status() or {}
                stats = status.get("shadows", {}).get(candidate_id, stats)
                compared = int(stats.get("compared", 0))
                report(
                    command_id,
                    operation_id,
                    "Running",
                    message=f"{compared} live observations compared",
                )
                if suite.max_samples is not None and compared >= suite.max_samples:
                    break
        finally:
            self.shadows.pop(candidate_id, None)
            with contextlib.suppress(Exception):
                await self.core.reconcile()
        verdict, why = shadow_verdict(
            stats, min_samples=suite.min_samples, tolerance=suite.tolerance
        )
        out_of_range = stats.get("out_of_range", {})
        result = {
            "kind": "shadow",
            "suite": suite.to_json(),
            "verdict": verdict,
            "samples": int(stats.get("compared", 0)),
            "offered": int(stats.get("offered", 0)),
            "robots": int(stats.get("robots", 0)),
            "duration_s": round(time.monotonic() - started, 1),
            "divergence": stats.get("divergence", {}),
            "candidate": {
                "invalid_outputs": int(stats.get("invalid_outputs", 0)),
                "failed": int(stats.get("failed", 0)),
                "out_of_range": int(out_of_range.get("candidate", 0)),
            },
            "baseline": {"out_of_range": int(out_of_range.get("running", 0))},
            "skipped": dict(stats.get("skipped", {})),
        }
        self.counters["evaluations"] = self.counters.get("evaluations", 0) + 1
        self.core.audit.write(
            "evaluation.finished",
            evaluation=message.evaluation_id,
            candidate=candidate_id,
            verdict=verdict,
        )
        report(command_id, operation_id, "Succeeded", message=why, result={"evaluation": result})

    # --- pieces -----------------------------------------------------------------------------

    def _workdir(self, job_id: str) -> Path:
        safe = "".join(c for c in job_id if c.isalnum() or c in "_-")[:64] or "job"
        work = self.directory / safe
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        return work

    def _verified(self, revision_id: str) -> tuple[dict[str, Any], ExecutionBinding]:
        """A revision of the committed manifest, verified: its documents and their binding."""
        from neura_agent.core import RevisionError

        try:
            revision, binding = self.core.verified_revision(revision_id)
        except RevisionError as exc:
            raise JobError(exc.code, str(exc)) from exc
        documents = {
            role: revision["documents"][role]["document"]
            for role in ("model_artifact", "robot_profile", "execution_profile")
        }
        return documents, binding

    async def _model(self, revision_id: str) -> _Model:
        """A verified revision and its weights, checked out of the site's artifact store."""
        documents, binding = self._verified(revision_id)
        from neura_agent.artifacts import ArtifactError

        try:
            checkpoint = await asyncio.to_thread(self.core.artifacts.checkout, binding.artifact)
        except ArtifactError as exc:
            raise JobError(exc.code, str(exc)) from exc
        return _Model(revision_id, documents, binding, checkpoint)

    async def _episodes(self, work: Path, name: str, selection: dict[str, Any]) -> dict[str, Any]:
        """Snapshot the episodes ``selection`` picks; link them, with their labels, into the
        job's directory."""
        store = self.core.capture.store
        try:
            query = EpisodeFilter.from_json(selection)
            dataset: dict[str, Any] = await asyncio.to_thread(store.create_dataset, name, query)
        except StoreError as exc:
            raise JobError(ReasonCode(exc.code), str(exc)) from exc
        target = work / "episodes"
        target.mkdir()
        labels = {}
        for episode_id in dataset["episode_ids"]:
            record = await asyncio.to_thread(store.get, episode_id)
            await asyncio.to_thread(link_tree, store.path(episode_id), target / episode_id)
            labels[episode_id] = {
                "label": record["outcome"],
                "source": record["outcome_source"],
                **({"note": record["outcome_note"]} if record["outcome_note"] else {}),
            }
        (target / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
        return dataset

    def device(self) -> str:
        """Where jobs run: never on a GPU a worker serves on, unless the site allows it."""
        setting = self.settings.job_device
        if setting != "auto":
            return setting
        cuda = [f"cuda:{i}" for i in self.core.cuda_indexes()]
        if not cuda:
            return "cpu"
        state = self.core.state
        serving = {
            p["device"] for revision, p in state.placements.items() if revision in state.prepared
        }
        free = [d for d in cuda if d not in serving]
        if free:
            return free[0]
        if self.settings.job_share_gpus:
            return cuda[-1]
        raise JobError(
            ReasonCode.AGENT_JOB_DEVICE_BUSY,
            "every GPU this agent may use serves robots; free one, set NEURA_AGENT_JOB_DEVICE, "
            "or allow sharing (NEURA_AGENT_JOB_SHARE_GPUS)",
        )

    async def _run(
        self,
        command_id: str,
        operation_id: str,
        subcommand: str,
        spec: dict[str, Any],
        inputs: list[tuple[str, Path]],
        work: Path,
        model: _Model,
    ) -> dict[str, Any]:
        spec_path = work / "spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        out = work / "out"
        invocation = Invocation(
            subcommand=subcommand,
            device=spec["device"],
            inputs=[("--spec", spec_path), *inputs],
            outputs=[("--out", out)],
        )
        from neura_agent.gateway import sandbox_config

        sandbox = sandbox_config(self.settings).model_copy(
            update={"entrypoint": list(self.settings.job_entrypoint)}
        )
        launcher = launcher_for(
            sandbox, self.settings.job_command, model.binding.artifact.spec.runtime_image.reference
        )
        command = launcher.command(f"job-{work.name}", invocation)
        self.core._report(
            command_id, operation_id, "Running", message=f"{subcommand} on {spec['device']}"
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HF_HUB_OFFLINE": "1"},
        )
        self._processes[operation_id] = process
        failure = ""
        last = 0.0
        stderr_tail: list[str] = []

        async def errors() -> None:
            assert process.stderr is not None
            async for raw in process.stderr:
                stderr_tail.append(raw.decode(errors="replace").rstrip())
                del stderr_tail[:-40]

        reader = asyncio.create_task(errors())
        try:
            async with asyncio.timeout(self.settings.job_timeout_s):
                assert process.stdout is not None
                async for raw in process.stdout:
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue
                    if event.get("event") == "failed":
                        failure = str(event.get("error", ""))
                    elif (
                        event.get("event") == "progress"
                        and time.monotonic() - last > PROGRESS_INTERVAL_S
                    ):
                        last = time.monotonic()
                        self.core._report(
                            command_id,
                            operation_id,
                            "Running",
                            message=str(event.get("message", ""))[:500],
                        )
                code = await process.wait()
        except TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise JobError(
                ReasonCode.AGENT_JOB_TIMED_OUT,
                f"{subcommand} ran longer than {self.settings.job_timeout_s} s",
            ) from exc
        finally:
            await launcher.remove(f"job-{work.name}")
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        if code != 0:
            detail = failure or "\n".join(stderr_tail[-5:]) or f"exit status {code}"
            raise JobError(ReasonCode.AGENT_JOB_FAILED, f"{subcommand} failed: {detail}"[:2000])
        result: dict[str, Any] = json.loads((out / "report.json").read_text(encoding="utf-8"))
        return result

    def training_key(self) -> Ed25519PrivateKey:
        """The site's key for the models it trains, made on first use and trusted by this site
        (the operator sees it with ``neura-agent publishers`` and may distrust it)."""
        path = self.settings.state_dir / TRAINING_KEY
        if not path.exists():
            atomic_write(path, signing.generate_key(), 0o600)
            key = signing.load_private_key(path.read_bytes())
            key_id, _ = trust.trust(
                self.settings, signing.raw_public_key(key).hex(), "this site's training key"
            )
            self.core.audit.write("training_key.created", key_id=key_id)
            return key
        return signing.load_private_key(path.read_bytes())

    def _register(
        self,
        base: _Model,
        checkpoint: Path,
        *,
        name: str,
        version: str,
        dataset: dict[str, Any],
        job_id: str,
        result: dict[str, Any],
    ) -> tuple[ModelArtifact, dict[str, str]]:
        """The trained checkpoint as a new artifact: the base's contract with the new weights
        and its lineage, its files in the artifact store, signed with the site's key."""
        if not checkpoint.is_dir():
            raise JobError(ReasonCode.AGENT_JOB_FAILED, "the training job produced no checkpoint")
        data: dict[str, Any] = json.loads(json.dumps(base.binding.artifact.canonical_data()))
        site = self.core.identity.site_id
        data["metadata"] = {
            **data["metadata"],
            "name": name,
            "version": version,
            "description": f"Fine-tuned at site {site} from {base.revision_id}",
        }
        recipe = result.get("recipe", {})
        spec: dict[str, Any] = data["spec"]
        spec["weights"] = weights_manifest(checkpoint)
        spec["evidence"] = []  # the base's evidence is not this model's
        spec["lineage"] = {
            "base_artifact": base.binding.artifact_digest,
            "datasets": [
                {
                    "digest": dataset["digest"],
                    "episodes": dataset["episodes"],
                    "site_id": self.core.identity.site_id,
                }
            ],
            "training": {
                "recipe": {
                    k: v
                    for k, v in recipe.items()
                    if isinstance(v, str | int | float | bool) or v is None
                },
                "steps": int(result.get("training", {}).get("steps") or recipe.get("steps") or 1),
                "job_id": job_id[:64],
                "trainer": f"neura-learn {_version('neura-learn')}; lerobot {_version('lerobot')}",
            },
        }
        artifact = parse_document(data)
        assert isinstance(artifact, ModelArtifact)
        self.core.artifacts.import_directory(checkpoint)
        self.core.artifacts.checkout(artifact)  # its files are in the store, as declared
        signature = signing.sign(artifact.digest(), self.training_key())
        return artifact, signature


def execution_profile_for(base: _Model, artifact_digest: str, version: str) -> dict[str, Any]:
    """The base's execution profile, bound to the new artifact: the same timing, merge mode,
    freshness and mappings, so the fine-tuned model is served as its base was."""
    profile: dict[str, Any] = json.loads(json.dumps(base.documents["execution_profile"]))
    profile["spec"]["binding"]["model_artifact"] = artifact_digest
    profile["metadata"]["version"] = f"{profile['metadata'].get('version', '')}+{version}"[:63]
    parsed = parse_document(profile)  # raises if the base profile does not carry over
    return parsed.canonical_data()


def job_command() -> list[str]:
    return [sys.executable, "-m", "neura_learn"]
