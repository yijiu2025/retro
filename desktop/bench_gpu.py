#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量测当前后端（CPU / GPU）的速度。

由 build_package 产出的包内 python 运行：
  · 不设 RETRO_COMPUTE_FORCE        -> 用 site-packages 里的 CPU torch
  · RETRO_COMPUTE_FORCE=gpu         -> sitecustomize 前置 data/gpu-pkgs 的 cu121 torch
打印：torch 版本、CUDA 是否可用、设备名，以及两类代表性负载的耗时。
两类负载：
  1) 大矩阵乘法（Transformer/GNN 聚合里最重的算子）
  2) 小型 GNN 前向（torch_geometric GCN，贴近 RetroWeb 实际模型形态）
"""
import os
import time
import torch

DEVICE_HINT = os.environ.get("RETRO_COMPUTE_FORCE", "").strip().lower() or "auto"
print("=" * 60)
print(f"RETRO_COMPUTE_FORCE = {DEVICE_HINT}")
print(f"torch               = {torch.__version__}")
cuda = torch.cuda.is_available()
print(f"cuda.is_available() = {cuda}")
if cuda:
    print(f"device              = {torch.cuda.get_device_name(0)}")
dev = "cuda" if cuda else "cpu"
print("=" * 60)


def bench_matmul(iters: int = 30) -> float:
    dtype = torch.float32
    if dev == "cuda":
        a = torch.randn(5000, 5000, device="cuda", dtype=dtype)
        b = torch.randn(5000, 5000, device="cuda", dtype=dtype)
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        for _ in range(iters):
            _ = a @ b  # noqa: F841  # 基准循环：故意丢弃结果，只为计时
        torch.cuda.synchronize()
    else:
        a = torch.randn(5000, 5000, dtype=dtype)
        b = torch.randn(5000, 5000, dtype=dtype)
        t0 = time.perf_counter()
        for _ in range(iters):
            _ = a @ b  # noqa: F841  # 基准循环：故意丢弃结果，只为计时
    return time.perf_counter() - t0


def bench_gnn(iters: int = 30):
    try:
        from torch_geometric.nn import GCNConv
        from torch_geometric.data import Data
    except Exception as e:  # noqa: BLE001
        return None, f"torch_geometric 不可用: {e}"
    n_nodes, n_edges = 12000, 60000
    edge_index = torch.randint(0, n_nodes, (2, n_edges))
    x = torch.randn(n_nodes, 128)
    if dev == "cuda":
        edge_index, x = edge_index.cuda(), x.cuda()
    data = Data(x=x, edge_index=edge_index)
    conv1 = GCNConv(128, 128)
    conv2 = GCNConv(128, 64)
    if dev == "cuda":
        conv1, conv2 = conv1.cuda(), conv2.cuda()
    if dev == "cuda":
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        for _ in range(iters):
            h = conv1(data.x, data.edge_index).relu()
            h = conv2(h, data.edge_index)
        torch.cuda.synchronize()
    else:
        t0 = time.perf_counter()
        for _ in range(iters):
            h = conv1(data.x, data.edge_index).relu()
            h = conv2(h, data.edge_index)
    return time.perf_counter() - t0, ""


print("\n[负载 1] 大矩阵乘法 5000x5000 @ 5000x5000 x30 次")
t_mm = bench_matmul()
print(f"  {dev:4s} 耗时 = {t_mm*1000:.1f} ms  (单次 {t_mm/30*1000:.2f} ms)")

print("\n[负载 2] GNN 前向 GCN(128->128->64) 12000节点/60000边 x30 次")
r = bench_gnn()
if r[0] is None:
    print(f"  {r[1]}")
else:
    t_gnn = r[0]
    print(f"  {dev:4s} 耗时 = {t_gnn*1000:.1f} ms  (单次 {t_gnn/30*1000:.2f} ms)")

print("\nDONE")
