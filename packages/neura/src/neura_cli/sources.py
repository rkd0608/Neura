"""What `neura policy push` and `neura check` accept, and what they make of it (design §29.3).

Three sources, one declaration model (``PolicyDeclaration``):

- **LeRobot**: a checkpoint directory (its ``config.json`` says what it reads and returns), or a
  Hugging Face Hub repository (``lerobot/act_...``) when ``huggingface_hub`` is installed.
- **Python**: a file with a ``@neura.policy`` class (``picker.py`` or ``picker.py:Picker``). The
  file's directory ships with the version: code and weights.
- **Container**: an image pinned by digest (``registry/acme/picker@sha256:...``) that answers
  Neura's policy interface, with a declaration file (``--declare policy.yaml``).

Each becomes the same thing: the files of a version (with their digests) and the model artifact
for the robot it is for. Neither the files nor the image ever pass through Neura.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from neura_cli.config import config_dir
from neura_cli.session import CliError
from neura_contracts import authoring
from neura_contracts.inference import (
    LEROBOT_ADAPTERS,
    InferenceError,
    PolicyDeclaration,
    declared_artifact,
    lerobot_declaration,
    read_lerobot_config,
)

IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
HUB_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
# Never shipped with a python version: caches, environments, version control.
SKIPPED_DIRECTORIES = {"__pycache__", "node_modules", ".git", ".venv", "venv", ".mypy_cache"}
MAX_PYTHON_FILES = 10_000

Kind = Literal["lerobot", "python", "container"]


@dataclass(frozen=True, slots=True)
class File:
    path: str  # where the version names it
    source: Path  # where it is on this machine
    digest: str
    size_bytes: int


@dataclass
class Source:
    kind: Kind
    reference: str
    declaration: PolicyDeclaration
    files: list[File] = field(default_factory=list)
    runtime_image: str | None = None
    code_license: dict[str, str] | None = None

    def weights(self) -> list[dict[str, Any]]:
        return [
            {"path": f.path, "digest": f.digest, "size_bytes": f.size_bytes} for f in self.files
        ]

    def version(self) -> str:
        """Names the artifact by its content, so pushing the same files again is the same
        version."""
        content = json.dumps([self.weights(), self.runtime_image], sort_keys=True)
        return "sha-" + hashlib.sha256(content.encode()).hexdigest()[:12]

    def artifact(self, robot: Mapping[str, Any], name: str) -> dict[str, Any]:
        try:
            return declared_artifact(
                self.declaration,
                robot,
                name=name,
                version=self.version(),
                weights=self.weights(),
                runtime_image=self.runtime_image,
                code_license=self.code_license,
            )
        except InferenceError as error:
            raise _unfit(error, robot) from error

    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)


def _unfit(error: InferenceError, robot: Mapping[str, Any]) -> CliError:
    name = robot.get("metadata", {}).get("name", "the robot")
    return CliError(f"This policy does not fit {name}.", *error.problems)


def digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}", path.stat().st_size


def _file(path: str, source: Path) -> File:
    digest, size = digest_file(source)
    return File(path, source, digest, size)


def _generated(name: str, document: Mapping[str, Any]) -> File:
    """A file the CLI writes (the entry file), kept under its digest in the CLI's directory."""
    data = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    digest = hashlib.sha256(data).hexdigest()
    directory = config_dir() / "generated"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}.json"
    if not target.exists():
        target.write_bytes(data)
    return File(name, target, f"sha256:{digest}", len(data))


def kind_of(reference: str) -> Kind:
    if IMAGE.match(reference):
        return "container"
    if Path(reference.split(":", 1)[0]).suffix == ".py":
        return "python"
    path = Path(reference)
    if path.is_dir():
        lerobot = (path / "config.json").is_file()
        return "python" if not lerobot and _decorated_files(path) else "lerobot"
    if HUB_REPO.match(reference):
        return "lerobot"
    raise CliError(
        f"{reference} is not a policy Neura can read.",
        "A LeRobot checkpoint directory (or Hub repository), a .py file with a @neura.policy "
        "class, or an image pinned by digest (name@sha256:...).",
    )


