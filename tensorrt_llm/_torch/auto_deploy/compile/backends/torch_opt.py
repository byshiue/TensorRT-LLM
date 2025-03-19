"""Mixed backend with torch + Cudagraph."""

from typing import Any, Dict, Iterable, List, Optional, Tuple

import torch
import torch.nn as nn
from torch.cuda import CUDAGraph
from torch.fx import GraphModule
from torch.utils._pytree import TreeSpec, tree_flatten

from ...utils.cuda_graph import CudaGraphWarmUpPhase
from ...utils.logger import ad_logger
from ..compiler import BackendCompiler, BackendRegistry, _flatten_args


class CompiledGraph(nn.Module):
    def __init__(self, model: GraphModule, max_batch_size: int):
        super().__init__()
        self._in_spec: TreeSpec = model._in_spec
        self._out_spec: TreeSpec = model._out_spec
        self.gm_compiled = torch.compile(model, dynamic=True)
        self.max_batch_size = max_batch_size
        self.graphs: Dict[int, CUDAGraph] = {}
        self._input_ids_buffer: torch.Tensor = torch.empty(0, 1)
        self._out_buffer_flat: List[torch.Tensor] = None
        self._args_hash: Optional[Tuple[int, ...]] = None

    def _get_hash(self, flat_args: List[Any]) -> Tuple[int, ...]:
        return tuple(hash(a) for a in flat_args)

    @staticmethod
    def round_up_to_closest(batch_sizes: Iterable[int], bs: int) -> Optional[int]:
        """Return closest batch size larger or equal to bs."""
        return min(batch_sizes, key=lambda x: (x < bs, abs(x - bs)), default=None)

    def _capture_one_graph(self, *args, **kwargs) -> torch.cuda.CUDAGraph:
        """Capture and return one cuda graph."""
        # warm-up
        with CudaGraphWarmUpPhase():
            for _ in range(3):
                self.gm_compiled(*args, **kwargs)

        # capture graph now
        torch.cuda.synchronize()
        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph):
            # compute output
            out = self.gm_compiled(*args, **kwargs)
            # write out into output buffer up to out batch size
            out_flat, out_spec = tree_flatten(out)
            assert out_spec == self._out_spec, "Output spec mismatch."
            for o_buffer, o in zip(self._out_buffer_flat, out_flat):
                o_buffer[: o.shape[0]] = o
        torch.cuda.synchronize()

        return graph

    @staticmethod
    def _get_graph_batch_sizes(
        max_bs: int, extra: Optional[List[int]] = None, multiplier: int = 8
    ) -> List[int]:
        """Heuristic to set batch sizes for graph capture."""
        # do 1, max_bs, and extra as special batch sizes
        batch_sizes = {1, max_bs, *(extra or [])}

        # add all multiples of multiplier up to max_bs
        batch_sizes.update(range(multiplier, max_bs + 1, multiplier))

        # return as sorted list
        return sorted(batch_sizes)

    def _capture_cudagraph(self, idxs: torch.Tensor, flat_args: List[Any]):
        """Capture graph for single-token generation."""
        # set the args hash --> this is used to compare the inputs during graph replay
        self._args_hash = self._get_hash(flat_args)

        # set the input buffer to the max needed batch size with seq_len=1
        assert self.max_batch_size >= idxs.shape[0], "Max batch size too small."
        self._input_ids_buffer = idxs[:1, :1].repeat_interleave(self.max_batch_size, dim=0)

        # unflatten args, kwargs
        args, kwargs = self._in_spec.unflatten([self._input_ids_buffer] + flat_args)

        # capture output once with max batch size to capture output buffers
        with CudaGraphWarmUpPhase():
            out = self.gm_compiled(*args, **kwargs)
        self._out_buffer_flat, out_spec = tree_flatten(out)
        assert out_spec == self._out_spec, "Output spec mismatch."

        # capture graph now for a range of batch sizes
        for bs in self._get_graph_batch_sizes(self.max_batch_size):
            ad_logger.info(f"Capturing graph for batch size: {bs}")

            # setup args, kwargs and capture graph
            args, kwargs = self._in_spec.unflatten([self._input_ids_buffer[:bs], *flat_args])
            self.graphs[bs] = self._capture_one_graph(*args, **kwargs)

    def capture_graph(self, *args, **kwargs):
        """Capture and pre-fetch the graph."""
        idxs, flat_args = _flatten_args(self._in_spec, *args, **kwargs)
        self._capture_cudagraph(idxs, flat_args)

    def forward(self, *args, **kwargs) -> Any:
        """Run the compiled graph."""
        input_ids, flat_args = _flatten_args(self._in_spec, *args, **kwargs)
        bs, seq_len = input_ids.shape

        # regular forward for seq_len != 1, no available cudagraph for bs, or non-matching args
        if (
            seq_len != 1
            or max(self.graphs, default=0) < bs
            or self._args_hash != self._get_hash(flat_args)
        ):
            return self.gm_compiled(*args, **kwargs)

        # run forward pass via graph
        self._input_ids_buffer[:bs] = input_ids
        self.graphs[self.round_up_to_closest(self.graphs, bs)].replay()

        # retrieve output from buffer, cut to batch size, and unfatten
        out_flat = [o_b[:bs].detach().clone() for o_b in self._out_buffer_flat]
        return self._out_spec.unflatten(out_flat)


@BackendRegistry.register("torch-opt")
class TorchOptCompiler(BackendCompiler):
    def compile(self) -> CompiledGraph:
        compiled_gm = CompiledGraph(self.gm, max_batch_size=self.max_batch_size)

        # try capturing cudagraph
        if self.args is not None or self.kwargs is not None:
            compiled_gm.capture_graph(*self.args, **self.kwargs)

        return compiled_gm
