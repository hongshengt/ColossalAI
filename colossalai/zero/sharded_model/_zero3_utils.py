
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Tuple, Union

import torch
import torch.nn.functional as F


def get_gradient_predivide_factor(world_size: int) -> float:
    factor: int = 1
    while world_size % factor == 0 and world_size / factor > factor:
        factor *= 2
    return float(factor)


def get_shard(tensor: torch.Tensor, rank: int, world_size: int) -> Tuple[torch.Tensor, int]:
    """Return the local shard of a full tensor."""
    # Shard using torch.chunk to match all-gather/reduce-scatter.
    chunks = list(torch.flatten(tensor).chunk(world_size))
    while len(chunks) < world_size:
        chunks.append(chunks[0].new_empty(0))

    # Determine number of padding elements.
    num_to_pad = chunks[0].numel() - chunks[rank].numel()
    assert num_to_pad >= 0, num_to_pad

    shard = chunks[rank].clone()
    if num_to_pad > 0:
        shard = F.pad(shard, [0, num_to_pad])
    return shard, num_to_pad


def free_storage(data: torch.Tensor) -> None:
    """Free underlying storage of a Tensor."""
    if data.storage().size() > 0:
        # Since we're modifying the Tensor's Storage directly, make sure the Tensor
        # is the sole occupant of the Storage.
        assert data.storage_offset() == 0
        data.storage().resize_(0)


@torch.no_grad()
def alloc_storage(data: torch.Tensor, size: torch.Size) -> None:
    """Allocate storage for a tensor."""
    if data.storage().size() == size.numel():  # no need to reallocate
        return
    assert data.storage().size() == 0
    data.storage().resize_(size.numel())


def cast_trensor_to_fp16(tensor: torch.Tensor) -> torch.Tensor:
    if tensor.dtype is torch.float32:
        out = tensor.half()
        if tensor.is_leaf:
            out.requires_grad = tensor.requires_grad
        return out
    return tensor


def cast_trensor_to_fp32(tensor: torch.Tensor) -> torch.Tensor:
    if tensor.dtype is torch.float16:
        out = tensor.float()
        if tensor.is_leaf:
            out.requires_grad = tensor.requires_grad
        return out
    return tensor


def apply_to_tensors(x: Any, fn: Callable):
    if torch.is_tensor(x):
        return fn(x)
    elif isinstance(x, list):
        return [apply_to_tensors(t, fn) for t in x]
    elif isinstance(x, tuple):
        return tuple(apply_to_tensors(t, fn) for t in x)
    elif isinstance(x, dict):
        return {key: apply_to_tensors(val, fn) for key, val in x.items()}
    else:
        return x


def cast_float_arguments(fn: Callable, *args: Any, **kwargs: Any) -> Tuple[Any, Any]:
    return apply_to_tensors(args, fn), apply_to_tensors(kwargs, fn)


def chunk_and_pad(tensor: torch.Tensor, num_chunks: int) -> List[torch.Tensor]:
    """Chunk a given Tensor into num_chunks parts and add any necessary padding."""
    chunks = list(torch.flatten(tensor).chunk(num_chunks))
    # torch.chunk may return fewer than num_chunks chunks, pad accordingly.
    num_pad_for_partial_chunk = chunks[0].numel() - chunks[-1].numel()
    if num_pad_for_partial_chunk > 0:
        chunks[-1] = F.pad(chunks[-1], [0, num_pad_for_partial_chunk])
    if len(chunks) < num_chunks:
        chunks.extend([torch.zeros_like(chunks[0]) for _ in range(num_chunks - len(chunks))])
    return chunks


def assert_in_engine(cond: Any, s: Any) -> None:
    """Used in backward context to make sure error is printed."""
    if not cond:
        print(s)
        raise AssertionError


def replace_state_dict_prefix(
    state_dict: Union[Dict[str, torch.Tensor], "OrderedDict[str, torch.Tensor]"], old_prefix: str, new_prefix: str
) -> None:
    """
    Replace all keys that match a given old_prefix with a new_prefix (in-place).

    Usage::

        state_dict = {"layer.xyz": torch.tensor(1)}
        replace_state_dict_prefix(state_dict, "layer.", "module.layer.")
        assert state_dict == {"module.layer.xyz": torch.tensor(1)}
    """
    if old_prefix == new_prefix:
        raise ValueError("old_prefix and new_prefix must be distinct")
    for key in list(state_dict.keys()):
        if not key.startswith(old_prefix):
            continue
        new_key = new_prefix + key[len(old_prefix):]
        state_dict[new_key] = state_dict[key]
        del state_dict[key]