def load(
    reference: str,
    robot: Mapping[str, Any],
    *,
    declare: Path | None = None,
    image: str | None = None,
) -> Source:
    kind = kind_of(reference)
    if kind == "container":
        if image is not None:
            raise CliError("A container is its own image: --image is for the other sources.")
        return _container(reference, declare)
    if declare is not None:
        raise CliError("--declare is for container images; other sources declare themselves.")
    if image is not None and not IMAGE.match(image):
        raise CliError(f"{image} is not an image pinned by digest (name@sha256:<64 hex>).")
    source = _python(reference) if kind == "python" else _lerobot(reference, robot)
    source.runtime_image = image
    return source


# --- LeRobot -----------------------------------------------------------------------------------


def _lerobot(reference: str, robot: Mapping[str, Any]) -> Source:
    directory = Path(reference)
    if not directory.is_dir():
        directory = _from_hub(reference)
    try:
        config = read_lerobot_config(directory)
    except InferenceError as error:
        raise CliError(*error.problems) from error
    if config["type"] not in LEROBOT_ADAPTERS:
        raise CliError(
            f"LeRobot policy type {config['type']!r} is not supported yet.",
            "Supported: " + ", ".join(sorted(LEROBOT_ADAPTERS)) + ".",
            "Any other model runs as a @neura.policy class or a container.",
        )
    try:
        declaration = lerobot_declaration(config, robot)
    except InferenceError as error:
        raise _unfit(error, robot) from error
    files = [
        _file(path.relative_to(directory).as_posix(), path)
        for path in sorted(p for p in directory.rglob("*") if p.is_file() and not p.is_symlink())
        if not any(part.startswith(".") for part in path.relative_to(directory).parts)
    ]
    return Source("lerobot", reference, declaration, files)


def _from_hub(repository: str) -> Path:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise CliError(
            f"{repository} is not a directory here.",
            "To read checkpoints from the Hugging Face Hub: pip install huggingface_hub",
        ) from error
    try:
        return Path(snapshot_download(repository))
    except Exception as error:  # the Hub's errors are many and all mean the same here
        raise CliError(
            f"Could not download {repository} from the Hugging Face Hub.", str(error)
        ) from error


# --- Python ------------------------------------------------------------------------------------


def _decorated_files(directory: Path) -> list[Path]:
    return [
        p
        for p in sorted(directory.glob("*.py"))
        if re.search(r"@neura\.policy\b|@policy\(", p.read_text(encoding="utf-8", errors="ignore"))
    ]


def _python(reference: str) -> Source:
    target, _, wanted = reference.partition(":")
    path = Path(target)
    if path.is_dir():
        candidates = _decorated_files(path)
        if len(candidates) != 1:
            raise CliError(
                f"{path} has {len(candidates) or 'no'} files with a @neura.policy class.",
                "Name the file: neura policy push path/to/policy.py",
            )
        path = candidates[0]
    if not path.is_file():
        raise CliError(f"There is no file {path}.")
    root = path.parent.resolve()
    classes = _decorated_classes(path, root)
    if wanted:
        if wanted not in classes:
            raise CliError(
                f"{path} has no @neura.policy class {wanted}.",
                "It has " + (", ".join(sorted(classes)) or "none") + ".",
            )
        name = wanted
    elif len(classes) == 1:
        name = next(iter(classes))
    else:
        raise CliError(
            f"{path} has {len(classes) or 'no'} @neura.policy classes.",
            *(
                ["Name one: " + ", ".join(f"{path}:{c}" for c in sorted(classes))]
                if classes
                else ["Decorate the class that turns observations into actions: @neura.policy"]
            ),
        )
    authored = classes[name]
    if authored.weights is not None and not (root / authored.weights).exists():
        raise CliError(f"{name} names weights {authored.weights!r}, which is not in {root}.")
    files = list(_tree(root))
    entry = authoring.entry_document(
        kind="python",
        declaration=authored.declaration,
        entry=f"{path.stem}:{name}",
        weights=authored.weights,
    )
    files.append(_generated(authoring.ENTRY_FILE, entry))
    return Source(
        "python",
        f"{path}:{name}",
        authored.declaration,
        sorted(files, key=lambda f: f.path),
        code_license={"name": "Customer-owned"},
    )


