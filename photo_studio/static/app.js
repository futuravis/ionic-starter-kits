/* ==========================================================================
   Photo Studio
   Talks to the same API as photo_shortlist; presents it as a three-step site
   with a gallery, a table and a lightbox.

   Everything is built with createElement rather than innerHTML with
   interpolation. Filenames come from disk and land in the page as text, so
   building markup out of them by string concatenation would put an injection
   hole exactly where user-supplied data arrives.
   ========================================================================== */

"use strict";

const BAR_LABELS = {
  sharpness: "Sharpness",
  exposure: "Exposure",
  contrast: "Contrast",
  saturation: "Colour",
  noise: "Low noise"
};

const State = {
  result: null,
  filter: "all",
  view: "grid",
  fit: "cover",            // "cover" crops wide frames, "contain" shows all
  lightboxOrder: [],
  lightboxAt: -1,
  poller: null
};

const $ = (id) => document.getElementById(id);
const make = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};

const thumbUrl = (name, w) => "/api/thumb?w=" + w + "&p=" + encodeURIComponent(name);
const photoUrl = (name, w) => "/api/photo?w=" + w + "&p=" + encodeURIComponent(name);

async function api(route, options) {
  const response = await fetch(route, options);
  let data = {};
  try { data = await response.json(); } catch (e) { /* no body */ }
  if (!response.ok) throw new Error(data.error || ("Request failed: " + response.status));
  return data;
}

function notify(message, kind) {
  const box = $("alert");
  if (!message) { box.hidden = true; return; }
  box.textContent = message;
  box.className = "alert shell" + (kind === "bad" ? " alert-bad"
    : kind === "good" ? " alert-good" : "");
  box.hidden = false;
}

/* ----------------------------------------------------------- navigation */

