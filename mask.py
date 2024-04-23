import torch
from roll import roll_tensor

def gen_i_range_mask(B, I, J, K, i_lens, j_lens):
    indices_i = torch.arange(I).unsqueeze(0).unsqueeze(-1).repeat(B, 1, J)
    indices_j = torch.arange(J).unsqueeze(0).unsqueeze(0).repeat(B, I, 1)
    indices = indices_i + indices_j

    limit_s = (i_lens - 1).unsqueeze(-1).unsqueeze(-1).expand(B, I, J)
    limit_e = j_lens.unsqueeze(-1).unsqueeze(-1).expand(B, I, J)

    mask_b = (indices >= limit_s).flip(1)
    mask_e = (indices < limit_e).flip(1)

    mask = (mask_b & mask_e).unsqueeze(-1)
    diff = i_lens - i_lens.max()
    mask = roll_tensor(mask, shifts=diff, dim=1)

    bool_tensor = i_lens.unsqueeze(1) > torch.arange(I)
    bool_tensor = bool_tensor[:, :, None, None].repeat(1, 1, J, 1)
    mask = mask * bool_tensor
    mask = mask.repeat(1, 1, 1, K)

    return mask

def gen_tri(B, I, J, K, direction):
    if direction == 'alpha':
        triu = torch.triu(torch.ones((K, J)), diagonal=0)
    else:
        triu = torch.tril(torch.ones((K, J)), diagonal=0)
    triu = triu.unsqueeze(-1).unsqueeze(0)  # (1, K, J, 1)
    triu = triu.repeat(B, 1, 1, I)  # (B, K, J, I)
    triu = triu.transpose(1, 3)  # (B, I, J, K)
    return triu.bool()

def gen_ijk_mask(text_mask, mel_mask, direction):
    ijk_mask = (
        text_mask.unsqueeze(2).unsqueeze(3)
        * mel_mask.unsqueeze(1).unsqueeze(3)
        * mel_mask.unsqueeze(1).unsqueeze(1)
    )
    if direction == "beta":  # because K is J-1
        ijk_mask = ijk_mask[:, :, :, :-1]
    return ijk_mask

def gen_ik_mask(text_mask, mel_mask, direction):
    _, J = mel_mask.shape
    ik_mask = (
        text_mask.unsqueeze(2).unsqueeze(3) * mel_mask.unsqueeze(1).unsqueeze(1)
    ).repeat(1, 1, J, 1)
    if direction == "beta":  # because K is J-1
        ik_mask = ik_mask[:, :, :, :-1]
    return ik_mask

def gen_most_i_mask(B, I, J, K):
    mask = torch.ones((B, I, J, K), dtype=torch.bool)
    mask[:, -1, :-1] = False
    return mask

def gen_upper_left_mask(B, I, J, K):
    tensor = torch.ones(B, I, J, K)
    for i in range(1, I):
        tensor[:, i, :i, :i] = 0
    return tensor

def phone_boundry_mask(I, J):
    left_mask = torch.tril(torch.ones(I, J), diagonal = -1).bool()
    right_mask = torch.triu(torch.ones(I, J), diagonal = J-I+1).bool()
    mask = left_mask | right_mask # True means position to mask
    return mask

if __name__ == "__main__":
    # 测试用例1
    B, I, J, K = 2, 5, 10, 10
    i_lens = torch.tensor([5, 2])
    j_lens = torch.tensor([10, 5])
    masked_tensor = gen_i_range_mask(B, I, J, K, i_lens, j_lens).int()
    print(masked_tensor.shape)