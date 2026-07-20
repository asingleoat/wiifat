# Multi-cell scale calibration: technical reference

Calibration of an $n$-cell platform scale (here $n=4$, Wii Balance Board)
using one precisely known small mass and unweighed auxiliary objects.

## 1. Problem statement

### 1.1 Sensor model

Each load cell $i$ reports

$$m_i = \max(g_i\,l_i + o_i,\ 0)$$

- $l_i \ge 0$: true incremental load on cell $i$ (above the empty-board
  state; the platform's own weight is absorbed into $o_i$),
- $g_i$: per-cell gain, nominally 1 when the device applies factory
  calibration upstream,
- $o_i$: per-cell zero offset, either sign,
- the $\max(\cdot,0)$ clamp models unsigned output: a cell with $o_i < 0$
  reads exactly 0 for all $l_i \le -o_i/g_i$. Readings in this region are
  censored, not noisy.

Displayed weight is $\sum_i$ of corrected cell values. Estimation targets:
all $g_i$ and $o_i$.

### 1.2 Why the clamp defeats taring

Taring subtracts the empty-scale reading, which equals
$\sum_i \max(o_i, 0)$ — the negative part of any offset is invisible when
empty. Under load, every cell is pushed into its linear region and the
full offset (including the negative part) enters the sum. A tared reading
is therefore biased by $\sum_i \min(o_i, 0)$, a constant that no taring
scheme can observe. On the development unit this bias was $-2.52$ kg.

### 1.3 Why per-cell isolation fails

The cells are mechanically coupled through the platform: a mass placed
"over" cell $i$ still distributes load across all cells (support points
sit inboard of the platform edges; the plate is stiff), and the
distribution is only partially controlled by placement. Any procedure
that assumes a placement loads one cell exclusively imports an
uncontrolled model error. The only exact constraint available for a
static capture of total mass $T$ is force balance:

$$\sum_i l_i = T.$$

(Moment constraints would require known placement positions; they are not
used.)

### 1.4 Available references

- One small mass $W$, known precisely (household kitchen/coffee scale;
  possibly $W < |o_i|$ for the clamped cell, so $W$ alone may not unclamp
  it anywhere on the platform).
- Heavy household objects (water jugs), masses unknown, heavy enough to
  push all cells above their clamps when centered.

## 2. Identifiability

### 2.1 Data structure

A capture is a stable time-averaged reading vector $(m_1,\dots,m_n)$
tagged with a total-mass expression. Totals are affine in the unknown
object masses: capture $p$ has total

$$T_p = \sum_b A_{pb} X_b + K_p$$

where $X_b$ are unknown object masses (fitted), $A_{pb} \in \{0,1,2,\dots\}$
counts how many of object $b$ are on the platform, and $K_p$ is the known
added mass (0 or $W$; $K_p$ known exactly). A fully known total has
$A_{p\cdot} = 0$.

Empty captures anchor $o_i$ directly for every cell with $m_i > 0$ when
empty. Cells clamped at empty have no such anchor.

### 2.2 Offset-difference cancellation

Two captures differing only by adding $W$ (same base object untouched,
all cells unclamped in both) subtract to

$$\sum_i \frac{\Delta m_i}{g_i} = W,$$

one linear equation in $1/g_i$ per addition, independent of all offsets.
Placing $W$ near each of the $n$ support points in turn yields an
$n \times n$ system with dominant diagonal; no isolation assumption is
required, only that the additions concentrate load differently.

### 2.3 Gauge freedom 1: common gain scale

If all loaded captures share a single known total $W$, the family

$$g_i' = s\,g_i,\qquad
  o_c' = o_c + g_c W (1-s),\qquad
  l_i' = l_i/s \ (i \ne c),\qquad
  l_c' = l_c/s + W(1 - 1/s)$$

(where $c$ is a cell with no empty anchor) reproduces every reading and
every sum constraint exactly, for any $s$. The fit is perfect and the
calibration extrapolates incorrectly (verified numerically: a fit
matching 10 kg placements to machine precision misread a 75 kg load by
+4.4 kg).

Breaking it requires two distinct known totals, or — sufficient and
cheaper — a known *difference*: captures at $X$ and $X + W$ with $X$
unknown but shared force $W = W/s$, hence $s = 1$. This is substitution
weighing; $W$ acts as a difference standard, so its smallness costs
precision, not identifiability.

### 2.4 Gauge freedom 2: offset–base slide

With $s$ fixed, a cell $c$ lacking an empty anchor still admits

$$o_c' = o_c + \delta,\qquad X_b' = X_b - \delta/g_c \ \ \forall b,$$

because $\delta$ enters once per capture (through cell $c$) and each
unknown base absorbs it. No collection of unknown-base captures
constrains $\delta$: differences cannot determine an additive constant.

Closure breaks it. For two unweighed objects $A$, $B$ weighed separately
and together, with per-weighing zero error $z$:

$$a = A + z,\quad b = B + z,\quad c = A + B + z
\;\Rightarrow\; z = a + b - c.$$

The zero error is counted once per weighing, object masses once per
object; three captures, three unknowns ($A$, $B$, $z$), exactly
determined. In the fit this appears as base-coefficient rows
$(1,0), (0,1), (1,1)$ over-determining the slide direction.

### 2.5 General criterion

For each cell lacking an empty anchor, collect the base-coefficient rows
$A_{p\cdot}$ of all usable captures in which that cell is unclamped. The
offset is identifiable iff

$$\mathrm{rank}([A \mid \mathbf{1}]) > \mathrm{rank}(A).$$

The all-ones column is the per-capture $\delta$; identifiability means it
is not expressible as a combination of object-mass columns. Fully known
totals contribute zero rows and satisfy the criterion trivially; any set
of captures of a single unknown object fails it. The implementation runs
this test as a guard and refuses to fit under-constrained sessions.

Additionally required: at least one nonzero known mass anywhere (absolute
scale), and the gain gauge broken per §2.3 (two distinct known totals, or
one base combination captured with two distinct $K_p$).

## 3. Procedure

1. **Empty capture.** Anchors $o_i$ for all unclamped-at-empty cells;
   identifies which cells are clamped.
2. **Heavy object 1 centered** (unweighed; base $X_1$). Must engage all
   cells; verified live, reprompted otherwise.
3. **$n$ corner additions.** For each support point: remove $W$, wait for
   return to the $X_1$ level, place $W$ near that corner, capture
   ($X_1 + W$). The between-placement level is monitored; if the base
   object shifted beyond tolerance, a fresh base id is started.
4. **Both heavy objects** ($X_1 + X_2$). All-engaged verification.
5. **Object 2 alone** ($X_2$). All-engaged verification; completes
   closure.
6. **Second empty capture.** Per-cell differences from step 1 quantify
   intra-session offset movement (warn threshold 0.10 kg/cell); both
   empty captures enter the fit as $l = 0$ anchors.

Requirements: $W$ any known mass (sub-kilogram accepted; see §5); each
heavy object must engage all cells alone when centered; the same objects
must remain unmodified across their captures.

## 4. Estimation

### 4.1 Variables and objective

Unknowns: $g_i$, $o_i$ ($i = 1..n$), base masses $X_b$, and latent loads
$l_{ip} \ge 0$ per usable capture. Usable = all cells unclamped
($m_i > 0\ \forall i$); captures containing any clamped cell are excluded
from the fit (censored values carry only inequality information, which is
not used). Objective: least squares over all usable readings,

$$\min \sum_{p,i} \big(m_{ip} - g_i l_{ip} - o_i\big)^2
\quad\text{s.t.}\quad \sum_i l_{ip} = T_p(X),\ \ l_{ip} \ge 0.$$

### 4.2 Alternating least squares

The problem is bilinear; each block has a closed form.

**Step L** (loads; per capture, given $g, o, X$): with
$r_i = m_{ip} - o_i$, minimizing subject to $\sum_i l_i = T_p$ gives

$$l_i = \frac{r_i}{g_i} - \frac{c}{g_i^2},\qquad
  c = \frac{\sum_j r_j/g_j - T_p}{\sum_j g_j^{-2}}.$$

Negativity handled by an active set: any $l_i < 0$ is fixed to 0 and the
constrained solve repeats over the remaining cells.

**Step X** (base masses; given $g, o$): each capture's unconstrained
total estimate is $\hat T_p = \sum_i (m_{ip} - o_i)/g_i$, and all
captures share the same residual weighting, so $X$ solves the ordinary
least-squares system

$$\min_X \sum_p \Big(\hat T_p - K_p - \sum_b A_{pb} X_b\Big)^2$$

via its normal equations (size = number of bases; 2–3 in practice).

**Step G** (per-cell line fit; given $l$): ordinary least squares of
$m_i$ on $l_i$ over usable captures plus $(0, m_i)$ points from empty
captures where cell $i$ is unclamped, with a tie-break ridge on the
slope:

$$g_i = \frac{\sum (l - \bar l)(m - \bar m) + \lambda}
             {\sum (l - \bar l)^2 + \lambda},\qquad
  o_i = \bar m - g_i \bar l,\qquad \lambda = 10^{-9}.$$

$\lambda$ must be negligible: on low-noise data the residual term
approaches zero, so any material $\lambda$ dominates the objective and
biases $g$ toward 1 with a compensating offset error (observed with
$\lambda = 1$: $o_c$ misestimated by 0.10 kg on clean synthetic data).
$\lambda$ exists only to select a solution for degenerate placement sets.

**Iteration**: L → X → G until max parameter change $< 10^{-7}$, cap
20 000 iterations. Convergence is linear; the reference fixtures converge
in $\sim 500$–12 000 iterations, each costing a few hundred float
operations.

### 4.3 Applying the calibration

Per frame, per cell:

$$\hat l_i = \begin{cases} 0 & m_i = 0 \\ (m_i - o_i)/g_i & m_i > 0.
\end{cases}$$

The $m_i = 0$ rule is exact at idle ($l = 0$) and irrelevant under an
occupant (all cells engaged); it is inexact only inside the dead zone
$0 < l_i \le -o_i/g_i$, which occurs transiently during load changes.
Corrected total $\sum_i \hat l_i$ feeds a downstream dynamic tare
(rolling idle median), which then absorbs post-calibration movement of
the anchorable cells; movement of a clamped-at-empty cell's offset
remains unobservable at idle and is bounded only by the session drift
check (§3 step 6).

## 5. Error propagation

With per-capture noise $\sigma$ (stable-window mean; tens of grams
here):

- **Gains**: each corner addition resolves a $W$-sized step, so relative
  gain error per addition $\approx \sigma\sqrt{2}/W$, averaged down by
  the number of additions. Relative error of $W$ itself propagates
  $\approx$ 1:1 into relative error at operating load. Heavier $W$
  improves both; this is the only cost of a small standard.
- **Zero point**: closure over three captures gives
  $\sigma_z \approx \sigma\sqrt{3}$, independent of any absolute
  standard.
- **Absolute error of an anchor mass** (when a fully known total is used
  instead of/alongside closure) propagates 1:1 in grams to the zero.
- **Offset movement over time** is not corrected between calibrations
  for clamped-at-empty cells (§4.3). The paired empty captures bound
  intra-session movement; movement between sessions shows up in the
  logged dynamic-tare series. No drift has been measured on the
  development unit; casual empty readings hours apart differed by a few
  kg but under uncontrolled conditions (surface, possible objects on the
  platform), so they do not constitute a measurement.
- **Minimal closure has zero redundancy**: three captures determine
  ($X_1$, $X_2$, $\delta$) exactly, so fit residuals cannot flag an error
  in them. Cross-checks require a third object, a known-total capture
  engaging the clamped cell, or external comparison.

## 6. Reference results (Wii Balance Board, 2026-07-14)

Session: $W = 1.451$ kg (coffee-scale standard), two unweighed water
jugs, protocol of §3, one round of corner additions.

| Quantity | Value |
| --- | --- |
| Gains (TR, BR, TL, BL) | 1.006, 1.013, 1.000, 0.992 |
| Offsets, unclamped cells (kg) | +2.369, +1.866, +1.721 |
| Offset, clamped cell (kg) | −2.525 |
| Fitted $X_1$, $X_2$ (kg) | 6.053, 10.202 |
| Per-cell residual RMS (kg) | 0.030, 0.044, 0.031, ~0 |
| Corner-delta sums vs $W$ (kg) | 1.443–1.467 |
| Tare-only bias removed (kg) | +2.3 at body weight |

The clamped cell's residual is ~0 because its parameters are exactly
determined (§5, last item). Corrected body-weight readings agreed with an
external scale; uncalibrated (tare-only) readings ran 2.3 kg low,
consistent with §1.2.