function goTo(name) {
  ["add", "analyse", "review"].forEach((step) => {
    $("panel-" + step).hidden = step !== name;
  });
  document.querySelectorAll(".step").forEach((button) => {
    const on = button.dataset.goto === name;
    button.classList.toggle("is-current", on);
    if (on) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* --------------------------------------------------------------- shared */

function severityOf(photo) {
  if (!photo.issues.length) return "clean";
  return photo.issues.some((i) => i.severity === "major") ? "major" : "minor";
}

function barsFor(breakdown) {
  const wrap = make("div", "bars");
  Object.keys(BAR_LABELS).forEach((key) => {
    if (!(key in breakdown)) return;
    const value = Math.round(breakdown[key] * 100);

    const row = make("div", "bar-row");
    row.appendChild(make("span", null, BAR_LABELS[key]));

    const track = make("span", "bar-track");
    track.setAttribute("role", "img");
    track.setAttribute("aria-label", BAR_LABELS[key] + ": " + value + " of 100");
    const fill = make("span", "bar-fill" +
      (value >= 70 ? " is-high" : value <= 35 ? " is-low" : ""));
    fill.style.width = value + "%";
    track.appendChild(fill);

    row.appendChild(track);
    row.appendChild(make("span", "v", value));
    wrap.appendChild(row);
  });
  return wrap;
}

function verdictFor(photo) {
  const list = make("ul", "verdict");

  if (!photo.issues.length) {
    const li = make("li");
    li.appendChild(make("span", "pip clean"));
    const body = make("span");
    body.appendChild(make("span", "what", "Nothing wrong found. "));
    body.appendChild(make("span", "why",
      "No measurement on this frame came out poorly."));
    li.appendChild(body);
    list.appendChild(li);
    return list;
  }

  photo.issues.forEach((issue) => {
    const li = make("li");
    li.appendChild(make("span", "pip " + issue.severity));
    const body = make("span");
    body.appendChild(make("span", "what", issue.label + ". "));
    body.appendChild(make("span", "why", issue.detail));
    li.appendChild(body);
    list.appendChild(li);
  });

  return list;
}

function tagsFor(photo) {
  const wrap = make("div", "tags");
  if (!photo.issues.length) {
    wrap.appendChild(make("span", "tag clean", "Clean"));
    return wrap;
  }
  photo.issues.forEach((issue) => {
    const tag = make("span", "tag " + issue.severity, issue.label);
    tag.title = issue.detail;
    wrap.appendChild(tag);
  });
  return wrap;
}

function scoreLine(photo) {
  const wrap = make("div", "big-score");
  wrap.appendChild(document.createTextNode(photo.score));
  wrap.appendChild(make("small", null, " / 100"));
  return wrap;
}

/* ------------------------------------------------------------- step one */

async function loadSettings() {
  const settings = await api("/api/settings");

  $("want-input").max = settings.max_want;
  $("want-input").min = settings.min_want;
  $("want-input").value = settings.default_want;

  $("drop-formats").textContent =
    settings.extensions.join("  ") + "   \u00b7   up to " +
    settings.max_files + " photos, " + settings.max_upload_mb + " MB per upload";

  $("run-hint").textContent = "Using " + settings.workers + " cores.";

  if (!settings.faces_available) {
    const toggle = $("opt-faces");
    toggle.checked = false;
    toggle.disabled = true;
    $("faces-hint").textContent =
      "Unavailable: the face model is missing. Run " +
      "python3 -m wedding_album download-models to enable it.";
  }

  paintTray(settings.uploaded, settings.names);

  // A finished analysis outlives the page. Without this, reloading the browser
  // drops you back on step one with Review greyed out while the result is
  // still sitting on the server, and the only way back is to analyse again.
  if (settings.ready) {
    try {
      paintReview(await api("/api/result"));
      notify("Showing the analysis already in progress on this machine.");
    } catch (err) {
      // Not fatal: the batch is still there to be analysed again.
      notify(err.message, "bad");
    }
  }

  // Likewise if an analysis is running when the page opens: pick the progress
  // back up rather than showing an idle screen.
  const progress = await api("/api/progress");
  if (progress.running) {
    goTo("analyse");
    $("progress").hidden = false;
    $("run").disabled = true;
    watchProgress();
  }
}

function paintTray(count, names) {
  $("tray").hidden = count === 0;
  $("run").disabled = count === 0;
  $("tray-count").textContent = count === 1 ? "1 photo" : count + " photos";

  // At least one frame has to survive, so the cap is one below the batch size.
  // Clamp the current value too, in case the batch shrank after it was set.
  const drop = $("drop-input");
  drop.max = String(Math.max(0, count - 1));
  if (Number(drop.value) > Number(drop.max)) drop.value = drop.max;

  const strip = $("filmstrip");
  strip.innerHTML = "";
  (names || []).forEach((name) => {
    const li = make("li");
    const img = make("img");
    img.src = thumbUrl(name, 168);
    img.alt = name;
    img.loading = "lazy";
    li.appendChild(img);
    strip.appendChild(li);
  });
}

async function sendFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;

  notify("Adding " + files.length + " photo" + (files.length > 1 ? "s" : "") + "\u2026");

  const form = new FormData();
  files.forEach((file) => form.append("photos", file, file.name));

  try {
    const data = await api("/api/upload", { method: "POST", body: form });
    paintTray(data.uploaded, data.names);
    // A new batch invalidates any review already on screen.
    State.result = null;
    $("panel-review").hidden = true;
    document.querySelector('[data-goto="review"]').disabled = true;

    let message = "Added " + data.stored + ". " + data.uploaded + " ready.";
    if (data.skipped && data.skipped.length) {
      message += " Skipped " + data.skipped.length + ": " + data.skipped[0];
    }
    notify(message, data.stored ? "good" : "bad");
  } catch (err) {
    notify(err.message, "bad");
  }
}

/* ------------------------------------------------------------- step two */

async function runAnalysis() {
  notify("");
  $("progress").hidden = false;
  $("run").disabled = true;

  try {
    await api("/api/analyse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        want: parseInt($("want-input").value, 10),
        skip_duplicates: $("opt-dupes").checked,
        use_faces: $("opt-faces").checked,
        drop_worst: parseInt($("drop-input").value, 10) || 0
      })
    });
    watchProgress();
  } catch (err) {
    $("progress").hidden = true;
    $("run").disabled = false;
    notify(err.message, "bad");
  }
}

