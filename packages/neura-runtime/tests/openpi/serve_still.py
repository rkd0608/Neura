"""A stand-in openpi policy behind openpi's own ``WebsocketPolicyServer``, for test_local.py.

It answers a LIBERO-style observation (``observation/state``, ``observation/image``,
``observation/wrist_image``, ``prompt``) with a chunk of zeros, and fails, as an openpi policy
does, once a file named ``fail`` appears in /work. Any other observation fails too: what the
robot sends is checked here, on the server's side.

    python serve_still.py CHUNK_LENGTH ACTION_DIM STATE_DIM
"""

import pathlib
import sys
from typing import Any

import numpy as np
from openpi_client import base_policy
from websocket_policy_server import WebsocketPolicyServer

CHUNK, ACTIONS, STATE = (int(a) for a in sys.argv[1:4])


class Still(base_policy.BasePolicy):  # type: ignore[misc]
    def infer(self, obs: dict[str, Any]) -> dict[str, Any]:
        if pathlib.Path("/work/fail").exists():
            raise RuntimeError("the model fell over")
        assert sorted(obs) == ["observation/image", "observation/state", "observation/wrist_image"]
        assert obs["observation/state"].shape == (STATE,), obs["observation/state"].shape
        for key in ("observation/image", "observation/wrist_image"):
            assert obs[key].shape == (480, 640, 3), key
            assert obs[key].dtype == np.uint8, key
        return {"actions": np.zeros((CHUNK, ACTIONS), dtype=np.float32)}


WebsocketPolicyServer(
    Still(), host="0.0.0.0", port=8000, metadata={"policy": "still"}
).serve_forever()
