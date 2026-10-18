# DLinear source attribution

Unchanged source `models/DLinear.py` from https://github.com/cure-lab/LTSF-Linear
at commit `0c113668a3b88c4c4ee586b8c5ec3e539c4de5a6`.
Copyright 2022 DLinear Authors. Apache License 2.0, bundled in DLINEAR_LICENSE.
Upstream source git blob: `1cf739ab3de99497d0225610153a05c52609c760`.
Upstream LICENSE git blob: `cf304c730facb7c19bb4204e9606c3fa4dd68a1d`.
No upstream NOTICE file was present in the inspected root.

Only DLinear.py is copied; the separate Transformer/FEDformer/Pyraformer models
and their transitive code are not included. New AutoTrade wrapper, cost model,
causal splitting, tests and packet assembly are independently written files.

This is a small model architecture trained locally, NOT a downloaded pretrained
financial trader. No third-party trained weights are included. The current
24-input, 1-step, 1-channel shared model has 50 trainable parameters.
