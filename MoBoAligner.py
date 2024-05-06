import math
from typing import List, Optional, Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

import monotonic_align
from layers import LinearNorm
from tensor_utils import (
    compute_max_length_diff,
    convert_geq_to_gt,
    geq_mask_on_text_dim,
    get_invalid_tri_mask,
    gt_pad_on_text_dim,
    reverse_and_pad_head_tail_on_alignment,
    shift_tensor,
)

# Define a very small logarithmic value to avoid division by zero or negative infinity in logarithmic calculations
LOG_EPS = -1000
# Calculate the natural logarithm of 2 and store it for repeated use to improve efficiency
LOG_2 = math.log(2.0)


class MoBoAligner(nn.Module):
    def __init__(self, text_channels, mel_channels, attention_dim, noise_scale=2.0):
        super(MoBoAligner, self).__init__()
        self.mel_layer = LinearNorm(
            mel_channels, attention_dim, bias=True, w_init_gain="tanh"
        )
        self.text_layer = LinearNorm(
            text_channels, attention_dim, bias=False, w_init_gain="tanh"
        )
        self.v = LinearNorm(
            attention_dim,
            1,
            bias=True,
            w_init_gain="sigmoid",
            weight_norm=True,
            init_weight_norm=math.sqrt(1 / attention_dim),
        )

        self.noise_scale = noise_scale

    def check_parameter_validity(self, text_mask, mel_mask, direction):
        if not (
            len(direction) >= 1
            and set(direction).issubset(set(["forward", "backward"]))
        ):
            raise ValueError("Direction must be a subset of 'forward' or 'backward'.")
        if not torch.all(text_mask.sum(1) < mel_mask.sum(1)):
            raise ValueError(
                "The dimension of text embeddings (I) is greater than or equal to the dimension of mel spectrogram embeddings (J), which is not allowed."
            )

    def compute_energy(self, text_embeddings, mel_embeddings):
        """
        Compute the energy matrix between text embeddings and mel embeddings.

        Args:
            text_embeddings (torch.Tensor): The text embeddings of shape (B, I, D_text).
            mel_embeddings (torch.Tensor): The mel spectrogram embeddings of shape (B, J, D_mel).

        Returns:
            torch.Tensor: The energy matrix of shape (B, I, J) which applied Gaussian noise.
        """
        processed_mel = self.mel_layer(mel_embeddings.unsqueeze(1))  # (B, 1, J, D_att)
        processed_text = self.text_layer(
            text_embeddings.unsqueeze(2)
        )  # (B, I, 1, D_att)
        energy = self.v(torch.tanh(processed_mel + processed_text))  # (B, I, J, 1)

        energy = energy.squeeze(-1)  # (B, I, J)

        noise = torch.randn_like(energy) * self.noise_scale
        energy = F.sigmoid(energy + noise)

        return energy

    def compute_reversed_energy_and_masks(self, energy, text_mask, mel_mask):
        """
        Compute the backward energy matrix and the corresponding text and mel masks.

        Args:
            energy (torch.Tensor): The energy matrix of shape (B, I, J).
            text_mask (torch.Tensor): The text mask of shape (B, I).
            mel_mask (torch.Tensor): The mel spectrogram mask of shape (B, J).

        Returns (tuple): A tuple containing:
            energy_backward (torch.Tensor): The backward energy matrix of shape (B, I-1, J-1).
            text_mask_backward (torch.Tensor): The backward text mask of shape (B, I-1).
            mel_mask_backward (torch.Tensor): The backward mel spectrogram mask of shape (B, J-1).
        """
        shifts_text_dim = compute_max_length_diff(text_mask)
        shifts_mel_dim = compute_max_length_diff(mel_mask)

        energy_backward = shift_tensor(
            energy.flip(1, 2).unsqueeze(-1),
            shifts_text_dim=-shifts_text_dim,
            shifts_mel_dim=-shifts_mel_dim,
        ).squeeze(-1)

        energy_backward = energy_backward[:, 1:, 1:]
        text_mask_backward = text_mask[:, 1:]
        mel_mask_backward = mel_mask[:, 1:]
        return energy_backward, text_mask_backward, mel_mask_backward

    def compute_log_cond_prob(self, energy, text_mask, mel_mask):
        """
        Compute the log conditional probability of the alignment in the specified direction.

        Args:
            energy (torch.Tensor): The energy matrix of shape (B, I, J) for forward, or (B, I-1, J-1) for backward.
            text_mask (torch.Tensor): The text mask of shape (B, I) for forward, or (B, I-1) for backward.
            mel_mask (torch.Tensor): The mel spectrogram mask of shape (B, J) for forward, or (B, J-1) for backward.

        Returns (tuple): A tuple containing:
            log_cond_prob (torch.Tensor): The log conditional probability tensor of shape (B, I, J, J) for forward, or (B, I-1, J-1, J-1) for backward.
            log_cond_prob_geq (torch.Tensor): The log cumulative conditional probability tensor of shape (B, I, J, J) for forward, or (B, I-1, J-1, J-1) for backward.
        """
        B, I = text_mask.shape
        _, J = mel_mask.shape

        energy_4D = energy.unsqueeze(-1).repeat(1, 1, 1, J)  # BIJK format
        tri_invalid = get_invalid_tri_mask(B, I, J, J, text_mask, mel_mask)
        energy_4D.masked_fill_(tri_invalid, LOG_EPS)
        log_cond_prob = energy_4D - torch.logsumexp(
            energy_4D, dim=2, keepdim=True
        )  # on the J dimension

        log_cond_prob_geq = torch.logcumsumexp(log_cond_prob.flip(2), dim=2).flip(2)
        log_cond_prob_geq.masked_fill_(tri_invalid, LOG_EPS)
        return log_cond_prob, log_cond_prob_geq

    def compute_forward_pass(self, log_cond_prob, text_mask, mel_mask):
        """
        Compute forward recursively in the log domain.

        Args:
            log_cond_prob (torch.Tensor): The log conditional probability tensor for forward of shape (B, I, J, J) for forward, or (B, I-1, J-1, J-1) for backward.
            text_mask (torch.Tensor): The text mask of shape (B, I) for forward, or (B, I-1) for backward.
            mel_mask (torch.Tensor): The mel spectrogram mask of shape (B, J) for forward, or (B, J-1) for backward.

        Returns:
            torch.Tensor: The forward tensor of shape (B, I+1, J+1) for forward, or (B, I, J) for backward.
        """
        B, I = text_mask.shape
        _, J = mel_mask.shape
        J_minus_I_max = (mel_mask.sum(1) - text_mask.sum(1)).max()

        B_ij = torch.full((B, I + 1, J + 1), -float("inf"), device=log_cond_prob.device)
        B_ij[:, 0, 0] = 0  # Initialize forward[0, 0] = 0
        for i in range(1, I + 1):
            B_ij[:, i, i : (J_minus_I_max + i + 2)] = torch.logsumexp(
                B_ij[:, i - 1, :-1].unsqueeze(1)
                + log_cond_prob[:, i - 1, (i - 1) : (J_minus_I_max + i + 1)],
                dim=2,  # sum at the K dimension
            )

        return B_ij

    def compute_interval_probability(self, prob, log_cond_prob_geq_or_gt):
        """
        Compute the log interval probability, which is the log of the probability P(B_{i-1} < j <= B_i), the sum of P(B_{i-1} < j <= B_i) over i is 1.

        Args:
            prob (torch.Tensor): The forward or backward tensor of shape (B, I, J) for forward, or (B, I, J-1) for backward.
            log_cond_prob_geq_or_gt (torch.Tensor): The log cumulative conditional probability tensor of shape (B, I, J, J) for forward, or (B, I, J-2, J-1) for backward.

        Returns:
            torch.Tensor: The log interval probability tensor of shape (B, I, J) for forward, or (B, I, J-2) for backward.
        """
        K = log_cond_prob_geq_or_gt.shape[2]
        x = prob.unsqueeze(-1).repeat(1, 1, 1, K) + log_cond_prob_geq_or_gt.transpose(
            2, 3
        )  # (B, I, K, J)
        x = torch.logsumexp(x, dim=2)
        return x

    def combine_alignments(self, log_boundary_forward, log_boundary_backward):
        """
        Combine the log probabilities from forward and backward boundary calculations.

        Args:
            log_boundary_forward (torch.Tensor): The log probabilities from the forward boundary calculation of shape (B, I, J).
            log_boundary_backward (torch.Tensor): The log probabilities from the backward boundary calculation of shape (B, I, J).

        Returns:
            torch.Tensor: The combined log probabilities of shape (B, I, J).
        """
        log_interval_probability = torch.logaddexp(
            log_boundary_forward - LOG_2, log_boundary_backward - LOG_2
        )
        return log_interval_probability

    @torch.no_grad()
    def compute_hard_alignment(self, log_probs, alignment_mask):
        """
        Compute the Viterbi path for the maximum alignment probabilities.

        This function uses `monotonic_align.maximum_path` to find the path with the maximum probabilities,
        subject to the constraints of the alignment mask.

        Args:
            log_probs (torch.Tensor): The log probabilities tensor of shape (B, I, J).
            alignment_mask (torch.Tensor): The alignment mask of shape (B, I, J).
        Returns:
            torch.Tensor: The tensor representing the hard alignment path of shape (B, I, J).
        """
        attn = monotonic_align.maximum_path(log_probs, alignment_mask)
        return attn

    def forward(
        self,
        text_embeddings: torch.FloatTensor,
        mel_embeddings: torch.FloatTensor,
        text_mask: torch.BoolTensor,
        mel_mask: torch.BoolTensor,
        direction: List[str],
        return_hard_alignment: bool = False,
    ) -> Tuple[
        Optional[torch.FloatTensor], Optional[torch.FloatTensor], torch.FloatTensor
    ]:
        """
        Compute the soft alignment and the expanded text embeddings.

        Args:
            text_embeddings (torch.FloatTensor): The text embeddings of shape (B, I, D_text).
            mel_embeddings (torch.FloatTensor): The mel spectrogram embeddings of shape (B, J, D_mel).
            text_mask (torch.BoolTensor): The text mask of shape (B, I).
            mel_mask (torch.BoolTensor): The mel spectrogram mask of shape (B, J).
            direction (List[str]): The direction of the alignment, a subset of ["forward", "backward"].
            return_hard_alignment (bool): Whether to return the hard alignment which obtained by Viterbi decoding.

        Returns:
            Tuple[Optional[torch.FloatTensor], Optional[torch.FloatTensor], torch.FloatTensor]:
                - soft_alignment (torch.FloatTensor): The soft alignment tensor of shape (B, I, J) in the log domain.
                - hard_alignment (torch.FloatTensor): The hard alignment tensor of shape (B, I, J).
                - expanded_text_embeddings (torch.FloatTensor): The expanded text embeddings of shape (B, J, D_text).
        """
        # Check length of text < length of mel and direction is either "forward" or "backward"
        self.check_parameter_validity(text_mask, mel_mask, direction)

        alignment_mask = text_mask.unsqueeze(-1) * mel_mask.unsqueeze(1)  # (B, I, J)

        # Compute the energy matrix and apply noise
        energy = self.compute_energy(text_embeddings, mel_embeddings)

        if "forward" in direction:
            # 1. Compute the log conditional probability P(B_i=j | B_{i-1}=k), P(B_i >= j | B_{i-1}=k) for forward
            log_cond_prob_forward, log_cond_prob_geq_forward = (
                self.compute_log_cond_prob(energy, text_mask, mel_mask)
            )
            log_cond_prob_geq_forward = geq_mask_on_text_dim(
                log_cond_prob_geq_forward, text_mask
            )

            # 2. Compute forward recursively in the log domain
            Bij_forward = self.compute_forward_pass(
                log_cond_prob_forward, text_mask, mel_mask
            )
            Bij_forward = Bij_forward[:, :-1, :-1]  # (B, I+1, J+1) -> (B, I, J)

            # 3. Compute the forward P(B_{i-1} < j <= B_i)
            log_boundary_forward = self.compute_interval_probability(
                Bij_forward, log_cond_prob_geq_forward
            )
            log_boundary_forward = log_boundary_forward.masked_fill(
                ~alignment_mask, LOG_EPS
            )

        if "backward" in direction:
            # 1.1 Compute the energy matrix for backward direction
            energy_backward, text_mask_backward, mel_mask_backward = (
                self.compute_reversed_energy_and_masks(energy, text_mask, mel_mask)
            )

            # 1.2 Compute the log conditional probability P(B_i=j | B_{i+1}=k), P(B_i < j | B_{i+1}=k) for backward
            log_cond_prob_backward, log_cond_prob_geq_backward = (
                self.compute_log_cond_prob(
                    energy_backward, text_mask_backward, mel_mask_backward
                )
            )
            log_cond_prob_gt_backward = convert_geq_to_gt(log_cond_prob_geq_backward)
            log_cond_prob_gt_backward = gt_pad_on_text_dim(
                log_cond_prob_gt_backward, text_mask, LOG_EPS
            )

            # 2. Compute backward recursively in the log domain
            Bij_backward = self.compute_forward_pass(
                log_cond_prob_backward, text_mask_backward, mel_mask_backward
            )
            Bij_backward = Bij_backward[:, :, :-1]  # (B, I, J) -> (B, I, J-1)

            # 3.1 Compute the backward P(B_{i-1} < j <= B_i)
            log_boundary_backward = self.compute_interval_probability(
                Bij_backward, log_cond_prob_gt_backward
            )

            # 3.2 reverse the text and mel direction of log_boundary_backward, and pad head and tail one-hot vector on mel dimension
            log_boundary_backward = reverse_and_pad_head_tail_on_alignment(
                log_boundary_backward, text_mask_backward, mel_mask_backward
            )
            log_boundary_backward = log_boundary_backward.masked_fill(
                ~alignment_mask, LOG_EPS
            )

        # Combine the forward and backward soft alignment
        if direction == ["forward"]:
            soft_alignment = log_boundary_forward
        elif direction == ["backward"]:
            soft_alignment = log_boundary_backward
        else:
            soft_alignment = self.combine_alignments(
                log_boundary_forward, log_boundary_backward
            )

        # Use soft_alignment to compute the expanded text_embeddings
        expanded_text_embeddings = torch.bmm(
            torch.exp(soft_alignment).transpose(1, 2), text_embeddings
        )
        expanded_text_embeddings = expanded_text_embeddings * mel_mask.unsqueeze(2)

        hard_alignment = None
        if return_hard_alignment:
            hard_alignment = self.compute_hard_alignment(soft_alignment, alignment_mask)

        return soft_alignment, hard_alignment, expanded_text_embeddings