function watchProgress() {
  if (State.poller) clearInterval(State.poller);

  State.poller = setInterval(async () => {
    let p;
    try { p = await api("/api/progress"); } catch (e) { return; }

    const pct = p.total ? Math.round((p.current / p.total) * 100) : 0;
    $("progress-fill").style.width = pct + "%";
    $("progress-stage").textContent = p.stage
      ? p.stage.charAt(0).toUpperCase() + p.stage.slice(1)
      : "Working";
    $("progress-count").textContent = p.total ? p.current + " / " + p.total : "";

    if (p.running) return;

    clearInterval(State.poller);
    State.poller = null;
    $("progress").hidden = true;
    $("run").disabled = false;

    if (p.error) { notify(p.error, "bad"); return; }
    if (p.ready) {
      notify(p.message || "Done.", "good");
      paintReview(await api("/api/result"));
    }
  }, 320);
}

/* ----------------------------------------------------------- step three */

/* Every tile is portrait, so a landscape original is cropped at the sides.
   This marks which ones, and the title says so, because silently hiding part
   of a photograph while asking someone to judge it would be misleading. */
function shapeTag(photo) {
  const cropped = photo.orientation === "landscape";
  const tag = make("span", "shape-tag" + (cropped ? " is-cropped" : ""),
    photo.orientation === "landscape" ? "Wide"
      : photo.orientation === "portrait" ? "Tall"
      : photo.orientation === "square" ? "Square" : "?");
  tag.title = cropped
    ? photo.dimensions + " landscape, shown cropped to portrait \u2014 " +
      "open it to see the whole frame"
    : photo.dimensions + " " + photo.orientation;
  return tag;
}

function frameCard(photo, wide) {
  const severity = severityOf(photo);
  const card = make("article", "frame" +
    (photo.pick ? " is-select" : severity === "major" ? " has-fault" : ""));

  const button = make("button", "frame-img");
  button.type = "button";
  button.setAttribute("aria-label", "Open " + photo.name + " larger");

  const img = make("img");
  img.src = thumbUrl(photo.photo, wide ? 520 : 380);
  img.alt = photo.name + ". " + (photo.headline || "");
  img.loading = "lazy";
  button.appendChild(img);

  const pin = make("span", "pin" + (photo.rank <= 3 ? " is-podium" : ""), photo.rank);
  button.appendChild(pin);
  button.appendChild(make("span", "score-tab", photo.score));
  if (photo.pick) button.appendChild(make("span", "flag", "Select"));
  button.appendChild(shapeTag(photo));

  button.addEventListener("click", () => openLightbox(photo.photo));

  const meta = make("div", "frame-meta");
  meta.appendChild(make("div", "frame-name", photo.name));
  meta.appendChild(make("div", "frame-state " + severity, photo.headline || ""));
  if (wide && photo.reason) {
    meta.appendChild(make("div", "frame-why", photo.reason));
  }

  card.appendChild(button);
  card.appendChild(meta);
  return card;
}

