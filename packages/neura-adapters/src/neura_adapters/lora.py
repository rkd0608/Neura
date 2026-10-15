"""Multi-adapter batching: one base forward pass serving requests for different LoRA adapters
(design §9.5).

Robots often run their own fine-tune of a shared base model. With LoRA fine-tunes, each adapted
layer computes ``W x + s * B (A x)`` where only ``A``, ``B`` and ``s`` differ per fine-tune. So one
batch can mix fine-tunes: the base ``W x`` is shared, and each request adds its own low-rank term.

This module adds that per-request term to a PyTorch model's linear layers with forward hooks. It
is used only where it reproduces unbatched execution within the declared tolerance:
`check_parity` compares every adapter's batched output with the same adapter merged into the
weights and run alone.

How the hooks find each request: each adapted layer's batch dimension is learned once, by a probe
forward pass with a distinctive batch size (LeRobot's ACT, for one, runs its transformer
sequence-first). The probe also records which layers actually run their `forward`: attention
blocks that use their projection weights directly bypass hooks, and adapters that target such a
layer are refused rather than silently ignored.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

PROBE_BATCH = 13  # a batch size unlikely to equal any other dimension of a robot policy's tensors


@dataclass(frozen=True)
class LoraLayer:
    """One adapted linear layer: ``delta = scale * B @ A`` with A (rank, in), B (out, rank)."""

    a: Any  # torch.Tensor
    b: Any  # torch.Tensor
    scale: float


LoraWeights = Mapping[str, LoraLayer]  # module name -> layer


class MultiLora:
    """Serve several LoRA adapters of one base model in one forward pass.

    ``adapters[k]`` maps module names to that adapter's layers. Request ``i`` of a batch uses the
    adapter ``indices[i]``; ``-1`` runs the base model unchanged.
    """

    def __init__(
        self,
        model: Any,
        adapters: Sequence[LoraWeights],
        *,
        probe: Callable[[int], Any],
    ) -> None:
        import torch

        if not adapters:
            raise ValueError("at least one adapter is needed")
        self.model = model
        self.adapters = list(adapters)
        targets = sorted(set().union(*(set(a) for a in adapters)))
        modules = dict(model.named_modules())
        missing = [name for name in targets if not isinstance(modules.get(name), torch.nn.Linear)]
        if missing:
            raise ValueError(f"adapters target modules that are not linear layers: {missing}")
        self.batch_dims = self._probe(targets, modules, probe)
        self._indices: Any = None
        self._stacks: dict[str, tuple[Any, Any, Any]] = {}
        for name in targets:
            module = modules[name]
            rank = max(a[name].a.shape[0] for a in adapters if name in a)
            n = len(adapters) + 1  # slot 0 is "no adapter": all zeros
            a_stack = torch.zeros(n, rank, module.in_features, dtype=module.weight.dtype)
            b_stack = torch.zeros(n, module.out_features, rank, dtype=module.weight.dtype)
            scales = torch.zeros(n, dtype=module.weight.dtype)
            for k, adapter in enumerate(adapters, start=1):
                layer = adapter.get(name)
                if layer is None:
                    continue
                r = layer.a.shape[0]
                # Zero-padding a smaller rank is exact: the extra rows and columns contribute 0.
                a_stack[k, :r] = layer.a
                b_stack[k, :, :r] = layer.b
                scales[k] = layer.scale
            device = module.weight.device
            self._stacks[name] = (a_stack.to(device), b_stack.to(device), scales.to(device))
            module.register_forward_hook(self._hook(name))

    @staticmethod
    def _probe(
        targets: Sequence[str], modules: Mapping[str, Any], probe: Callable[[int], Any]
    ) -> dict[str, int]:
        """Run the model once with PROBE_BATCH requests and record, for each target, which input
        dimension is the batch."""
        import torch

        seen: dict[str, tuple[int, ...]] = {}
        handles = []
        for name in targets:

            def record(module: Any, args: tuple[Any, ...], output: Any, name: str = name) -> None:
                seen[name] = tuple(args[0].shape)

            handles.append(modules[name].register_forward_hook(record))
        try:
            with torch.inference_mode():
                probe(PROBE_BATCH)
        finally:
            for handle in handles:
                handle.remove()
        dims: dict[str, int] = {}
        bypassed = [name for name in targets if name not in seen]
        if bypassed:
            raise ValueError(
                f"these layers never run their forward (fused attention?), so per-request "
                f"adapters cannot reach them: {bypassed}"
            )
        for name, shape in seen.items():
            matches = [d for d, size in enumerate(shape[:-1]) if size == PROBE_BATCH]
            if len(matches) != 1:
                raise ValueError(f"cannot tell the batch dimension of {name} from shape {shape}")
            dims[name] = matches[0]
        return dims

    def _hook(self, name: str) -> Callable[[Any, tuple[Any, ...], Any], Any]:
        def hook(module: Any, args: tuple[Any, ...], output: Any) -> Any:
            indices = self._indices
            if indices is None:
                return output
            a_stack, b_stack, scales = self._stacks[name]
            dim = self.batch_dims[name]
            x = args[0].movedim(dim, 0)  # (batch, ..., in)
            slots = indices + 1
            a = a_stack[slots]  # (batch, rank, in)
            b = b_stack[slots]  # (batch, out, rank)
            flat = x.reshape(x.shape[0], -1, x.shape[-1])  # (batch, tokens, in)
            low = flat @ a.transpose(1, 2)  # (batch, tokens, rank)
            delta = (low @ b.transpose(1, 2)) * scales[slots].view(-1, 1, 1)
            delta = delta.reshape(*x.shape[:-1], delta.shape[-1]).movedim(0, dim)
            return output + delta

        return hook

    @contextlib.contextmanager
    def active(self, indices: Sequence[int]) -> Iterator[None]:
        """Within this block, request ``i`` of each forward pass uses adapter ``indices[i]``."""
        import torch

        for index in indices:
            if not -1 <= index < len(self.adapters):
                raise ValueError(f"no adapter {index}")
        device = next(iter(self._stacks.values()))[0].device
        self._indices = torch.tensor(list(indices), dtype=torch.long, device=device)
        try:
            yield
        finally:
            self._indices = None

    @contextlib.contextmanager
    def merged(self, index: int) -> Iterator[None]:
        """Within this block, the model's weights include adapter ``index`` (the reference that
        batched execution must match)."""
        import torch

        modules = dict(self.model.named_modules())
        originals = {}
        with torch.no_grad():
            for name, layer in self.adapters[index].items():
                weight = modules[name].weight
                originals[name] = weight.detach().clone()
                weight += layer.scale * (layer.b @ layer.a).to(weight.dtype)
        try:
            yield
        finally:
            with torch.no_grad():
                for name, original in originals.items():
                    modules[name].weight.copy_(original)


@dataclass(frozen=True, slots=True)
class ParityReport:
    max_abs_error: float
    max_rel_error: float
    tolerance: float
    passed: bool


def check_parity(
    lora: MultiLora,
    run: Callable[[Sequence[int]], Sequence[Any]],
    *,
    tolerance: float = 1e-4,
) -> ParityReport:
    """Run one batch mixing every adapter and the base model, and compare each request's output
    with the same request run alone with its adapter merged into the weights.

    ``run(positions)`` runs a forward pass over fixed requests ``positions`` (indices into the
    caller's own list of len(adapters) + 1 requests) and returns one output per request. Request
    ``p`` uses adapter ``p - 1`` (request 0 uses the base model).
    """
    indices = [-1, *range(len(lora.adapters))]
    positions = list(range(len(indices)))
    with lora.active(indices):
        batched = [np.asarray(o, dtype=np.float64) for o in run(positions)]
    worst_abs = 0.0
    worst_rel = 0.0
    for position, index in zip(positions, indices, strict=True):
        if index == -1:
            alone = np.asarray(run([position])[0], dtype=np.float64)
        else:
            with lora.merged(index):
                alone = np.asarray(run([position])[0], dtype=np.float64)
        error = float(np.abs(batched[position] - alone).max())
        scale = max(float(np.abs(alone).max()), 1e-12)
        worst_abs = max(worst_abs, error)
        worst_rel = max(worst_rel, error / scale)
    return ParityReport(worst_abs, worst_rel, tolerance, worst_rel <= tolerance)


def random_adapters(
    model: Any,
    targets: Sequence[str],
    *,
    count: int,
    rank: int = 8,
    alpha: float = 16.0,
    seed: int = 0,
    magnitude: float = 0.05,
) -> list[dict[str, LoraLayer]]:
    """Synthetic LoRA adapters for benchmarks and tests (trained ones come from PEFT)."""
    import torch

    generator = torch.Generator().manual_seed(seed)
    modules = dict(model.named_modules())
    adapters = []
    for _ in range(count):
        adapter = {}
        for name in targets:
            module = modules[name]
            a = torch.randn(rank, module.in_features, generator=generator) * magnitude
            b = torch.randn(module.out_features, rank, generator=generator) * magnitude
            adapter[name] = LoraLayer(
                a.to(module.weight.dtype), b.to(module.weight.dtype), alpha / rank
            )
        adapters.append(adapter)
    return adapters


def called_linear_layers(model: Any, probe: Callable[[int], Any]) -> list[str]:
    """Names of the linear layers whose forward runs in a forward pass: what LoRA can target."""
    import torch

    called: set[str] = set()
    handles = []
    for name, module in model.named_modules():
        if isinstance(module, torch.nn.Linear):

            def record(*_: Any, name: str = name) -> None:
                called.add(name)

            handles.append(module.register_forward_hook(record))
    try:
        with torch.inference_mode():
            probe(2)
    finally:
        for handle in handles:
            handle.remove()
    return sorted(called)
