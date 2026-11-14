# WhiteBIT BTC and ETH displayed-liquidity study recovery

The registered observer sealed two `INVALID_MISSING_SHARD` exclusions during its November 14 01:42:46 UTC check. The feature shards arrived at 01:43:01 and 01:43:20 UTC, after the observer’s fixed capture grace. Both authenticated shards contained zero trades and zero book observations. On subsequent runs, the original verifier regenerated both exclusions as `INCOMPLETE_TWO_MARKET_FEATURE_SET` and aborted on the first immutable receipt mismatch. Seventeen comparison intervals remained unpublished.

The recovery tool verifies the original runner and registration, preserves both excluded receipts by their exact SHA-256, and verifies that each matching empty shard arrived after the original check. It delegates all other intervals to the original verifier and verifies any existing immutable score before publication. On Windows, it uses a same-directory rename that refuses to overwrite an existing target.

| Decision epoch | Existing receipt SHA-256 | Feature shard SHA-256 |
| --- | --- | --- |
| `1794613500` | `0618b6e624f76c9e334820270b3a45e2bd2710d0e29695f3bfa5ac4f1934ac46` | `b4a38551d48d91367c681e519d85da55069abb8bb97c19b73b0ca3ecc7cbcaea` |
| `1794617100` | `e97ab1e04daf61b21851e0a659d1c4e34a9993a996f4d8c2a47ec58532a621a8` | `939296799ebc9e945fb71272713f612b4be7f18e63b2a6bd1e1e7870d8982120` |

From the repository root, use local Python and point the tool at the campaign directory containing the registered runner, V3 protocol, registration, repair receipt and `receipts-v3`. The sibling `20261113-btc-eth-exact-retention` must contain its registered archive helpers, protocol, immutable shards and continuity snapshots. Publication requires write access to the campaign directory.

```powershell
python -B tools/recover_whitebit_displayed_liquidity.py --campaign-root 'D:/автотрейд/runtime/20261113-btc-eth-displayed-liquidity'
python -B tools/recover_whitebit_displayed_liquidity.py --campaign-root 'D:/автотрейд/runtime/20261113-btc-eth-displayed-liquidity' --publish
```

The first command is read-only. The second publishes the outstanding receipts and terminal score. After a successful isolated run, the same tool completed the original Windows campaign at 18:05:50 UTC. It preserved the first 13 receipts byte for byte and added 17. A read-only run verified the final score. The frozen gate rejected the study. Five of 30 comparisons had valid evidence, against a minimum of 20. The candidate recorded zero directional trades, against a minimum of eight, and returned 0%. The flow-only control made one modeled trade and returned −0.114103%. The publication made no model calls, network calls or orders.

Both publications produced `score-v3.json` SHA-256 `b0b46a32344152edef4acd6bcf53f62349503f5583b82c8b169421b07a0c5a1c`. The combined canonical receipts SHA-256 was `1542c9c2887c7504cd55899d8c6beb20e94b350628061b0c96ad6599eb7ffd61`.
