/* Parameter definitions and the guidance shown in popovers.
   Kept apart from app.js so the words can be edited without touching logic.
   Every default and limit comes from the server (engine.DEFAULTS / LIMITS);
   only presentation lives here. */
"use strict";

var pct = function (v) { return Math.round(v * 100) + "%"; };
var pc1 = function (v) { return (v * 100).toFixed(1) + "%"; };
var pctile = function (v) { return Math.round(v) + "%"; };
var num2 = function (v) { return Number(v).toFixed(2); };
var int = function (v) { return String(Math.round(v)); };

var PARAMS = {
  2: [
    { key: "straighten_margin", label: "Confidence before rotating", min: 0, max: 0.3, step: 0.01, fmt: num2,
      range: ["0.00 rotate on any gain", "0.30 only when certain"],
      help: "<p>Each photo is tried at all four quarter turns and kept at whichever the face detector is most confident about. This is how much <b>better</b> that turn must score before the file is actually rotated.</p><p>Too low and detector noise spins correctly-oriented photos; too high and sideways ones stay sideways. Leave at <code>0.06</code> unless you see mistakes. A frame with no face is never rotated automatically &mdash; use the buttons on its card.</p>" }
  ],
  3: [
    { key: "dup_threshold", label: "How alike counts as a duplicate", min: 5, max: 140, step: 1, fmt: int,
      range: ["5 identical only", "140 merges different poses"],
      help: "<p>Hamming distance on a <b>256-bit</b> perceptual hash &mdash; how many of 256 bits differ between two frames.</p><p>Most advice online quotes about 12, but that is for a 64-bit hash, so those numbers are four times too small here. Use <code>20&ndash;40</code> for true re-shoots only, <code>60</code> for a burst where nobody moved much. Above <code>100</code> genuinely different poses start merging.</p>" },
    { key: "dup_keep", label: "Which frame of a pair to keep", type: "select",
      options: [["face", "Sharper on the face"], ["score", "Highest score"]],
      help: "<p><b>Sharper on the face</b> is the better default for people. Score alone gets duplicates wrong often enough to matter: the higher-scoring frame is sometimes the grainier one, while its twin is sharper where it counts.</p>" }
  ],
  4: [
    { key: "exp_blown", on: "exp_blown_on", hit: "blown", label: "Blown highlights", min: 0.01, max: 0.4, step: 0.01, fmt: pct,
      range: ["1% very strict", "40% only the worst"],
      help: "<p>The share of the frame that is pure white. That detail is gone for good &mdash; no edit recovers it.</p><p>A little clipping is normal: a window, sun on a white saree. <code>10%</code> usually means the shot is lost. Use <code>4&ndash;6%</code> if you are strict about highlights, <code>20%</code> for a very bright venue.</p>" },
    { key: "exp_bright", on: "exp_bright_on", hit: "bright", label: "Washed out", min: 150, max: 250, step: 1, fmt: int,
      range: ["150 catches more", "250 almost nothing"],
      help: "<p>Average brightness of the whole frame, 0&ndash;255. Above this the entire photograph reads as overexposed, not just its highlights.</p><p><code>202</code> is high on purpose &mdash; a bright outdoor ceremony can legitimately average 180&ndash;190.</p>" },
    { key: "exp_dark", on: "exp_dark_on", hit: "dark", label: "Low light", min: 20, max: 110, step: 1, fmt: int,
      range: ["20 near-black only", "110 catches moody shots"],
      help: "<p>Average brightness below this reads as too dark. Dark frames can often be lifted in editing, so this is forgiving by nature.</p><p>Careful raising it: a deliberate candle-lit shot may average 70&ndash;90 and is not a fault.</p>" },
    { key: "exp_shadow", on: "exp_shadow_on", hit: "shadow", label: "Lost shadow detail", min: 0.02, max: 0.5, step: 0.01, fmt: pct,
      range: ["2% very strict", "50% only extreme"],
      help: "<p>The share of the frame at pure black. Set higher than highlights on purpose &mdash; a dark suit or night background is normal, and crushed black is less noticeable than clipped white.</p>" },
    { key: "exp_hot", on: "exp_hot_on", hit: "hot", label: "Bright highlights (minor)", min: 0.005, max: 0.2, step: 0.005, fmt: pc1,
      range: ["0.5% strict", "20% lenient"],
      help: "<p>Some clipping, but not enough to ruin the frame. <b>Off by default</b> and it should usually stay off &mdash; turning it on removes a lot of photos for a fault most clients never notice.</p>" }
  ],
  5: [
    { key: "focus_soft", on: "focus_soft_on", hit: "soft", label: "Out of focus", min: 0, max: 50, step: 1, fmt: pctile,
      range: ["0 remove none", "50 remove half"],
      help: "<p>A <b>percentile, not a sharpness value</b>: at <code>22</code> the softest 22% of this batch is called out of focus.</p><p>Relative because sharpness has no universal number &mdash; a ring close-up and a wide hall shot measure completely differently while both being sharp. The consequence: it always removes roughly that share, even on a flawless shoot. Lower it if your focus hit rate is good, and use the rejected pile to rescue anything it took unfairly.</p>" },
    { key: "focus_slight", on: "focus_slight_on", hit: "slight", label: "Slightly soft (minor)", min: 0, max: 70, step: 1, fmt: pctile,
      range: ["low", "high"],
      help: "<p>Frames between the out-of-focus cut and this percentile. <b>Off by default</b>: a slightly soft candid can still be the best photograph of the moment. Let the score cut at Narrow handle these instead.</p>" }
  ],
  6: [
    { key: "subj_fsoft", on: "subj_fsoft_on", hit: "fsoft", label: "Face not in focus", min: 0.05, max: 1, step: 0.05, fmt: num2,
      range: ["0.05 extreme only", "1.00 face must beat frame"],
      help: "<p>The face must be softer than this fraction of its own frame's sharpness <b>and</b> among the softest quarter of faces in the batch. Both must agree.</p><p>The frame ratio alone misfires &mdash; faces are usually <b>sharper</b> than their frames. <code>0.35</code> sat in a clean gap in real wedding data. Raising it towards <code>0.6</code> starts catching sharp faces.</p>" },
    { key: "subj_fdark", on: "subj_fdark_on", hit: "fdark", label: "Face in shadow", min: 0.3, max: 1, step: 0.02, fmt: num2,
      range: ["0.30 severe backlight", "1.00 any dimming"],
      help: "<p>Catches backlighting &mdash; a bright window behind the couple leaving faces dark. The face must be dimmer than this fraction of the frame <b>and</b> below 95 of 255.</p><p>A whole-frame exposure reading misses backlighting completely, because the average looks fine.</p>" },
    { key: "subj_noface_on", type: "toggle", hit: "noface", label: "Remove frames with nobody in them",
      help: "<p><b>Leave off for a wedding.</b> Rings, the cake, the mandap and table settings have nobody in them and are not faults.</p>" },
    { key: "subj_turned", on: "subj_turned_on", hit: "turned", label: "Head turned away (minor)", min: 0.1, max: 0.9, step: 0.05, fmt: num2,
      range: ["0.10 full profile", "0.90 any angle"],
      help: "<p><b>Off, and for candid work it should stay off.</b> Someone looking away is candid photography, not a defect.</p><p>Known limit: the measure saturates, so a 40&deg; turn and a full profile can read the same.</p>" },
    { key: "face_min", label: "Smallest face that counts", min: 0.02, max: 0.2, step: 0.01, fmt: pct,
      range: ["2% counts bystanders", "20% foreground only"],
      help: "<p>Faces smaller than this share of the frame's longest edge are ignored &mdash; guests in the background, not subjects. Raise it to <code>5&ndash;8%</code> if portraits are being judged on a bystander. This also decides the people bands at step 10.</p>" }
  ],
  8: [
    { key: "grain_pct", on: "grain_on", hit: "grain", label: "Grainy", min: 50, max: 99, step: 1, fmt: pctile,
      range: ["50 flags half", "99 only the worst"],
      help: "<p>Noise above this percentile of the batch <b>and</b> at least 1.25&times; the median. Both are needed: a percentile alone flags everything when a batch has little spread.</p><p>Grain is the price of a usable shutter speed at night. Leave this off unless you have photos to spare.</p>" },
    { key: "flat", on: "flat_on", hit: "flat", label: "Flat, low contrast", min: 10, max: 60, step: 1, fmt: int,
      range: ["10 almost nothing", "60 catches soft light"],
      help: "<p>Spread of brightness across the frame. Low means haze or a grey sky. Contrast is the easiest thing to fix in editing, which is why this is off.</p>" },
    { key: "noise_w", label: "Noise weight in the score", min: 0.05, max: 0.45, step: 0.01, fmt: num2,
      range: ["0.05 grain barely counts", "0.45 grain dominates"],
      help: "<p>Instead of removing grainy frames, push them <b>down the ranking</b> so Narrow handles them. Usually the better approach.</p><p>The other four weights keep their proportions. Above about <code>0.35</code> legitimate low-light work starts being over-punished.</p>" }
  ],
  9: [
    { key: "narrow_keep", label: "How many to keep", min: 1, max: 100, step: 1, fmt: int, dynamicMax: true,
      range: ["1", "all"],
      help: "<p>Kept in score order, best first. A count rather than a score threshold because scores are relative to the batch &mdash; \"above 60\" means something different for every shoot.</p><p>Aim for about <b>3&times;</b> the selects you want, so the later steps have real choices.</p>" },
    { key: "penalty", label: "Penalty per major fault", min: 0.4, max: 1, step: 0.05, fmt: num2,
      range: ["0.40 brutal", "1.00 none"],
      help: "<p>Each major fault multiplies the score by this, so two compound. It exists because a frame can measure well while the focus missed the person entirely.</p><p><code>0.80</code> costs about a fifth: enough to rank a flawed frame below a clean one of similar quality. Set <code>1.00</code> to rank on measurements alone.</p>" }
  ],
  10: [
    { key: "band_split", label: "Where a small group becomes large", min: 3, max: 12, step: 1, fmt: int,
      range: ["3", "12"],
      help: "<p>Faces at or above this count are a <b>large group</b>. Groups start at 3, matching the album tool's <code>GROUP_MIN_PEOPLE</code>.</p>" },
    { key: "face_min", label: "Smallest face that counts", min: 0.02, max: 0.2, step: 0.01, fmt: pct,
      range: ["2% counts the crowd", "20% foreground only"],
      help: "<p>This decides the bands more than anything else. At <code>2%</code> a guest in the background counts, so a portrait with onlookers lands in \"small group\". It counts <b>faces the detector found</b>, not people, so large groups are usually undercounted.</p>" }
  ],
  11: [
    { key: "shot_threshold", label: "How alike counts as the same moment", min: 20, max: 200, step: 5, fmt: int,
      range: ["20 little groups", "200 everything merges"],
      help: "<p>The same hash distance as Duplicates, but looser: that step asks \"is this the same photograph\", this asks \"is this the same moment\". Frames in between compete with each other rather than with the whole shoot.</p><p>Lower it to <code>60&ndash;80</code> if different poses are being merged.</p>" },
    { key: "prefer_clean", type: "toggle", label: "Prefer frames with no major fault",
      help: "<p>Within each moment, clean frames are offered first &mdash; but flawed ones are <b>held back, not excluded</b>, so you still get the number of selects you asked for.</p>" }
  ]
};