function paintFeature(photo, total) {
  const body = $("feature-body");
  body.innerHTML = "";

  const figure = make("figure", "feature-frame");
  const img = make("img");
  img.src = photoUrl(photo.photo, 1200);
  img.alt = photo.name + ". " + (photo.headline || "");
  figure.appendChild(img);
  figure.appendChild(make("span", "ribbon", "Best of " + total));
  figure.appendChild(shapeTag(photo));
  figure.addEventListener("click", () => openLightbox(photo.photo));

  const meta = make("div", "feature-meta");
  meta.appendChild(scoreLine(photo));
  if (photo.penalty > 0) {
    meta.appendChild(make("div", "dims",
      "measured " + photo.quality + ", less " + photo.penalty +
      " for major faults"));
  }
  meta.appendChild(make("div", "filename", photo.name));
  const extra = [photo.dimensions, photo.orientation];
  if (photo.faces > 0) {
    extra.push(photo.faces + (photo.faces === 1 ? " face" : " faces"));
  }
  meta.appendChild(make("div", "dims", extra.filter(Boolean).join("  \u00b7  ")));
  meta.appendChild(verdictFor(photo));
  meta.appendChild(barsFor(photo.breakdown || {}));

  body.appendChild(figure);
  body.appendChild(meta);
}

function visiblePhotos() {
  const all = State.result.ranked;
  return all.filter((photo) => {
    if (State.filter === "clean") return photo.usable;
    if (State.filter === "flawed") return !photo.usable;
    if (State.filter === "selects") return photo.pick;
    return true;
  });
}

function paintCollection() {
  const shown = visiblePhotos();
  State.lightboxOrder = shown.map((p) => p.photo);

  const grid = $("grid");
  grid.innerHTML = "";
  grid.classList.toggle("is-fit", State.fit === "contain");
  shown.forEach((photo) => grid.appendChild(frameCard(photo, false)));

  const body = $("table-body");
  body.innerHTML = "";
  shown.forEach((photo) => body.appendChild(tableRow(photo)));

  grid.hidden = State.view !== "grid";
  $("table-wrap").hidden = State.view !== "table";

  const total = State.result.ranked.length;
  $("all-note").textContent = shown.length === total
    ? "All " + total + " frames, ranked by score after any fault discount."
    : "Showing " + shown.length + " of " + total + " frames.";
}

function tableRow(photo) {
  const tr = make("tr", photo.pick ? "is-select" : null);

  tr.appendChild(make("td", "num", photo.rank));

  const cell = make("td");
  const holder = make("div", "frame-cell");
  const img = make("img");
  img.src = thumbUrl(photo.photo, 120);
  img.alt = photo.name;
  img.loading = "lazy";
  img.addEventListener("click", () => openLightbox(photo.photo));
  holder.appendChild(img);
  holder.appendChild(make("span", "fn", photo.name));
  cell.appendChild(holder);
  tr.appendChild(cell);

  tr.appendChild(make("td", "num", photo.score));
  tr.appendChild(make("td", "num", photo.group));
  tr.appendChild(make("td", "num", photo.faces >= 0 ? photo.faces : "\u2014"));

  const condition = make("td");
  condition.appendChild(tagsFor(photo));
  tr.appendChild(condition);

  return tr;
}

/* Removal is shown, not silent. Setting frames aside without saying which ones
   would leave no way to tell a sensible cut from a mistake, and these files are
   still on disk. Shown in Fit mode so the whole frame is visible - you cannot
   check a decision against a crop. */
function paintDropped(dropped, considered) {
  const section = $("dropped-section");

  if (!dropped.length) {
    section.hidden = true;
    return;
  }

  $("dropped-note").textContent =
    dropped.length + " of " + considered + " frames were set aside as the " +
    "weakest, worst first. They are excluded from the ranking and the " +
    "selects, and are still on disk \u2014 nothing was deleted.";

  const wrap = $("dropped");
  wrap.innerHTML = "";
  dropped.forEach((photo) => wrap.appendChild(frameCard(photo, false)));

  section.hidden = false;
}

