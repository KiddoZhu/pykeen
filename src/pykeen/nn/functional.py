"""Functional forms of interaction methods."""
from typing import Optional, Tuple

import torch
from torch import nn

from ..utils import is_cudnn_error, normalize_for_einsum, split_complex

__all__ = [
    "complex_interaction",
    "conve_interaction",
    "distmult_interaction",
]


def _normalize_terms_for_einsum(
    h: torch.FloatTensor,
    r: torch.FloatTensor,
    t: torch.FloatTensor,
) -> Tuple[torch.FloatTensor, str, torch.FloatTensor, str, torch.FloatTensor, str]:
    batch_size = max(h.shape[0], r.shape[0], t.shape[0])
    h_term, h = normalize_for_einsum(x=h, batch_size=batch_size, symbol='h')
    r_term, r = normalize_for_einsum(x=r, batch_size=batch_size, symbol='r')
    t_term, t = normalize_for_einsum(x=t, batch_size=batch_size, symbol='t')
    return h, h_term, r, r_term, t, t_term


def _add_cuda_warning(func):
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except RuntimeError as e:
            if not is_cudnn_error(e):
                raise e
            raise RuntimeError(
                '\nThis code crash might have been caused by a CUDA bug, see '
                'https://github.com/allenai/allennlp/issues/2888, '
                'which causes the code to crash during evaluation mode.\n'
                'To avoid this error, the batch size has to be reduced.',
            ) from e

    return wrapped


@_add_cuda_warning
def conve_interaction(
    h: torch.FloatTensor,
    r: torch.FloatTensor,
    t: torch.FloatTensor,
    t_bias: torch.FloatTensor,
    input_channels: int,
    embedding_height: int,
    embedding_width: int,
    num_in_features: int,
    embedding_dim: int,
    bn0: Optional[nn.BatchNorm1d],
    bn1: Optional[nn.BatchNorm1d],
    bn2: Optional[nn.BatchNorm1d],
    inp_drop: nn.Dropout,
    feature_map_drop: nn.Dropout2d,
    hidden_drop: nn.Dropout,
    conv1: nn.Conv2d,
    activation: nn.Module,
    fc: nn.Linear,
) -> torch.FloatTensor:
    # bind sizes
    batch_size = max(x.shape[0] for x in (h, r, t))
    num_heads = h.shape[1]
    num_relations = r.shape[1]
    num_tails = t.shape[1]

    # repeat if necessary
    h = h.unsqueeze(dim=2).repeat(1 if h.shape[0] == batch_size else batch_size, 1, num_relations, 1)
    r = r.unsqueeze(dim=1).repeat(1 if r.shape[0] == batch_size else batch_size, num_heads, 1, 1)

    # resize and concat head and relation, batch_size', num_input_channels, 2*height, width
    # with batch_size' = batch_size * num_heads * num_relations
    x = torch.cat([
        h.view(-1, input_channels, embedding_height, embedding_width),
        r.view(-1, input_channels, embedding_height, embedding_width),
    ], dim=2)

    # batch_size, num_input_channels, 2*height, width
    if bn0 is not None:
        x = bn0(x)

    # batch_size, num_input_channels, 2*height, width
    x = inp_drop(x)

    # (N,C_out,H_out,W_out)
    x = conv1(x)

    if bn1 is not None:
        x = bn1(x)

    x = activation(x)
    x = feature_map_drop(x)

    # batch_size', num_output_channels * (2 * height - kernel_height + 1) * (width - kernel_width + 1)
    x = x.view(-1, num_in_features)
    x = fc(x)
    x = hidden_drop(x)

    if bn2 is not None:
        x = bn2(x)
    x = activation(x)

    # reshape: (batch_size', embedding_dim)
    x = x.view(batch_size, num_heads, num_relations, 1, embedding_dim)

    # For efficient calculation, each of the convolved [h, r] rows has only to be multiplied with one t row
    # output_shape: (batch_size, num_heads, num_relations, num_tails)
    t = t.view(t.shape[0], 1, 1, num_tails, embedding_dim).transpose(-1, -2)
    x = (x @ t).squeeze(dim=-2)

    # add bias term
    x = x + t_bias.view(t.shape[0], 1, 1, num_tails)

    return x


def distmult_interaction(
    h: torch.FloatTensor,
    r: torch.FloatTensor,
    t: torch.FloatTensor,
) -> torch.FloatTensor:
    h, h_term, r, r_term, t, t_term = _normalize_terms_for_einsum(h, r, t)
    return torch.einsum(f'{h_term},{r_term},{t_term}->bhrt', h, r, t)


def complex_interaction(
    h: torch.FloatTensor,
    r: torch.FloatTensor,
    t: torch.FloatTensor,
) -> torch.FloatTensor:
    h, h_term, r, r_term, t, t_term = _normalize_terms_for_einsum(h, r, t)
    (h_re, h_im), (r_re, r_im), (t_re, t_im) = [split_complex(x=x) for x in (h, r, t)]
    return sum(
        torch.einsum(f'{h_term},{r_term},{t_term}->bhrt', hh, rr, tt)
        for hh, rr, tt in [
            (h_re, r_re, t_re),
            (h_re, r_im, t_im),
            (h_im, r_re, t_im),
            (h_im, r_im, t_re),
        ]
    )