var STEP_HELP = {
  1: "<p>Drop photos in, or open a folder on disk. Either way they are <b>copied</b> into a working area first &mdash; your originals are never modified.</p><p>Opening a folder lets the final step save the selection inside it.</p>",
  2: "<p>Rotates frames so heads sit at the top, using whichever quarter turn the face detector is most confident about. These files often carry no usable orientation tag, so it has to be measured.</p><p>Quarter turns only &mdash; nothing cropped or stretched. Frames with no face are left alone; rotate those by hand.</p>",
  3: "<p>Near-identical frames. Only one of each survives.</p><p>Duplicates run before the quality steps because finding them needs the photos decoded anyway, and the decode is most of the cost.</p>",
  4: "<p>Light that cannot be fixed afterwards. Each check is independent; untick one to keep those frames.</p><p>A photo is counted against the <b>first</b> step that removes it. Cards show other steps it would also fail.</p>",
  5: "<p>Softness that cannot be sharpened back, judged against the rest of this batch.</p>",
  6: "<p>The frame can be well lit and sharp and still fail the people in it &mdash; focus behind the couple, or faces in shadow.</p>",
  7: "<p><b>Not available in this version.</b> Reading expressions and closed eyes needs two models that are not installed: an expression classifier (<code>emotion-ferplus</code>) and a separate eye-state or landmark model, because closed eyes is not one of the expression classes.</p><p>This step passes everything through unchanged.</p>",
  8: "<p>A review step. Grain has no major fault, so nothing is removed unless you switch a check on. The noise weight is usually the better tool: it ranks grainy frames lower instead of removing them.</p>",
  9: "<p>Everything here is usable &mdash; only weaker than the rest. Keep the strongest N by score.</p>",
  10: "<p>Sort survivors by how many faces they hold, and set how many selects each kind contributes. An album needs portraits and group shots, not six of whichever kind scored best.</p><p>Unticking a band <b>removes</b> its photos; a quota of 0 keeps them but asks for none.</p>",
  11: "<p>Frames can show the same moment without being duplicates. The quotas from step 10 are filled one moment at a time, so the selects are different photographs rather than one pose repeated.</p>",
  12: "<p>The single best frame, how it got there, and where to save the result.</p><p>The funnel's selects are numbered <code>01_</code> onward. Your manual picks go to <code>Manually_Selected/</code> and never change the funnel's answer.</p>"
};

var BANDS = [
  ["solo", "One person"], ["duo", "Two people"], ["small", "Small group"],
  ["large", "Large group"], ["none", "Nobody in frame"]
];