function paintFaults(tally, total) {
  const wrap = $("faults");
  wrap.innerHTML = "";

  const entries = Object.entries(tally || {});
  if (!entries.length) {
    wrap.appendChild(make("p", "section-note",
      "No faults were found anywhere in this batch."));
    return;
  }

  const worst = Math.max.apply(null, entries.map((e) => e[1]));
  const majorLabels = new Set();
  State.result.ranked.forEach((photo) => {
    photo.issues.forEach((issue) => {
      if (issue.severity === "major") majorLabels.add(issue.label);
    });
  });

  entries.forEach(([label, count]) => {
    const line = make("div", "fault-line");
    line.appendChild(make("div", "label", label));

    const bar = make("div", "fault-bar");
    const fill = make("span", majorLabels.has(label) ? "major" : "minor");
    fill.style.width = Math.round((count / worst) * 100) + "%";
    bar.appendChild(fill);
    line.appendChild(bar);

    line.appendChild(make("div", "count", count + " / " + total));
    wrap.appendChild(line);
  });
}

function paintReview(result) {
  if (!result.ready) return;
  State.result = result;

  const s = result.stats;

  if (result.best) paintFeature(result.best, s.analyzed);

  State.filter = "all";
  document.querySelectorAll("[data-filter]").forEach((b) => {
    const on = b.dataset.filter === "all";
    b.classList.toggle("is-on", on);
    b.setAttribute("aria-pressed", String(on));
  });
  paintCollection();

  paintDropped(result.dropped || [], result.considered || s.analyzed);

  paintFaults(result.issue_tally, s.analyzed);

  const shapes = Object.entries(result.shape_tally || {})
    .map(([shape, n]) => n + " " + shape).join(", ");
  const faceNote = result.faces_probed
    ? "Face checks ran: presence, size, lighting and focus. Closed eyes and " +
      "expressions are not detected \u2014 the model gives five landmarks " +
      "with no eyelid or mouth-shape data, so that verdict would be guesswork."
    : "Face checks did not run, so every fault here is about the photograph " +
      "rather than the people in it.";
  $("faults-note").textContent = shapes
    ? "Shapes in this batch: " + shapes +
      ". Tiles are shown portrait, so wide frames are cropped at the sides " +
      "in the grid \u2014 open one to see all of it. " + faceNote
    : faceNote;

  const bad = $("unreadable");
  if (result.failures.length) {
    bad.textContent = result.failures.length +
      " file(s) could not be read: " +
      result.failures.map((f) => f.name + " (" + f.error + ")").join(", ");
    bad.hidden = false;
  } else {
    bad.hidden = true;
  }

  document.querySelector('[data-goto="review"]').disabled = false;
  goTo("review");
}

/* ------------------------------------------------------------- lightbox */

async function openLightbox(name) {
  const order = State.lightboxOrder.length
    ? State.lightboxOrder
    : State.result.ranked.map((p) => p.photo);
  State.lightboxOrder = order;
  State.lightboxAt = order.indexOf(name);

  $("lightbox").hidden = false;
  document.body.style.overflow = "hidden";
  await showLightbox(name);
  $("lb-close").focus();
}

async function showLightbox(name) {
  let detail;
  try {
    detail = await api("/api/detail?p=" + encodeURIComponent(name));
  } catch (err) {
    notify(err.message, "bad");
    return;
  }

  $("lb-rank").textContent = "#" + detail.rank + " of " + detail.total;
  $("lb-name").textContent = detail.name;

  const img = $("lb-img");
  img.src = photoUrl(detail.photo, 1800);
  img.alt = detail.name + ". " + (detail.headline || "");

  const side = $("lb-side");
  side.innerHTML = "";

  side.appendChild(scoreLine(detail));
  if (detail.penalty > 0) {
    side.appendChild(make("p", "side-note",
      "measured " + detail.quality + ", less " + detail.penalty +
      " for major faults"));
  }
  if (detail.reason) {
    const why = make("div", "lb-block");
    why.appendChild(make("h3", null, "Why it was selected"));
    why.appendChild(make("p", "section-note", detail.reason));
    side.appendChild(why);
  }

  const condition = make("div", "lb-block");
  condition.appendChild(make("h3", null, "Condition"));
  condition.appendChild(verdictFor(detail));
  side.appendChild(condition);

  const measured = make("div", "lb-block");
  measured.appendChild(make("h3", null, "Measurements"));
  measured.appendChild(barsFor(detail.breakdown || {}));
  side.appendChild(measured);

  if (detail.siblings && detail.siblings.length) {
    const block = make("div", "lb-block");
    block.appendChild(make("h3", null,
      "Other frames of this shot (" + detail.siblings.length + ")"));
    const strip = make("div", "lb-siblings");
    detail.siblings.forEach((sibling) => {
      const button = make("button");
      button.type = "button";
      button.title = "#" + sibling.rank + "  score " + sibling.score;
      button.setAttribute("aria-label",
        "Show " + sibling.name + ", rank " + sibling.rank);
      const thumb = make("img");
      thumb.src = thumbUrl(sibling.photo, 150);
      thumb.alt = sibling.name;
      button.appendChild(thumb);
      button.addEventListener("click", () => {
        const at = State.lightboxOrder.indexOf(sibling.photo);
        if (at >= 0) State.lightboxAt = at;
        showLightbox(sibling.photo);
      });
      strip.appendChild(button);
    });
    block.appendChild(strip);
    side.appendChild(block);
  }
}

