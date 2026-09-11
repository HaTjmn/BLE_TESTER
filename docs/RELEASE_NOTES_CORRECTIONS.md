# Release notes corrections

## BLE-D4.0U test-count wording (corrected during BLE-D4.0V)

BLE-D4.0U's own commit message (`57b647b`, immutable -- git history is
never rewritten in this project) states "72 new deterministic offline
tests." That figure is **not the count of newly-added tests** -- it was
the total number of tests run across both files touched by that commit,
most of which already existed before BLE-D4.0U.

Verified from git history (`git diff <BLE-D4.0T commit> <BLE-D4.0U commit>`)
and from each suite's own directly-observed `unittest` run output:

| Suite | Before BLE-D4.0U | After BLE-D4.0U | Newly added by BLE-D4.0U |
|---|---|---|---|
| `tools/test_diag_generation_monitor.py` | 33 (already existed, BLE-D4.0T) | 36 | **3** (the new `compute_expected_tliv_bytes` formula tests) |
| `tools/test_holter_diag_generate.py` | did not exist | 36 | **36** (the entire file is new) |
| `tools/test_ble_d4_0i_reconstruction.py` | 27 | 27 (byte-identical diff) | **0** |
| **Total** | 60 | **99** | **39** |

**Corrected statement:** BLE-D4.0U added 39 new tests (36 CLI + 3
size-formula) on top of 60 pre-existing tests, for a total of 99/99
passing. The "99/99" total itself was always correct; only the "72 new
tests" framing was misleading.

This note does not alter any raw test evidence, test file, or prior
commit -- it only corrects how the BLE-D4.0U test count is described in
project documentation going forward (see `BLE_VERIFICATION_REPORT.md`).
