"""Multi-adapter batching reproduces unbatched execution (design §9.5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

torch = pytest.importorskip("torch", reason="needs the models group: uv sync --group models")

from neura_adapters.lora import (  # noqa: E402
    MultiLora,
    called_linear_layers,
    check_parity,
    random_adapters,
)


class SeqFirst(torch.nn.Module):  # type: ignore[misc, name-defined]  # torch is optional
    """A small model with both layouts: a batch-first input projection and a sequence-first
    block, like LeRobot's ACT."""

    def __init__(self) -> None:
        super().__init__()
        self.proj = torch.nn.Linear(6, 32)
        self.block = torch.nn.Linear(32, 32)
        self.attention = torch.nn.MultiheadAttention(32, 4)
        self.head = torch.nn.Linear(32, 5)

    def forward(self, x: Any) -> Any:
        tokens = self.proj(x).unsqueeze(0).repeat(4, 1, 1)  # (seq, batch, dim)
        tokens = torch.relu(self.block(tokens))
        tokens, _ = self.attention(tokens, tokens, tokens)
        return self.head(tokens.mean(0))


@pytest.fixture
def model() -> Any:
    torch.manual_seed(0)
    return SeqFirst().eval()


def test_mixed_adapters_in_one_batch_match_each_adapter_alone(model: Any) -> None:
    requests = torch.randn(5, 6)

    def probe(n: int) -> Any:
        return model(torch.randn(n, 6))

    targets = called_linear_layers(model, probe)
    assert "attention.out_proj" not in targets  # fused attention never calls it
    adapters = random_adapters(model, targets, count=4, rank=4, magnitude=0.3)
    lora = MultiLora(model, adapters, probe=probe)
    assert lora.batch_dims == {"proj": 0, "block": 1, "head": 0}

    def run(positions: Any) -> list[Any]:
        with torch.inference_mode():
            return list(model(requests[list(positions)]).numpy())

    report = check_parity(lora, run, tolerance=1e-5)
    assert report.passed, report
    # The adapters really change the outputs: otherwise parity would prove nothing.
    with lora.active([-1, 0]):
        base, adapted = run([0, 0])
    assert not np.allclose(base, adapted)


def test_adapters_on_layers_that_never_run_are_refused(model: Any) -> None:
    adapters = random_adapters(model, ["attention.out_proj"], count=1)
    with pytest.raises(ValueError, match="never run their forward"):
        MultiLora(model, adapters, probe=lambda n: model(torch.randn(n, 6)))


def test_ranks_may_differ_between_adapters(model: Any) -> None:
    def probe(n: int) -> Any:
        return model(torch.randn(n, 6))

    targets = called_linear_layers(model, probe)
    adapters = random_adapters(model, targets, count=1, rank=2) + random_adapters(
        model, targets, count=1, rank=8, seed=1
    )
    lora = MultiLora(model, adapters, probe=probe)
    requests = torch.randn(3, 6)

    def run(positions: Any) -> list[Any]:
        with torch.inference_mode():
            return list(model(requests[list(positions)]).numpy())

    assert check_parity(lora, run, tolerance=1e-5).passed


def test_multi_adapter_act_matches_unbatched(tmp_path: Path, binding_factory: Any) -> None:
    pytest.importorskip("lerobot")
    from neura_adapters import load_adapter
    from neura_adapters.base import blank_inputs
    from neura_adapters.synthetic import write_act_checkpoint
    from neura_runtime.policy import ModelInputs

    def small(a: dict[str, Any], r: dict[str, Any], p: dict[str, Any]) -> None:
        for model_input in a["spec"]["inputs"]:
            if model_input["kind"] == "image":
                model_input["width"], model_input["height"] = 128, 96
        for camera in r["spec"]["cameras"]:
            camera["width"], camera["height"] = 128, 96

    binding = binding_factory(edit=small)
    checkpoint = write_act_checkpoint(binding.artifact, tmp_path / "act", size="tiny")
    adapter: Any = load_adapter("lerobot-act", binding, checkpoint=checkpoint)
    blank = blank_inputs(binding)
    rng = np.random.default_rng(0)
    requests = [
        ModelInputs(
            state={
                k: rng.normal(0, 20, v.shape).astype(np.float32) for k, v in blank.state.items()
            },
            images={
                k: rng.integers(0, 255, v.shape, dtype=np.uint8) for k, v in blank.images.items()
            },
            instruction="",
            frozen_prefix=None,
        )
        for _ in range(4)
    ]
    network = adapter.policy.model

    def probe(n: int) -> Any:
        return adapter.infer([requests[0]] * n)

    targets = called_linear_layers(network, probe)
    assert targets  # ACT has adaptable layers: feed-forward blocks and projections
    lora = MultiLora(network, random_adapters(network, targets, count=3, rank=4), probe=probe)
    report = check_parity(lora, lambda positions: adapter.infer([requests[p] for p in positions]))
    assert report.passed, report
    with lora.active([-1, 0]):
        base, adapted = adapter.infer([requests[0], requests[0]])
    assert float(np.abs(base - adapted).max()) > 1e-3  # the adapters really change the actions