function closeLightbox() {
  $("lightbox").hidden = true;
  $("lb-img").src = "";
  document.body.style.overflow = "";
}

function stepLightbox(delta) {
  const order = State.lightboxOrder;
  if (!order.length) return;
  State.lightboxAt = (State.lightboxAt + delta + order.length) % order.length;
  showLightbox(order[State.lightboxAt]);
}

/* ---------------------------------------------------------------- wiring */

$("dropzone").addEventListener("click", () => $("file-input").click());
$("dropzone").addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    $("file-input").click();
  }
});
["dragenter", "dragover"].forEach((name) => {
  $("dropzone").addEventListener(name, (event) => {
    event.preventDefault();
    $("dropzone").classList.add("is-over");
  });
});
["dragleave", "drop"].forEach((name) => {
  $("dropzone").addEventListener(name, (event) => {
    event.preventDefault();
    $("dropzone").classList.remove("is-over");
  });
});
$("dropzone").addEventListener("drop", (event) => sendFiles(event.dataTransfer.files));

$("file-input").addEventListener("change", (event) => {
  sendFiles(event.target.files);
  event.target.value = "";          // so the same file can be added again
});

/* Continue stands every frame upright before moving on.
   These files carry no usable EXIF orientation - every one reads "normal",
   including the ones that are plainly sideways - so the rotation has to be
   measured from the photograph rather than read from metadata. Only whole
   quarter turns are applied: nothing is cropped, stretched or resampled. */
async function straightenThenContinue() {
  const button = $("to-analyse");
  button.disabled = true;
  button.textContent = "Checking orientation\u2026";
  notify("Standing photos upright\u2026");

  try {
    await api("/api/straighten", { method: "POST" });
    await waitForIdle();

    const settings = await api("/api/settings");
    paintTray(settings.uploaded, settings.names);
    reportStraighten(settings.straightened);
    goTo("analyse");
  } catch (err) {
    notify(err.message, "bad");
  } finally {
    button.disabled = false;
    button.textContent = "Continue";
  }
}

/* Poll until the background job finishes, surfacing its stage as it goes. */
function waitForIdle() {
  return new Promise((resolve) => {
    const timer = setInterval(async () => {
      let p;
      try { p = await api("/api/progress"); } catch (e) { return; }

      if (p.stage) {
        const count = p.total ? "  " + p.current + " / " + p.total : "";
        notify(p.stage.charAt(0).toUpperCase() + p.stage.slice(1) + count);
      }
      if (p.running) return;

      clearInterval(timer);
      if (p.error) notify(p.error, "bad");
      else if (p.message) notify(p.message, "good");
      resolve();
    }, 300);
  });
}

