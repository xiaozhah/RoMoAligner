import numpy as np
import torch
import torch.nn.functional as F
from .robo_utils.core import float_to_int_duration_batch_c, generate_random_intervals_batch_c

def float_to_int_duration(dur, T, mask):  
    """ Cython optimised version of converting float duration to int duration.
    
    Args:
        dur (torch.Tensor): input float duration, shape (B, I)
        T (torch.Tensor): input int duration, shape (B,)
        mask (torch.Tensor): mask, shape (B, I)
    Returns:
        torch.LongTensor: output int duration, shape (B, I)
    """
    dur = dur * mask
    device = dur.device
    dur = dur.data.cpu().numpy().astype(np.float32)
    T = T.data.cpu().numpy().astype(np.int32)
    mask = mask.data.cpu().numpy().astype(np.int32)

    int_dur = np.zeros_like(dur).astype(dtype=np.int32)
    float_to_int_duration_batch_c(dur, T, mask, int_dur)
    return torch.from_numpy(int_dur).to(device=device, dtype=torch.long)

def generate_random_intervals(boundaries_batch, num_randoms):
    boundaries_batch = F.pad(boundaries_batch, (1, 0, 0, 0))
    
    device = boundaries_batch.device
    boundaries_batch = boundaries_batch.data.cpu().numpy().astype(np.int32)

    result_batch = np.zeros((boundaries_batch.shape[0], (boundaries_batch.shape[1] - 1) * (num_randoms + 1)), dtype=np.int32)
    generate_random_intervals_batch_c(boundaries_batch, result_batch, num_randoms)
    return torch.from_numpy(result_batch).to(device=device, dtype=torch.long)
