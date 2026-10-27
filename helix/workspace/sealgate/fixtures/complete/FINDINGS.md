# COLD REVIEW FINDINGS — fixture 01
Reviewer: Alice          date: 2026-09-23
Read against `PREREG.md` at the hash recorded in the stamp.

- [F1] The bar of 0.72 is asserted, not derived. Nothing in the seal says why 0.72 and not 0.68.
  DISPOSITION: ADDRESSED — the bar stays fixed at 0.72 and is checked, not derived, against the
  highest non-claim control, which is 0.5040 here; 0.72 clears it by more than the 0.10 margin.

- [F2] Band B2 could be satisfied by an artefact of the split rather than by the network.
  DISPOSITION: DECLINED: the split is stratified and fixed before the run, and its seed is
  recorded in the run token; B2 would need a different split to fire, which S1 forbids.

- [F3] No positive control: a rig that has never caught anything cannot report a null.
  DISPOSITION: ACCEPTED — planted_positive is in the controls block and ran.