function reportStraighten(report) {
  const box = $("straighten-report");
  if (!box) return;

  if (!report || !report.ran || !report.rotated) {
    box.hidden = true;
    return;
  }

  box.innerHTML = "";
  box.appendChild(make("p", "section-note",
    "Rotated " + report.rotated + " of " + report.total +
    " photos upright. Only whole quarter turns were applied \u2014 nothing " +
    "was cropped or stretched. Your original files are untouched; this " +
    "applies to the uploaded copies."));

  const list = make("ul", "turn-list");
  report.photos.forEach((photo) => {
    const li = make("li");
    if (photo.error) {
      li.appendChild(make("span", "turn-angle bad", "failed"));
      li.appendChild(make("span", "turn-name", photo.name + " \u2014 " + photo.error));
    } else {
      li.appendChild(make("span", "turn-angle", photo.angle + "\u00b0"));
      li.appendChild(make("span", "turn-name",
        photo.name + "   " + photo.before + " \u2192 " + photo.after));
    }
    list.appendChild(li);
  });
  box.appendChild(list);
  box.hidden = false;
}

$("to-analyse").addEventListener("click", straightenThenContinue);
$("run").addEventListener("click", runAnalysis);

function stepNumber(input, by) {
  const next = (parseInt(input.value, 10) || 0) + by;
  const low = Number(input.min);
  const high = input.max === "" ? Infinity : Number(input.max);
  input.value = Math.max(low, Math.min(high, next));
}

document.querySelectorAll("[data-nudge]").forEach((button) => {
  button.addEventListener("click", () =>
    stepNumber($("want-input"), parseInt(button.dataset.nudge, 10)));
});

document.querySelectorAll("[data-drop]").forEach((button) => {
  button.addEventListener("click", () =>
    stepNumber($("drop-input"), parseInt(button.dataset.drop, 10)));
});

document.querySelectorAll(".step").forEach((button) => {
  button.addEventListener("click", () => {
    if (!button.disabled) goTo(button.dataset.goto);
  });
});

document.querySelectorAll("[data-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    State.filter = button.dataset.filter;
    document.querySelectorAll("[data-filter]").forEach((other) => {
      const on = other === button;
      other.classList.toggle("is-on", on);
      other.setAttribute("aria-pressed", String(on));
    });
    paintCollection();
  });
});

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => {
    State.view = button.dataset.view;
    document.querySelectorAll("[data-view]").forEach((other) => {
      const on = other === button;
      other.classList.toggle("is-on", on);
      other.setAttribute("aria-pressed", String(on));
    });
    paintCollection();
  });
});

document.querySelectorAll("[data-fit]").forEach((button) => {
  button.addEventListener("click", () => {
    State.fit = button.dataset.fit;
    document.querySelectorAll("[data-fit]").forEach((other) => {
      const on = other === button;
      other.classList.toggle("is-on", on);
      other.setAttribute("aria-pressed", String(on));
    });
    paintCollection();
  });
});

$("lb-close").addEventListener("click", closeLightbox);
$("lb-prev").addEventListener("click", () => stepLightbox(-1));
$("lb-next").addEventListener("click", () => stepLightbox(1));
$("lightbox").addEventListener("click", (event) => {
  if (event.target === $("lightbox")) closeLightbox();
});

document.addEventListener("keydown", (event) => {
  if ($("lightbox").hidden) return;
  if (event.key === "Escape") closeLightbox();
  if (event.key === "ArrowLeft") stepLightbox(-1);
  if (event.key === "ArrowRight") stepLightbox(1);
});

async function wipe() {
  try {
    await api("/api/reset", { method: "POST" });
    State.result = null;
    paintTray(0, []);
    $("panel-review").hidden = true;
    document.querySelector('[data-goto="review"]').disabled = true;
    goTo("add");
    notify("Cleared. Add a new batch.", "good");
  } catch (err) {
    notify(err.message, "bad");
  }
}

$("clear-all").addEventListener("click", wipe);
$("start-over").addEventListener("click", wipe);

loadSettings().catch((err) => notify(err.message, "bad"));
