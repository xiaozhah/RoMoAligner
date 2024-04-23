from MoBoAligner import MoBoAligner
import torch
import monotonic_align
import numpy as np

torch.autograd.set_detect_anomaly(True)

# Set a random seed to ensure reproducibility of the results
torch.manual_seed(1234)

I = 20
J = 40
# Initialize the text and mel embedding tensors
text_embeddings = torch.randn(
    2, I, 10, requires_grad=True
)  # Batch size: 2, Text tokens: 5, Embedding dimension: 10
mel_embeddings = torch.randn(
    2, J, 10, requires_grad=True
)  # Batch size: 2, Mel frames: 800, Embedding dimension: 10
# Initialize the text and mel masks
text_mask = torch.tensor(
    [[1] * I, [1] * 10 + [0] * 10], dtype=torch.bool
)  # Batch size: 2, Text tokens: 5
mel_mask = torch.tensor(
    [[1] * J, [1] * 20 + [0] * 20], dtype=torch.bool
)  # Batch size: 2, Mel frames: 800

temperature_ratio = 0.5  # Temperature ratio for Gumbel noise

# Initialize the MoBoAligner model
aligner = MoBoAligner()

log_delta, log_delta_mask, expanded_text_embeddings = aligner(
    text_embeddings, mel_embeddings, text_mask, mel_mask, temperature_ratio
)
# log_delta still in the log domain

# Print the shape of the soft alignment (log_delta) and the expanded text embeddings
print("Soft alignment (log_delta):")
print(log_delta.shape)
print("Expanded text embeddings:")
print(expanded_text_embeddings)

# Backward pass test

with torch.autograd.detect_anomaly():
    print(expanded_text_embeddings.mean())
    expanded_text_embeddings.mean().backward()

print("Gradient for text_embeddings:")
print(text_embeddings.grad)
print("Gradient for mel_embeddings:")
print(mel_embeddings.grad)

# Test the hard alignment (Viterbi decoding) function
with torch.no_grad():
    attn = monotonic_align.maximum_path(log_delta, log_delta_mask)
    print(attn.requires_grad)

print("Hard alignment (Viterbi decoding):")
print(attn.shape)
