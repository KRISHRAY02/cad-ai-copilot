# AM Readiness Guide -- real-part validation log

`tests/test_am_guide.py` checks the orientation ranking, dedup, and
printability-warnings logic against known synthetic geometry (`MockAdapter`),
so the math is verified. It cannot tell you whether the tool's result
actually looks *right* on a real Fusion 360 part, since that depends on
real geometry this project doesn't control.

This log is for Krish to fill in by hand after running
`generate_am_readiness_guide()` against real Fusion 360 parts, to build
confidence (or find a real disagreement) between the tool's recommendation
and his own engineering judgement.

## How to fill this in

For each real part:
1. Open it in Fusion 360.
2. Before asking the copilot anything, decide what orientation **you**
   would print it in and write that down ("Expected best orientation
   (my judgement)") -- don't look at the tool's answer first.
3. Ask the copilot "help me set up this part for 3D printing" (or run
   `generate_am_readiness_guide()` directly) and record its actual
   `best_orientation`.
4. Mark "Match?" yes/no. If no, note *why* in a sentence or two -- e.g.
   did the tool miss that a face would actually rest flush on the bed
   (a documented limitation -- see `compare_build_orientations()`'s
   docstring), or did your own judgement miss something the overhang
   math caught?

## Results

| Part name | Expected best orientation (my judgement) | Tool's result | Match? (yes/no) | Notes |
|---|---|---|---|---|
| | | | | |
| | | | | |
| | | | | |

## Known limitations to keep in mind while filling this in

- The overhang analysis flags every downward-facing face past the 45°
  threshold, including a face that would actually sit flush on the print
  bed in that orientation -- it doesn't know which face (if any) touches
  the bed. A part whose "best" orientation the tool picks may still look
  odd if its real base happens to be a face the tool is also counting as
  "overhang area" for ranking purposes. This affects the *absolute*
  numbers shown more than the *ranking* (every orientation a given face
  appears flat-down in is penalized consistently), but is worth
  double-checking if a result looks surprising.
- `min_feature_size` and `trapped_volume` checks always report
  "not_checked" -- the tool doesn't look for thin walls/ribs or enclosed
  voids yet. Don't read a clean "Check before printing" section as
  covering those.
- Support-strategy volume is explicitly an estimate (see the guide's
  fixed footer) -- it's not a slicer-accurate material/time prediction.
