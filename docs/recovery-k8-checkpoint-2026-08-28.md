# K8 recovery checkpoint — 2026-08-28 05:35 Asia/Shanghai

The semantic pipeline task is incomplete and remains production-blocked.

## Last verified state

- K2 high-recall ContextPacket, K3 staged DeepSeek A/B/C analyzer, and K4 adapter are implemented.
- Full regression after K6: 466 tests passed.
- K5 development audit on 2026-08-25: key-context recall 46/46, distractor 1/100, zero-tolerance violations 0; DeepSeek pilot blocked because the largest packet was about 33,396 token proxy.
- K6 compact prototype passed synthetic contracts but failed real K7 validation.
- K7 blocked artifact: `data/private/gold_standard/2026-08-25/context_packet_compact_development_v1`.
- K7 failure: 20 roots expanded to 6,095 leaves; 4,870 pending; evidence recovery 0/32; compact index about 60.6 MB versus about 1.08 MB input; no provider call was made.
- K6/K7 artifacts must remain as failure baselines and must not be overwritten or connected to production.

## Next task: K8

Implement a new independent `src/wechat_bridge/linear_stage_packets.py`; do not keep patching the 3,256-line K6 module.

Frozen public API:

- `LinearStagePacketStore`
- `build_linear_stage_packets`
- `materialize_stage_a`
- `materialize_stage_b`
- `materialize_stage_c`
- `recover_linear_packet`

Required design:

- single global message/content/candidate/evidence tables;
- root packets contain ordered page references only;
- no message × candidate × evidence cross-product;
- Stage A receives minimal topic-mapping material;
- Stage B receives only topic-relevant messages and evidence;
- Stage C audits A/B and normally repeats no message body;
- system prompt plus user payload must be at most 2,000 token proxy, with a user-payload target of at most 1,600;
- linear page-count bound, lossless source/primary/adjacent/greeting/evidence recovery, body-free default serialization, cross-chat and time-only strong-relation guards.

Add an independent `tests/test_linear_stage_packets_contract.py`, then rerun the same 20 K5 roots on 2026-08-25 development. Do not call DeepSeek until real K8 capacity and recovery audits pass. Do not read frozen/frozen_test.

## Resource blocker

Both requested `gpt-5.6-luna` max K8 agents stopped because the Luna usage limit was reached. A partial, unverified `src/wechat_bridge/linear_stage_packets.py` is present; treat it as an interrupted write, inspect/compile it before continuing, and do not assume its public API or tests are complete. Resume the Luna agents after the stated reset time; the parent agent must continue to delegate implementation and only review.