def _decorated_classes(path: Path, root: Path) -> dict[str, authoring.Authored]:
    module_name = f"_neura_policy_{hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:8]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise CliError(f"Could not read {path} as Python.")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(root))
    try:
        spec.loader.exec_module(module)
    except authoring.AuthoringError as error:
        raise CliError(f"{path}: {error}") from error
    except Exception as error:
        raise CliError(f"Importing {path} failed.", f"{type(error).__name__}: {error}") from error
    finally:
        sys.path.remove(str(root))
    found = {}
    for value in vars(module).values():
        declared = authoring.authored(value)
        if declared is not None and getattr(value, "__module__", None) == module_name:
            found[value.__name__] = declared
    return found


def _tree(root: Path) -> Iterator[File]:
    count = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in SKIPPED_DIRECTORIES or part.startswith(".") for part in relative.parts):
            continue
        if path.name == authoring.ENTRY_FILE or path.suffix == ".pyc":
            continue
        if not path.is_file() or path.is_symlink():
            continue
        if (path.parent / "pyvenv.cfg").exists():
            continue
        count += 1
        if count > MAX_PYTHON_FILES:
            raise CliError(
                f"{root} has more than {MAX_PYTHON_FILES} files.",
                "Keep the policy's code and weights in their own directory.",
            )
        yield _file(relative.as_posix(), path)


# --- container ---------------------------------------------------------------------------------


ENTRY = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


def read_declaration(path: Path) -> tuple[PolicyDeclaration, str]:
    """A container's declaration file: what its policy reads and returns, and the class
    (``entry: package.module:Class``) installed in the image."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise CliError(f"Could not read {path}.", str(error)) from error
    if not isinstance(data, dict) or "chunk_length" not in data:
        raise CliError(
            f"{path} is not a declaration.",
            "It needs at least entry and chunk_length, and says what the policy reads: state, "
            "cameras, language.",
        )
    entry = data.pop("entry", None)
    if not isinstance(entry, str) or not ENTRY.match(entry):
        raise CliError(
            f"{path} names no entry.",
            "Name the @neura.policy class installed in the image: entry: package.module:Class",
        )
    unknown = set(data) - set(PolicyDeclaration.__dataclass_fields__) - {"adapter"}
    if unknown:
        raise CliError(f"{path} has unknown fields: {', '.join(sorted(unknown))}.")
    try:
        return PolicyDeclaration.from_json({**data, "adapter": "container"}), entry
    except (TypeError, ValueError, IndexError) as error:
        raise CliError(f"{path} is not a declaration.", str(error)) from error


def _container(image: str, declare: Path | None) -> Source:
    if declare is None:
        default = Path("neura.yaml")
        if not default.is_file():
            raise CliError(
                "A container declares what it reads and returns.",
                "Write it in a file (entry, chunk_length, cameras, state, language) and pass "
                "--declare FILE.",
            )
        declare = default
    declaration, class_name = read_declaration(declare)
    entry = authoring.entry_document(kind="container", declaration=declaration, entry=class_name)
    return Source(
        "container",
        image,
        declaration,
        [_generated(authoring.ENTRY_FILE, {**entry, "image": image})],
        runtime_image=image,
        code_license={"name": "Customer-owned"},
    )
