from copy import deepcopy

import pytest
import torch
import torch.nn as nn
from torch.nn.modules.conv import _ConvTransposeNd

from tensor_parallel import Sharded, TensorParallel


@pytest.mark.parametrize("emb_cls", [nn.Embedding, nn.EmbeddingBag])
@pytest.mark.parametrize("devices", [None, ("cpu",), ("cpu", "cpu"), ("cpu", "cpu", "cpu")])
def test_embeds_and_linear(emb_cls, devices):
    model = nn.Sequential(
        emb_cls(num_embeddings=1337, embedding_dim=64),
        nn.LayerNorm(64),
        nn.Linear(64, 128),
        nn.ReLU(),
        nn.Linear(128, 10),
    )

    inputs = torch.randint(1, 1000, size=(1, 10))
    ref_out = model(inputs)
    ref_out.norm().backward()

    model_tp = deepcopy(model)  # deepcopy to avoid accidental grad spillage and false positives
    model_tp = TensorParallel(model_tp, device_ids=devices)
    out_ours = model_tp(inputs)
    out_ours.norm().backward()
    torch.testing.assert_close(ref_out, out_ours, atol=1e-6, rtol=1e-05)
    our_grad = torch.cat([next(shard[0].parameters()).grad for shard in model_tp.module_shards], dim=1)
    torch.testing.assert_close(model[0].weight.grad, our_grad, atol=1e-6, rtol=1e-05)


@pytest.mark.parametrize("devices", [None, ("cpu",), ("cpu",) * 2, ("cpu",) * 3, ("cpu",) * 4])
@pytest.mark.parametrize("extra_options", [{}, {"padding": "same"}, {"stride": 2}, {"dilation": 2}, {"groups": 2}])
def test_convs(devices, extra_options):
    batchnorm_cls = (None, nn.BatchNorm1d, nn.BatchNorm2d, nn.BatchNorm3d)
    # ^-- note: batchnorms test that tensor_parallel handles buffers (non-parameter state tensors) correctly
    for Conv, nd in (
        (nn.Conv1d, 1),
        (nn.Conv2d, 2),
        (nn.Conv3d, 3),
        (nn.ConvTranspose1d, 1),
        (nn.ConvTranspose2d, 2),
        (nn.ConvTranspose3d, 3),
    ):
        if issubclass(Conv, _ConvTransposeNd) and "padding" in extra_options:
            continue  # unsupported by pytorch
        model = nn.Sequential(
            Conv(32, 64, kernel_size=(3,) * nd, **extra_options),
            batchnorm_cls[nd](64),
            nn.ReLU(),
            Conv(64, 32, kernel_size=(3,) * nd, **extra_options),
        )
        inputs1 = torch.randn(3, 32, *[10 for _ in range(nd)], requires_grad=True)
        inputs2 = inputs1.detach().clone().requires_grad_(True)
        ref_out = model(inputs1)
        ref_out.norm().backward()

        model_tp = deepcopy(model)  # deepcopy to avoid accidental grad spillage and false positives
        model_tp = TensorParallel(model_tp, device_ids=devices)
        out_ours = model_tp(inputs2)
        out_ours.norm().backward()
        torch.testing.assert_close(
            ref_out,
            out_ours,
            atol=1e-6,
            rtol=1e-3,
            msg=lambda msg: f"{msg}\n where Conv is {Conv} with extra_options {extra_options}",
        )
        torch.testing.assert_close(
            inputs1.grad,
            inputs2.grad,
            atol=1e-3,
            rtol=1e-05,
            msg=lambda msg: f"{msg}\n where Conv is {Conv} with extra_options {extra_options}",
        )


@pytest.mark.parametrize("emb_cls", [nn.Embedding, nn.EmbeddingBag])
@pytest.mark.parametrize("devices", [None, ("cpu",), ("cpu", "cpu"), ("cpu", "cpu", "cpu")])
def test_sharding(emb_cls, devices):
    model = nn.Sequential(
        emb_cls(num_embeddings=1337, embedding_dim=64),
        nn.LayerNorm(64),
        nn.Linear(64, 128),
        nn.ReLU(),
        nn.Linear(128, 10),
    )
    num_params_original = sum(p.numel() for p in model.parameters())

    inputs = torch.randint(1, 1000, size=(1, 10))
    ref_out = model(inputs)
    ref_out.norm().backward()

    model_tp = deepcopy(model)  # deepcopy to avoid accidental grad spillage and false positives
    model_tp = TensorParallel(model_tp, device_ids=devices)
    world_size = len(model_tp.module_shards)
    num_params_tp = sum(p.numel() for p in model_tp.parameters())
    model_tp = Sharded(model_tp)
    num_params_sharded = sum(p.numel() for p in model_tp.parameters())
    assert num_params_sharded < num_params_tp or world_size == 1

    padding_params = 0 if world_size < 3 else 4
    assert num_params_sharded == num_params_original + padding_params

    out_ours = model_tp(inputs)
    out_ours.norm().backward()
    torch.testing.assert_close(ref_out, out_ours, atol=1e-6, rtol=1e-05)
    our_grad = torch.cat([next(shard[0].parameters()).grad for shard in model_tp.module.module_shards], dim=1)
    torch.testing.assert_close(model[0].weight.grad, our_grad, atol=1e-6, rtol=1e-05)
