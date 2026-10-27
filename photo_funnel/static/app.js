/* Photo Funnel - client.
   All markup is built with createElement and textContent. Filenames come from
   disk and land in the page as data; building HTML out of them by string
   concatenation would put an injection hole exactly where external data
   arrives. The only innerHTML used is for help text authored in help.js. */
"use strict";

var App = {
  state: null, funnel: null, step: 1, by: {},
  showAllRot: false, pileOpen: false, expanded: {},
  lb: { list: [], at: 0 },
  dest: { key: null, custom: "", name: "", touched: false,
          include: { selects: true, manual: true, notes: true, csv: true, rejected: false, vertical: false } },
  dests: null, saved: null
};

var STEP_NAMES = ["", "Add", "Straighten", "Duplicates", "Exposure", "Focus", "Subject",
  "Expressions", "Grain", "Narrow", "People", "Per moment", "The best"];

/* ------------------------------------------------------------ helpers */
function $(id) { return document.getElementById(id); }
function mk(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = String(text);
  return e;
}
function enc(s) { return encodeURIComponent(s); }
function thumb(name, w) {
  var p = App.by[name], f = fileInfo(name);
  var v = p ? p.v : (f ? f.v : "");
  return "/api/thumb?p=" + enc(name) + "&w=" + (w || 420) + "&v=" + v;
}
function fileInfo(name) {
  var fs = App.state ? App.state.files : [];
  for (var i = 0; i < fs.length; i++) if (fs[i].name === name) return fs[i];
  return null;
}
function api(route, body, method) {
  var opt = { method: method || (body !== undefined ? "POST" : "GET") };
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) { opt.headers = { "Content-Type": "application/json" }; opt.body = JSON.stringify(body); }
  return fetch(route, opt).then(function (r) {
    return r.json().catch(function () { return {}; }).then(function (d) {
      if (!r.ok) { var e = new Error(d.error || ("Request failed: " + r.status)); e.data = d; throw e; }
      return d;
    });
  });
}
var toastTimer = null;
function toast(msg, kind) {
  var t = $("toast");
  clearTimeout(toastTimer);
  if (!msg) { t.hidden = true; return; }
  t.textContent = msg;
  t.className = "toast" + (kind ? " " + kind : "");
  t.hidden = false;
  if (kind !== "bad") toastTimer = setTimeout(function () { t.hidden = true; }, 6000);
}

/* Poll a background job until it finishes. Resolves with the final progress. */
function waitJob() {
  var busy = $("busy");
  busy.hidden = false;
  return new Promise(function (resolve, reject) {
    var timer = setInterval(function () {
      api("/api/progress").then(function (p) {
        $("busyStage").textContent = p.stage ? p.stage.charAt(0).toUpperCase() + p.stage.slice(1) : "Working";
        $("busyCount").textContent = p.total ? p.current + " / " + p.total : "";
        $("busyFill").style.width = (p.total ? Math.round(p.current / p.total * 100) : 8) + "%";
        if (p.running) return;
        clearInterval(timer);
        busy.hidden = true;
        if (p.error) { reject(new Error(p.error)); return; }
        resolve(p);
      }).catch(function () { /* transient; keep polling */ });
    }, 300);
  });
}

function refreshState() {
  return api("/api/state").then(function (s) {
    App.state = s;
    $("picksN").textContent = s.picks.length;
    $("ver").textContent = "v" + s.version;
    return s;
  });
}
function applyFunnel(f) {
  App.funnel = f && f.ready ? f : null;
  App.by = {};
  if (App.funnel) {
    App.funnel.photos.forEach(function (p) { App.by[p.name] = p; });
    $("picksN").textContent = App.funnel.picks.length;
    if (App.state) App.state.picks = App.funnel.picks;
  }
}
function picks() { return App.funnel ? App.funnel.picks : (App.state ? App.state.picks : []); }
function isPicked(name) { return picks().indexOf(name) >= 0; }
function P(key) {
  if (App.funnel && key in App.funnel.params) return App.funnel.params[key];
  return App.state ? App.state.defaults[key] : null;
}

/* ------------------------------------------------------------ popovers */
var pop = null;
function closePop() {
  if (!pop) return;
  pop.el.remove(); pop.shade.remove();
  if (pop.btn) { pop.btn.setAttribute("aria-expanded", "false"); pop.btn.focus(); }
  pop = null;
}
function openPop(btn, title, sub, html, range) {
  if (pop && pop.btn === btn) { closePop(); return; }
  closePop();
  var shade = mk("div", "shade"); shade.addEventListener("click", closePop);
  var el = mk("div", "pop"); el.setAttribute("role", "dialog");
  var h = mk("div", "pop-h"), tw = mk("div");
  tw.appendChild(mk("div", "pop-t", title));
  if (sub) tw.appendChild(mk("div", "pop-s", sub));
  var x = mk("button", "pop-x", "\u00d7"); x.type = "button"; x.setAttribute("aria-label", "Close");
  x.addEventListener("click", closePop);
  h.appendChild(tw); h.appendChild(x); el.appendChild(h);
  if (range) { var r = mk("div", "pop-r"); r.appendChild(mk("span", null, range[0])); r.appendChild(mk("span", null, range[1])); el.appendChild(r); }
  var b = mk("div", "pop-b"); b.innerHTML = html; el.appendChild(b);   /* authored help only */
  document.body.appendChild(shade); document.body.appendChild(el);
  var rc = btn.getBoundingClientRect(), m = 12;
  var left = Math.max(m, Math.min(window.innerWidth - el.offsetWidth - m, rc.left + rc.width / 2 - el.offsetWidth / 2));
  var top = rc.bottom + 10;
  if (top + el.offsetHeight > window.innerHeight - m) top = Math.max(m, rc.top - el.offsetHeight - 10);
  el.style.left = left + "px"; el.style.top = top + "px";
  btn.setAttribute("aria-expanded", "true");
  pop = { el: el, shade: shade, btn: btn };
  x.focus();
}
function infoBtn(title, sub, html, range, large) {
  var b = mk("button", "info" + (large ? " lg" : ""), "i");
  b.type = "button"; b.setAttribute("aria-expanded", "false");
  b.setAttribute("aria-label", large ? "About this step" : "How to choose " + title);
  b.addEventListener("click", function (e) { e.stopPropagation(); openPop(b, title, sub, html, range); });
  return b;
}
document.addEventListener("keydown", function (e) { if (e.key === "Escape") { closePop(); closeLb(); } });
window.addEventListener("resize", closePop);

/* ------------------------------------------------------------ rail */
function railData(n) {
  var f = App.funnel, files = App.state ? App.state.files.length : 0;
  if (!f) return { count: n <= 2 && files ? files : "\u2014", delta: "" };
  var s = f.steps[n - 1];
  if (n === 2) {
    var turned = App.state.files.filter(function (x) { return x.angle; }).length;
    return { count: s.after, delta: turned ? turned + " turned" : "" };
  }
  if (n === 12) return { count: s.after, delta: s.before ? "from " + s.before : "" };
  var r = s.removed.length;
  return { count: s.after, delta: !s.available ? "n/a" : (r ? r + " out" : (n > 2 ? "0 out" : "")), cut: r > 0, na: !s.available };
}
function paintRail() {
  var rail = $("rail"); rail.innerHTML = "";
  var files = App.state ? App.state.files.length : 0;
  for (var n = 1; n <= 12; n++) {
    var d = railData(n);
    var b = mk("button", "rl" + (n === App.step ? " on" : "") + (d.na ? " na" : ""));
    b.type = "button";
    b.disabled = n > 1 && !files;
    if (n === App.step) b.setAttribute("aria-current", "step");
    b.appendChild(mk("div", "k", n));
    b.appendChild(mk("div", "nm", STEP_NAMES[n]));
    b.appendChild(mk("div", "ct", d.count));
    b.appendChild(mk("div", "dl" + (d.cut ? " cut" : ""), d.delta));
    (function (k) { b.addEventListener("click", function () { go(k); }); })(n);
    rail.appendChild(b);
  }
  var live = App.funnel ? App.funnel.steps[Math.max(0, App.step - 1)].after : files;
  if (App.funnel && App.step <= 2) live = files;
  $("liveN").textContent = live;
  $("liveL").textContent = App.step >= 3 && App.funnel ? "still in" : "photos";
}

/* ------------------------------------------------------------ navigation */
function ensureFunnel() {
  if (App.funnel && !App.state.scan_needed) return Promise.resolve();
  return api("/api/scan", {}).then(waitJob).then(function () {
    return Promise.all([refreshState(), api("/api/funnel")]);
  }).then(function (r) { applyFunnel(r[1]); });
}
function go(n) {
  closePop();
  if (n >= 2 && (!App.state || !App.state.files.length)) { toast("Add some photos first.", "bad"); return; }
  var ready = n >= 3 ? ensureFunnel() : Promise.resolve();
  ready.then(function () {
    App.step = n;
    App.expanded = {};
    if (location.hash !== "#" + n) history.replaceState(null, "", "#" + n);
    render();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }).catch(function (e) { toast(e.message, "bad"); });
}

/* ------------------------------------------------------------ shared UI */
function head(n, title) {
  var h = mk("div", "head");
  h.appendChild(mk("h1", null, n + " \u00b7 " + title));
  h.appendChild(infoBtn(title, "Step " + n, STEP_HELP[n] || "", null, true));
  h.appendChild(mk("span", "grow"));
  return h;
}
function tally(before, removed, why) {
  var t = mk("div", "tally");
  function blk(v, l, cls) { var b = mk("div", "blk " + (cls || "")); b.appendChild(mk("div", "n", v)); b.appendChild(mk("div", "l", l)); return b; }
  t.appendChild(blk(before, "before"));
  t.appendChild(mk("div", "arw", "\u2192"));
  t.appendChild(blk(removed, "removed", "cut"));
  t.appendChild(mk("div", "arw", "\u2192"));
  t.appendChild(blk(before - removed, "after", "out"));
  var w = mk("div", "why");
  w.appendChild(mk("div", null, why || ""));
  if (before) {
    var bar = mk("div", "fbar"), k = mk("span", "k"), d = mk("span", "d");
    k.style.width = ((before - removed) / before * 100) + "%";
    d.style.width = (removed / before * 100) + "%";
    bar.appendChild(k); bar.appendChild(d); w.appendChild(bar);
  }
  t.appendChild(w);
  return t;
}
function severity(p) {
  if (!p.faults.length) return "clean";
  return p.faults.some(function (f) { return f.severity === "major"; }) ? "major" : "minor";
}
function headline(p) {
  if (p.removed_by) return p.reason;
  if (!p.faults.length) return p.faces > 0 ? "Clean, " + p.faces + (p.faces === 1 ? " face" : " faces") : "Clean";
  return p.faults[0].label + (p.faults.length > 1 ? " +" + (p.faults.length - 1) : "");
}
function pickBtn(name) {
  var on = isPicked(name);
  var b = mk("button", "pick" + (on ? " on" : ""), on ? "\uD83D\uDCCC Picked by you" : "\uD83D\uDCCC Pick this one");
  b.type = "button";
  b.addEventListener("click", function (e) { e.stopPropagation(); togglePick(name); });
  return b;
}
function togglePick(name) {
  api("/api/pick", { name: name, on: !isPicked(name) }).then(function (d) {
    if (App.funnel) App.funnel.picks = d.picks;
    App.state.picks = d.picks;
    $("picksN").textContent = d.picks.length;
    renderDynamic();
    if (!$("lightbox").hidden) showLb();
  }).catch(function (e) { toast(e.message, "bad"); });
}
function card(name, opt) {
  opt = opt || {};
  var p = App.by[name];
  var cls = "card";
  if (opt.cut) cls += " cut";
  if (p && p.pick_rank && !opt.cut) cls += " sel";
  if (isPicked(name)) cls += " picked";
  var c = mk("article", cls);
  var t = mk("button", "thumb"); t.type = "button";
  t.setAttribute("aria-label", "Open " + name);
  var img = mk("img"); img.loading = "lazy"; img.alt = name; img.src = thumb(name, opt.w || 420);
  t.appendChild(img);
  if (opt.pin) t.appendChild(mk("span", "pin" + (opt.gold ? " gold" : ""), opt.pin));
  if (p) t.appendChild(mk("span", "tab", p.score));
  if (opt.why) t.appendChild(mk("span", "why", opt.why));
  t.addEventListener("click", function () { openLb(opt.list || [name], name); });
  c.appendChild(t);
  var m = mk("div", "cm");
  m.appendChild(mk("div", "fn", name));
  if (opt.state !== false && p) m.appendChild(mk("div", "st " + (opt.cut ? "major" : severity(p)), opt.label || headline(p)));
  if (opt.cut && p && p.also && p.also.length) {
    m.appendChild(mk("div", "also", "also fails: " + p.also.map(function (s) { return STEP_NAMES[s].toLowerCase(); }).join(", ")));
  }
  if (opt.extra) m.appendChild(opt.extra);
  if (opt.pick !== false && p) m.appendChild(pickBtn(name));
  c.appendChild(m);
  return c;
}
/* A grid that shows the first `limit` cards and a "show all" button. */
function cardGrid(names, opt, key, limit) {
  var wrap = mk("div");
  var g = mk("div", "grid" + (opt && opt.wide ? " wide" : ""));
  limit = limit || 24;
  var open = App.expanded[key];
  var shown = open ? names : names.slice(0, limit);
  shown.forEach(function (n) {
    var o = Object.assign({ list: names }, opt || {});
    if (typeof o.why === "function") o.why = o.why(n);
    g.appendChild(card(n, o));
  });
  wrap.appendChild(g);
  if (names.length > limit) {
    var b = mk("button", "btn btn-sm more", open ? "Show fewer" : "Show all " + names.length);
    b.type = "button";
    b.addEventListener("click", function () { App.expanded[key] = !open; renderDynamic(); });
    wrap.appendChild(b);
  }
  return wrap;
}
function section(title, count) {
  var s = mk("h2", "sect", title);
  if (count !== undefined) s.appendChild(mk("span", "cnt", count));
  return s;
}
function emptyBox(title, text) {
  var e = mk("div", "empty"); e.appendChild(mk("b", null, title)); e.appendChild(document.createTextNode(text)); return e;
}
function navRow(n, nextLabel, onNext) {
  var nav = mk("div", "nav");
  if (n > 1) { var b = mk("button", "btn", "\u2190 Back"); b.type = "button"; b.addEventListener("click", function () { go(n - 1); }); nav.appendChild(b); }
  else nav.appendChild(mk("span"));
  if (nextLabel) {
    var x = mk("button", "btn btn-p btn-big", nextLabel); x.type = "button";
    x.addEventListener("click", onNext || function () { go(n + 1); });
    nav.appendChild(x);
  }
  return nav;
}

/* ------------------------------------------------------------ parameters */
var pending = {}, paramTimer = null;
function setParam(key, value) {
  pending[key] = value;
  clearTimeout(paramTimer);
  paramTimer = setTimeout(flushParams, 120);
}
function flushParams() {
  var body = { params: pending }; pending = {};
  return api("/api/params", body).then(function (f) {
    applyFunnel(f); renderDynamic(); paintRail();
  }).catch(function (e) {
    if (e.data && e.data.scan_needed) { App.state.scan_needed = true; go(App.step); }
    else toast(e.message, "bad");
  });
}
function resetParams() {
  api("/api/params", { reset: true }).then(function (f) { applyFunnel(f); render(); toast("Parameters reset to defaults.", "good"); })
    .catch(function (e) { toast(e.message, "bad"); });
}
function hitText(n, spec) {
  if (!App.funnel || !spec.hit) return null;
  var c = (App.funnel.steps[n - 1].hits || {})[spec.hit] || 0;
  var on = spec.on ? P(spec.on) : (spec.type === "toggle" ? P(spec.key) : true);
  if (!c) return { text: "none here", on: false };
  return { text: on ? c + " removed" : "would remove " + c, on: on };
}
function paramRow(n, spec) {
  var row = mk("div", "prm");
  row.dataset.key = spec.key;
  var left = mk("div", "prm-l");
  var enabled = spec.on ? !!P(spec.on) : true;

  if (spec.on || spec.type === "toggle") {
    var ck = mk("input", "ck"); ck.type = "checkbox";
    var tkey = spec.on || spec.key;
    ck.checked = !!P(tkey);
    ck.setAttribute("aria-label", spec.label);
    ck.addEventListener("change", function () {
      row.classList.toggle("off", spec.on ? !ck.checked : false);
      if (sl) sl.disabled = !ck.checked;
      setParam(tkey, ck.checked);
    });
    left.appendChild(ck);
  }
  left.appendChild(mk("span", "prm-name", spec.label));
  left.appendChild(infoBtn(spec.label, spec.key, spec.help, spec.range));
  var hit = mk("span", "prm-hit"); hit.dataset.hit = spec.hit || "";
  left.appendChild(hit);
  row.appendChild(left);

  var ctl = mk("div", "prm-c"), sl = null;
  if (spec.type === "select") {
    var sel = mk("select"); sel.setAttribute("aria-label", spec.label);
    spec.options.forEach(function (o) { var op = mk("option", null, o[1]); op.value = o[0]; if (P(spec.key) === o[0]) op.selected = true; sel.appendChild(op); });
    sel.addEventListener("change", function () { setParam(spec.key, sel.value); });
    ctl.appendChild(sel);
  } else if (spec.type !== "toggle") {
    sl = mk("input"); sl.type = "range";
    sl.min = spec.min; sl.step = spec.step;
    var max = spec.max;
    if (spec.dynamicMax && App.funnel) max = Math.max(1, App.funnel.steps[n - 1].before);
    sl.max = max;
    sl.value = Math.min(max, P(spec.key));
    sl.disabled = !enabled;
    sl.setAttribute("aria-label", spec.label);
    var v = mk("span", "prm-v", spec.fmt(Number(sl.value)));
    sl.addEventListener("input", function () { v.textContent = spec.fmt(Number(sl.value)); });
    sl.addEventListener("change", function () { setParam(spec.key, Number(sl.value)); });
    ctl.appendChild(sl); ctl.appendChild(v);
  }
  row.appendChild(ctl);
  if (spec.on && !enabled) row.classList.add("off");
  return row;
}
function paramsPanel(n, title) {
  var specs = PARAMS[n];
  if (!specs) return null;
  var box = mk("div", "params");
  var h = mk("div", "params-h");
  h.appendChild(mk("h3", null, title || "Parameters"));
  var r = mk("button", "linkbtn", "Reset all to defaults"); r.type = "button";
  r.addEventListener("click", resetParams);
  h.appendChild(r);
  box.appendChild(h);
  var b = mk("div", "params-b");
  specs.forEach(function (s) { b.appendChild(paramRow(n, s)); });
  box.appendChild(b);
  return box;
}
function paintHits(n) {
  (PARAMS[n] || []).forEach(function (spec) {
    var el = document.querySelector('.prm[data-key="' + spec.key + '"] .prm-hit');
    if (!el) return;
    var h = hitText(n, spec);
    el.textContent = h ? h.text : "";
    el.className = "prm-hit" + (h && h.on ? " on" : "");
  });
}

/* ------------------------------------------------------------ rejected pile */
function pile(upto) {
  if (!App.funnel) return mk("span");
  var gone = App.funnel.photos.filter(function (p) { return p.removed_by && p.removed_by < upto; });
  var box = mk("div", "pile" + (App.pileOpen ? " open" : ""));
  var h = mk("button", "pile-h"); h.type = "button";
  h.appendChild(mk("span", "car", "\u25B6"));
  h.appendChild(mk("span", "t", "Rejected so far \u2014 add any to your own picks"));
  h.appendChild(mk("span", "n", gone.length || "none"));
  h.addEventListener("click", function () { App.pileOpen = !App.pileOpen; box.classList.toggle("open", App.pileOpen); });
  box.appendChild(h);
  var b = mk("div", "pile-b");
  if (!gone.length) b.appendChild(mk("p", "faint", "Nothing removed yet."));
  var steps = {};
  gone.forEach(function (p) { (steps[p.removed_by] = steps[p.removed_by] || []).push(p.name); });
  Object.keys(steps).map(Number).sort(function (a, c) { return a - c; }).forEach(function (s) {
    var g = mk("div", "pg");
    g.appendChild(mk("div", "pg-h", "Step " + s + " \u00b7 " + STEP_NAMES[s] + " \u2014 " + steps[s].length + " removed"));
    g.appendChild(cardGrid(steps[s], { cut: true }, "pile" + s, 12));
    b.appendChild(g);
  });
  box.appendChild(b);
  return box;
}

/* ------------------------------------------------------------ render */
function render() {
  paintRail();
  var stage = $("stage"); stage.innerHTML = "";
  var fn = { 1: renderAdd, 2: renderStraighten, 12: renderBest }[App.step] || renderStep;
  fn(stage, App.step);
}
function renderDynamic() {
  paintRail();
  var dyn = $("dyn");
  if (!dyn) { render(); return; }
  dyn.innerHTML = "";
  var fn = { 2: dynStraighten, 12: dynBest }[App.step] || dynStep;
  fn(dyn, App.step);
  paintHits(App.step);
}

/* ---- 1 add ---- */
function renderAdd(stage) {
  stage.appendChild(head(1, "Add the shoot"));
  var drop = mk("div", "drop"); drop.tabIndex = 0; drop.setAttribute("role", "button");
  drop.setAttribute("aria-label", "Choose photos or drop them here");
  drop.appendChild(mk("p", "big", "Drop photos here, or click to browse"));
  drop.appendChild(mk("p", "small", (App.state.extensions || []).join("  ") + " \u00b7 up to " + App.state.max_files + " photos"));
  var input = mk("input"); input.type = "file"; input.multiple = true; input.accept = "image/*"; input.hidden = true;
  drop.addEventListener("click", function () { input.click(); });
  drop.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  ["dragenter", "dragover"].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("over"); }); });
  ["dragleave", "drop"].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("over"); }); });
  drop.addEventListener("drop", function (e) { upload(e.dataTransfer.files); });
  input.addEventListener("change", function () { upload(input.files); input.value = ""; });
  stage.appendChild(drop); stage.appendChild(input);

  stage.appendChild(mk("div", "or", "or open a folder"));
  var fr = mk("div", "folder-row");
  var path = mk("input", "txt"); path.placeholder = App.state.home_desktop + "/\u2026"; path.spellcheck = false;
  path.setAttribute("aria-label", "Folder path");
  var open = mk("button", "btn", "Open folder"); open.type = "button";
  open.addEventListener("click", function () {
    if (!path.value.trim()) { toast("Type or paste a folder path.", "bad"); return; }
    api("/api/import", { path: path.value }).then(waitJob).then(function (p) { toast(p.message, "good"); return refreshState(); })
      .then(function () { App.funnel = null; render(); }).catch(function (e) { toast(e.message, "bad"); });
  });
  path.addEventListener("keydown", function (e) { if (e.key === "Enter") open.click(); });
  fr.appendChild(path); fr.appendChild(open);
  stage.appendChild(fr);

  var files = App.state.files;
  if (files.length) {
    var s = section("In this batch", files.length + (App.state.source_pretty ? " \u00b7 " + App.state.source_pretty : ""));
    stage.appendChild(s);
    var strip = mk("div", "strip");
    files.slice(0, 120).forEach(function (f) { var i = mk("img"); i.loading = "lazy"; i.alt = f.name; i.src = thumb(f.name, 160); strip.appendChild(i); });
    stage.appendChild(strip);
  }
  var nav = mk("div", "nav");
  var clear = mk("button", "btn", "Clear all"); clear.type = "button"; clear.disabled = !files.length;
  clear.addEventListener("click", function () {
    api("/api/reset", {}).then(refreshState).then(function () { App.funnel = null; App.saved = null; render(); toast("Cleared.", "good"); })
      .catch(function (e) { toast(e.message, "bad"); });
  });
  var next = mk("button", "btn btn-p btn-big", "Continue: fix sideways photos \u2192"); next.type = "button"; next.disabled = !files.length;
  next.addEventListener("click", function () {
    var job = App.state.straightened ? Promise.resolve()
      : api("/api/straighten", { margin: P("straighten_margin") }).then(waitJob).then(function (p) { toast(p.message, "good"); return refreshState(); });
    job.then(function () { App.funnel = null; go(2); }).catch(function (e) { toast(e.message, "bad"); });
  });
  nav.appendChild(clear); nav.appendChild(next);
  stage.appendChild(nav);
}
function upload(list) {
  var files = Array.prototype.slice.call(list || []);
  if (!files.length) return;
  var fd = new FormData();
  files.forEach(function (f) { fd.append("photos", f, f.name); });
  toast("Adding " + files.length + " photo(s)\u2026");
  api("/api/upload", fd).then(function (d) {
    toast("Added " + d.stored + "." + (d.skipped.length ? " Skipped " + d.skipped.length + ": " + d.skipped[0] : ""), d.stored ? "good" : "bad");
    return refreshState();
  }).then(function () { App.funnel = null; render(); }).catch(function (e) { toast(e.message, "bad"); });
}

/* ---- 2 straighten ---- */
function renderStraighten(stage) {
  stage.appendChild(head(2, "Fix sideways photos"));
  var dyn = mk("div"); dyn.id = "dyn"; stage.appendChild(dyn);
  var panel = paramsPanel(2, "Orientation");
  var again = mk("button", "btn btn-sm", "Check orientation again"); again.type = "button";
  again.addEventListener("click", function () {
    var v = Number(panel.querySelector("input[type=range]").value);
    api("/api/straighten", { margin: v }).then(waitJob).then(function (p) { toast(p.message, "good"); return refreshState(); })
      .then(function () { App.funnel = null; renderDynamic(); }).catch(function (e) { toast(e.message, "bad"); });
  });
  panel.querySelector(".params-h").appendChild(again);
  stage.insertBefore(panel, dyn);
  stage.insertBefore(tallyHolder(), panel);
  dynStraighten(dyn);
  stage.appendChild(navRow(2, "Next: remove duplicates \u2192"));
}
function tallyHolder() { var t = mk("div"); t.id = "tallySlot"; return t; }
function dynStraighten(dyn) {
  var files = App.state.files, turned = files.filter(function (f) { return f.angle; });
  var slot = $("tallySlot");
  if (slot) { slot.innerHTML = ""; slot.appendChild(tally(files.length, 0, turned.length + " rotated upright, none removed. Quarter turns only.")); }
  var list = App.showAllRot ? files : turned;
  var top = mk("div", "sect");
  top.appendChild(document.createTextNode(App.showAllRot ? "All frames" : "Rotated frames"));
  top.appendChild(mk("span", "cnt", list.length));
  var tog = mk("button", "linkbtn", App.showAllRot ? "Show only rotated" : "Show all " + files.length + " to rotate by hand");
  tog.type = "button";
  tog.addEventListener("click", function () { App.showAllRot = !App.showAllRot; renderDynamic(); });
  top.appendChild(tog);
  dyn.appendChild(top);
  if (!list.length) { dyn.appendChild(emptyBox("Nothing needed rotating", "Every photo with a face was already upright. Show all frames to rotate one by hand.")); return; }
  var g = mk("div", "grid");
  var names = list.map(function (f) { return f.name; });
  list.slice(0, App.expanded.rot ? list.length : 48).forEach(function (f) {
    var rot = mk("div", "rot");
    [["\u21BA", 270, "Rotate left"], ["\u21BB", 90, "Rotate right"]].forEach(function (b) {
      var x = mk("button", null, b[0]); x.type = "button"; x.title = b[2]; x.setAttribute("aria-label", b[2] + " " + f.name);
      x.addEventListener("click", function () {
        api("/api/rotate", { name: f.name, angle: b[1] }).then(function (d) {
          App.state.files = d.files; App.state.scan_needed = true; App.funnel = null; renderDynamic();
        }).catch(function (e) { toast(e.message, "bad"); });
      });
      rot.appendChild(x);
    });
    g.appendChild(card(f.name, { pin: f.angle ? f.angle + "\u00b0" : null, pick: false, state: false, extra: rot, list: names, label: f.dims }));
  });
  dyn.appendChild(g);
  if (list.length > 48 && !App.expanded.rot) {
    var more = mk("button", "btn btn-sm more", "Show all " + list.length); more.type = "button";
    more.addEventListener("click", function () { App.expanded.rot = true; renderDynamic(); });
    dyn.appendChild(more);
  }
}

/* ---- 3-11 ---- */
var STEP_TITLES = { 3: "Remove duplicates", 4: "Exposure problems", 5: "Focus problems", 6: "Subject and faces",
  7: "Expressions", 8: "Grain and clarity", 9: "Narrow to the strongest", 10: "Group by people", 11: "One photo per moment" };
var NEXT = { 3: "exposure", 4: "focus", 5: "subject and faces", 6: "expressions", 7: "grain", 8: "narrow",
  9: "group by people", 10: "one per moment", 11: "the best photo" };

function renderStep(stage, n) {
  stage.appendChild(head(n, STEP_TITLES[n]));
  stage.appendChild(tallyHolder());
  if (n === 7) {
    var nt = mk("div", "notice"); nt.appendChild(mk("span", "lab", "NOT AVAILABLE"));
    nt.appendChild(mk("span", null, "Expression and closed-eye detection need models that are not installed. This step passes every photo through unchanged."));
    stage.appendChild(nt);
  }
  var panel = paramsPanel(n);
  if (panel) stage.appendChild(panel);
  var dyn = mk("div"); dyn.id = "dyn"; stage.appendChild(dyn);
  dynStep(dyn, n);
  paintHits(n);
  stage.appendChild(navRow(n, "Next: " + NEXT[n] + " \u2192"));
}

function removedAt(n) {
  return App.funnel.photos.filter(function (p) { return p.removed_by === n; }).map(function (p) { return p.name; });
}
var WHY = {
  3: "Near-identical frames. The kept frame is marked green.",
  4: "Light that cannot be fixed in editing.",
  5: "Softness, judged against the rest of this batch.",
  6: "Faults about the people in the frame.",
  7: "Passes through unchanged.",
  8: "Removes nothing unless a check is switched on.",
  9: "Nothing here is broken \u2014 only weaker than the rest.",
  10: "A sorting step. Nothing goes unless you switch a band off.",
  11: "Quotas filled one moment at a time."
};
function dynStep(dyn, n) {
  var f = App.funnel, s = f.steps[n - 1];
  var slot = $("tallySlot");
  if (slot) {
    slot.innerHTML = "";
    var why = WHY[n];
    if (n === 3 && f.failures.length) why += " " + f.failures.length + " unreadable file(s) excluded.";
    if (n === 11) why = f.moments + " distinct moments among " + s.before + " frames.";
    if (!s.available && s.note && n !== 7) why = "Skipped: " + s.note + ".";
    slot.appendChild(tally(s.before, s.removed.length, why));
  }

  if (n === 3) dynPairs(dyn);
  if (n === 10) dynBands(dyn);
  if (n === 11) {
    dyn.appendChild(section("Selects", f.selects.length));
    if (f.selects.length) {
      dyn.appendChild(cardGrid(f.selects, {
        wide: true, w: 560,
        why: function (nm) { var p = App.by[nm]; return p.faces >= 0 ? (p.faces + (p.faces === 1 ? " face" : " faces") + " \u00b7 " + p.band) : "moment " + p.group; }
      }, "sel", 24));
    } else dyn.appendChild(emptyBox("No selects", "Every band quota is 0 \u2014 raise one at step 10."));
  }

  var gone = removedAt(n);
  if (n !== 3) {
    dyn.appendChild(section(n === 11 ? "Not chosen" : "Removed here", gone.length));
    if (gone.length) {
      dyn.appendChild(cardGrid(gone, { cut: true }, "gone" + n, 24));
    } else {
      dyn.appendChild(emptyBox("Nothing removed here", n === 8 ? "Every grain check is switched off." : "No photo reaching this step fails its checks."));
    }
  }
  dyn.appendChild(pile(n));
}

function dynPairs(dyn) {
  var pairs = App.funnel.pairs;
  dyn.appendChild(section("Duplicate groups", pairs.length));
  if (!pairs.length) {
    dyn.appendChild(emptyBox("No duplicates found", "No two frames are alike enough at this threshold. Raise it if you expected some."));
    return;
  }
  var box = mk("div", "pairs");
  var shown = App.expanded.pairs ? pairs : pairs.slice(0, 20);
  shown.forEach(function (pr) {
    var row = mk("div", "pair");
    var all = [pr.keeper].concat(pr.removed.map(function (r) { return r.name; }));
    function mini(name, keep, note) {
      var m = mk("div", "mini " + (keep ? "keep" : "gone"));
      var img = mk("img"); img.loading = "lazy"; img.alt = name; img.src = thumb(name, 240);
      img.addEventListener("click", function () { openLb(all, name); });
      m.appendChild(img);
      m.appendChild(mk("div", "c", (keep ? "kept \u00b7 " : "") + note));
      if (!keep) m.appendChild(pickBtn(name));
      return m;
    }
    row.appendChild(mini(pr.keeper, true, App.by[pr.keeper].score));
    pr.removed.forEach(function (r) { row.appendChild(mini(r.name, false, "d " + r.distance + " \u00b7 " + App.by[r.name].score)); });
    box.appendChild(row);
  });
  dyn.appendChild(box);
  if (pairs.length > 20) {
    var b = mk("button", "btn btn-sm more", App.expanded.pairs ? "Show fewer" : "Show all " + pairs.length); b.type = "button";
    b.addEventListener("click", function () { App.expanded.pairs = !App.expanded.pairs; renderDynamic(); });
    dyn.appendChild(b);
  }
}

function dynBands(dyn) {
  var f = App.funnel, bands = f.bands;
  var box = mk("div", "params");
  var h = mk("div", "params-h"); h.appendChild(mk("h3", null, "Selects from each band"));
  box.appendChild(h);
  var b = mk("div", "params-b");
  var total = 0;
  BANDS.forEach(function (pair) {
    var key = pair[0], info = bands[key];
    var row = mk("div", "bandrow" + (info.on ? "" : " off"));
    var ck = mk("input", "ck"); ck.type = "checkbox"; ck.checked = info.on; ck.setAttribute("aria-label", pair[1]);
    ck.addEventListener("change", function () { setParam("band_" + key + "_on", ck.checked); });
    row.appendChild(ck);
    row.appendChild(mk("span", "bn2", pair[1]));
    row.appendChild(mk("span", "bc", info.count + " here"));
    var c = mk("div", "prm-c");
    var sl = mk("input"); sl.type = "range"; sl.min = 0; sl.max = Math.max(info.count, info.quota, 1); sl.step = 1;
    sl.value = info.quota; sl.disabled = !info.on; sl.setAttribute("aria-label", pair[1] + " quota");
    var v = mk("span", "prm-v", info.quota);
    sl.addEventListener("input", function () { v.textContent = sl.value; });
    sl.addEventListener("change", function () { setParam("q_" + key, Number(sl.value)); });
    c.appendChild(sl); c.appendChild(v); row.appendChild(c);
    b.appendChild(row);
    if (info.on) total += info.quota;
  });
  var q = mk("div", "qsum" + (total ? "" : " bad"),
    total ? "Quotas total " + total + " select" + (total === 1 ? "" : "s") + ". A band short of its quota passes the rest to the strongest remaining frames."
          : "Every quota is 0 \u2014 the next step would have nothing to choose.");
  b.appendChild(q);
  box.appendChild(b);
  dyn.appendChild(box);
  if (!f.faces_probed) dyn.appendChild(emptyBox("Faces were not checked", "The face model is missing, so every photo is unbanded and the quotas apply as one total."));

  BANDS.forEach(function (pair) {
    var info = bands[pair[0]];
    if (!info.count) return;
    var here = f.photos.filter(function (p) { return p.band === pair[0] && (p.removed_by === null || p.removed_by >= 10); })
      .map(function (p) { return p.name; });
    dyn.appendChild(section(pair[1], info.count + " \u00b7 quota " + info.quota));
    dyn.appendChild(cardGrid(here, { why: function (nm) { var p = App.by[nm]; return p.faces + (p.faces === 1 ? " face" : " faces"); } }, "band" + pair[0], 8));
  });
}

/* ---- 12 the best + export ---- */
function renderBest(stage) {
  stage.appendChild(head(12, "The best photo"));
  var dyn = mk("div"); dyn.id = "dyn"; stage.appendChild(dyn);
  dynBest(dyn);
  stage.appendChild(navRow(12, null));
}
function bars(bd) {
  var w = mk("div", "bars");
  [["sharpness", "Sharpness"], ["exposure", "Exposure"], ["contrast", "Contrast"], ["saturation", "Colour"], ["noise", "Low noise"]].forEach(function (k) {
    if (!(k[0] in bd)) return;
    var v = Math.round(bd[k[0]] * 100);
    var r = mk("div", "bar"); r.appendChild(mk("span", null, k[1]));
    var t = mk("span", "t"), f = mk("span", "f" + (v >= 70 ? " hi" : v <= 35 ? " lo" : "")); f.style.width = v + "%";
    t.appendChild(f); r.appendChild(t); r.appendChild(mk("span", "v", v)); w.appendChild(r);
  });
  return w;
}
function faultList(p) {
  var ul = mk("ul", "flist");
  if (!p.faults.length) { var li = mk("li"); li.appendChild(mk("span", "p")); li.appendChild(mk("span", null, "Nothing wrong found.")); ul.appendChild(li); }
  p.faults.forEach(function (f) {
    var li = mk("li"); li.appendChild(mk("span", "p " + f.severity));
    var t = mk("span"); var b = mk("b", null, f.label + ". "); t.appendChild(b); t.appendChild(document.createTextNode(f.detail + (f.enabled ? "" : " (check off)")));
    li.appendChild(t); ul.appendChild(li);
  });
  return ul;
}
function dynBest(dyn) {
  var f = App.funnel;
  if (!f.best) { dyn.appendChild(emptyBox("No photo made it through", "Raise a band quota at step 10, or loosen an earlier step.")); }
  else {
    var p = App.by[f.best];
    var win = mk("div", "winner");
    var hero = mk("div", "hero"); var img = mk("img"); img.alt = p.name; img.src = thumb(p.name, 1600);
    hero.appendChild(img); hero.addEventListener("click", function () { openLb(f.selects, p.name); });
    win.appendChild(hero);
    var side = mk("div");
    var bs = mk("div", "bs", p.score); bs.appendChild(mk("small", null, " / 100")); side.appendChild(bs);
    side.appendChild(mk("div", "fn", p.name));
    side.appendChild(mk("div", "faint", [p.dims, p.shape, p.faces >= 0 ? p.faces + " face(s)" : null, "best of " + f.total].filter(Boolean).join(" \u00b7 ")));
    side.appendChild(faultList(p)); side.appendChild(bars(p.breakdown)); side.appendChild(pickBtn(p.name));
    win.appendChild(side);
    dyn.appendChild(win);
  }

  dyn.appendChild(section("Selects", f.selects.length));
  dyn.appendChild(cardGrid(f.selects, { pin: null, why: function (nm) { return "#" + App.by[nm].pick_rank; } }, "sel12", 24));

  dyn.appendChild(section("How it got here"));
  var j = mk("ul", "journey");
  f.steps.forEach(function (s) {
    var li = mk("li" , s.available ? "" : "na");
    li.appendChild(mk("span", "n", s.n));
    li.appendChild(mk("span", null, s.name + (s.available ? "" : " \u2014 " + s.note)));
    li.appendChild(mk("span", "d", s.n >= 3 && s.removed.length ? "\u2212" + s.removed.length : ""));
    li.appendChild(mk("span", "c", s.after));
    li.addEventListener("click", function () { go(s.n); });
    j.appendChild(li);
  });
  var mp = mk("li"); mp.appendChild(mk("span", "n", "\uD83D\uDCCC")); mp.appendChild(mk("span", null, "Manually selected by you"));
  mp.appendChild(mk("span", "d", "")); mp.appendChild(mk("span", "c", picks().length)); j.appendChild(mp);
  dyn.appendChild(j);

  if (picks().length) {
    dyn.appendChild(section("Your manual selections", picks().length));
    dyn.appendChild(cardGrid(picks(), { why: function (nm) { var q = App.by[nm]; return q && q.removed_by ? "removed at " + STEP_NAMES[q.removed_by].toLowerCase() : "also a select"; } }, "picks", 24));
  }

  dyn.appendChild(exportPanel());
  dyn.appendChild(pile(13));
}

/* ------------------------------------------------------------ export */
var INCLUDES = [
  ["selects", "The numbered selects"], ["manual", "Your manual selections"],
  ["notes", "why_these_photos.txt"], ["csv", "ranking.csv"],
  ["rejected", "Rejected frames, by step"], ["vertical", "9:16 vertical versions"]
];
function currentDest() {
  if (!App.dests) return null;
  var key = App.dest.key;
  var d = App.dests.destinations.filter(function (x) { return x.key === key; })[0];
  return d || App.dests.destinations[0];
}
function destBase() {
  var d = currentDest();
  if (!d) return "";
  return d.key === "custom" ? App.dest.custom : d.path;
}
function exportPanel() {
  var box = mk("div", "params");
  box.id = "exportBox";
  var h = mk("div", "params-h"); h.appendChild(mk("h3", null, "Save the selected photos"));
  h.appendChild(infoBtn("Saving", "export", "<p>Copies go to the folder you choose. <b>Nothing is ever overwritten</b>: if the folder exists, saving stops and offers the next free name.</p><p>The funnel's selects are numbered in score order. Your manual picks go to <code>Manually_Selected/</code> with their original names. Photos are written upright, as straightened at step 2.</p>"));
  box.appendChild(h);
  var b = mk("div", "params-b"); b.id = "exportBody";
  b.appendChild(mk("p", "faint", "Loading destinations\u2026"));
  box.appendChild(b);
  var load = App.dests ? Promise.resolve(App.dests) : api("/api/destinations");
  load.then(function (d) { App.dests = d; paintExport(); }).catch(function (e) { toast(e.message, "bad"); });
  return box;
}
function paintExport() {
  var b = $("exportBody"); if (!b) return;
  b.innerHTML = "";
  var dests = App.dests.destinations;
  if (!App.dest.key) { var sug = dests.filter(function (x) { return x.suggested; })[0] || dests[0]; App.dest.key = sug.key; }
  var d = currentDest();
  if (!App.dest.touched) App.dest.name = d.names[0] || "BestImages_v1";

  var row1 = mk("div", "prm"); row1.style.alignItems = "start";
  var l1 = mk("div"); l1.appendChild(mk("div", "prm-name", "Where to save"));
  row1.appendChild(l1);
  var list = mk("div", "dests");
  dests.forEach(function (x) {
    var lab = mk("label", "dest" + (x.key === d.key ? " on" : ""));
    var r = mk("input"); r.type = "radio"; r.name = "dest"; r.checked = x.key === d.key;
    r.addEventListener("change", function () { App.dest.key = x.key; App.dest.touched = false; paintExport(); });
    lab.appendChild(r);
    var t = mk("span"); t.appendChild(mk("div", "l", x.label + (x.suggested ? "  \u00b7 suggested" : "")));
    t.appendChild(mk("div", "p", x.pretty || "type a folder path"));
    lab.appendChild(t); list.appendChild(lab);
  });
  row1.style.gridTemplateColumns = "1fr"; row1.appendChild(list);
  if (d.key === "custom") {
    var cp = mk("input", "txt"); cp.placeholder = "~/Pictures/Delivery"; cp.value = App.dest.custom; cp.spellcheck = false;
    cp.setAttribute("aria-label", "Destination folder");
    cp.addEventListener("change", function () { App.dest.custom = cp.value; paintPreview(); });
    cp.addEventListener("input", function () { App.dest.custom = cp.value; paintPreview(); });
    row1.appendChild(cp);
  }
  b.appendChild(row1);

  var row2 = mk("div", "prm"); row2.style.gridTemplateColumns = "1fr";
  row2.appendChild(mk("div", "prm-name", "Folder name"));
  var nm = mk("input", "txt"); nm.value = App.dest.name; nm.spellcheck = false; nm.setAttribute("aria-label", "Folder name");
  nm.addEventListener("input", function () { App.dest.name = nm.value; App.dest.touched = true; paintSugg(); paintPreview(); });
  row2.appendChild(nm);
  var sg = mk("div", "sugg"); sg.id = "sugg"; row2.appendChild(sg);
  var warn = mk("div", "warn"); warn.id = "nameWarn"; warn.hidden = true; row2.appendChild(warn);
  b.appendChild(row2);

  var row3 = mk("div", "prm"); row3.style.gridTemplateColumns = "1fr";
  row3.appendChild(mk("div", "prm-name", "Include"));
  var inc = mk("div", "incl");
  INCLUDES.forEach(function (it) {
    var lab = mk("label"); var c = mk("input", "ck"); c.type = "checkbox"; c.checked = !!App.dest.include[it[0]];
    c.addEventListener("change", function () { App.dest.include[it[0]] = c.checked; paintPreview(); });
    lab.appendChild(c); lab.appendChild(document.createTextNode(it[1])); inc.appendChild(lab);
  });
  row3.appendChild(inc);
  b.appendChild(row3);

  var row4 = mk("div", "prm"); row4.style.gridTemplateColumns = "1fr";
  var pb = mk("div", "pathbox"); pb.id = "pathbox"; row4.appendChild(pb);
  var acts = mk("div"); acts.style.display = "flex"; acts.style.gap = "10px"; acts.style.flexWrap = "wrap";
  var save = mk("button", "btn btn-p btn-big", "Save"); save.type = "button"; save.id = "saveBtn";
  save.addEventListener("click", doExport);
  var csv = mk("a", "btn btn-big", "Download ranking CSV"); csv.href = "/api/ranking.csv";
  acts.appendChild(save); acts.appendChild(csv); row4.appendChild(acts);
  if (App.saved) {
    var sv = mk("div", "saved");
    sv.appendChild(document.createTextNode("Saved to "));
    sv.appendChild(mk("code", null, App.saved.pretty));
    sv.appendChild(document.createTextNode(" \u2014 " + App.saved.photos + " photo(s)" + (App.saved.errors.length ? ", " + App.saved.errors.length + " problem(s)" : "") + "."));
    row4.appendChild(sv);
  }
  b.appendChild(row4);
  paintSugg(); paintPreview();
}
function paintSugg() {
  var sg = $("sugg"); if (!sg) return;
  sg.innerHTML = "";
  (currentDest().names || []).forEach(function (n) {
    var x = mk("button", n === App.dest.name ? "on" : "", n); x.type = "button";
    x.addEventListener("click", function () { App.dest.name = n; App.dest.touched = true; paintExport(); });
    sg.appendChild(x);
  });
}
function paintPreview() {
  var d = currentDest(), pb = $("pathbox"), warn = $("nameWarn"), save = $("saveBtn");
  if (!pb) return;
  var name = (App.dest.name || "").trim();
  var base = destBase() || "\u2026";
  var exists = d.key !== "custom" && (d.existing || []).indexOf(name) >= 0;
  warn.hidden = !exists;
  warn.textContent = exists ? name + " already exists here. Nothing will be overwritten \u2014 pick another name." : "";
  var f = App.funnel, inc = App.dest.include, lines = [];
  var full = base.replace(/\/+$/, "") + "/" + (name || "\u2026") + "/";
  pb.innerHTML = "";
  pb.appendChild(mk("span", "hl", full)); pb.appendChild(document.createTextNode("\n"));
  if (inc.selects) f.selects.forEach(function (n, i) { lines.push("  " + (i < 9 ? "0" : "") + (i + 1) + "_" + n); });
  if (inc.manual) lines.push("  Manually_Selected/      " + picks().length + " photo(s)");
  if (inc.vertical) lines.push("  Vertical/               " + f.selects.length + " at 1080\u00d71920");
  if (inc.rejected) lines.push("  Rejected/               " + f.photos.filter(function (p) { return p.removed_by; }).length + " photo(s), by step");
  if (inc.notes) lines.push("  why_these_photos.txt");
  if (inc.csv) lines.push("  ranking.csv");
  pb.appendChild(document.createTextNode(lines.join("\n")));
  var count = (inc.selects ? f.selects.length : 0) + (inc.manual ? picks().length : 0);
  save.textContent = "Save " + count + " photo" + (count === 1 ? "" : "s") + " to " + (name || "\u2026");
  save.disabled = !name || exists || (d.key === "custom" && !App.dest.custom.trim());
}
function doExport() {
  api("/api/export", { path: destBase(), name: App.dest.name, include: App.dest.include })
    .then(waitJob)
    .then(function (p) {
      toast(p.message, "good");
      return Promise.all([refreshState(), api("/api/destinations")]);
    })
    .then(function (r) { App.saved = r[0].last_export; App.dests = r[1]; App.dest.touched = false; paintExport(); })
    .catch(function (e) {
      var s = e.data && e.data.suggestion;
      toast(e.message + (s ? " Try " + s + "." : ""), "bad");
      if (s) { App.dest.name = s; App.dest.touched = true; paintExport(); }
    });
}

/* ------------------------------------------------------------ lightbox */
function openLb(list, name) {
  App.lb.list = list && list.length ? list : [name];
  App.lb.at = Math.max(0, App.lb.list.indexOf(name));
  $("lightbox").hidden = false;
  document.body.style.overflow = "hidden";
  showLb();
  $("lbClose").focus();
}
function closeLb() {
  if ($("lightbox").hidden) return;
  $("lightbox").hidden = true; $("lbImg").src = ""; document.body.style.overflow = "";
}
function stepLb(d) {
  var n = App.lb.list.length; if (!n) return;
  App.lb.at = (App.lb.at + d + n) % n; showLb();
}
function showLb() {
  var name = App.lb.list[App.lb.at], p = App.by[name];
  $("lbName").textContent = name;
  $("lbRank").textContent = p ? "#" + p.rank + " of " + App.funnel.total : (App.lb.at + 1) + " / " + App.lb.list.length;
  $("lbImg").src = thumb(name, 2000); $("lbImg").alt = name;
  var side = $("lbSide"); side.innerHTML = "";
  if (!p) { var fi = fileInfo(name); side.appendChild(mk("div", null, fi ? fi.dims : "")); return; }
  var bs = mk("div", "bs", p.score); bs.appendChild(mk("small", null, " / 100")); side.appendChild(bs);
  side.appendChild(mk("div", null, [p.dims, p.shape, p.faces >= 0 ? p.faces + " face(s), " + p.band : null].filter(Boolean).join(" \u00b7 ")));
  side.appendChild(mk("h4", null, "Status"));
  side.appendChild(mk("div", null, p.removed_by ? "Removed at step " + p.removed_by + " (" + STEP_NAMES[p.removed_by] + "): " + p.reason
    : p.pick_rank ? "Select #" + p.pick_rank + (App.funnel.best === name ? " \u2014 the best photo" : "") : "In the funnel"));
  side.appendChild(pickBtn(name));
  side.appendChild(mk("h4", null, "Faults")); side.appendChild(faultList(p));
  side.appendChild(mk("h4", null, "Measurements")); side.appendChild(bars(p.breakdown));
}
$("lbClose").addEventListener("click", closeLb);
$("lbPrev").addEventListener("click", function () { stepLb(-1); });
$("lbNext").addEventListener("click", function () { stepLb(1); });
document.addEventListener("keydown", function (e) {
  if ($("lightbox").hidden) return;
  if (e.key === "ArrowLeft") stepLb(-1);
  if (e.key === "ArrowRight") stepLb(1);
});
$("picksChip").addEventListener("click", function () { if (App.funnel) go(12); });

/* ------------------------------------------------------------ start */
refreshState().then(function (s) {
  var p = s.progress && s.progress.running ? waitJob().then(refreshState) : Promise.resolve();
  return p.then(function () { return api("/api/funnel"); });
}).then(function (f) {
  applyFunnel(f);
  var fallback = App.funnel ? 12 : (App.state.files.length ? (App.state.straightened ? 2 : 1) : 1);
  var wanted = parseInt((location.hash || "").replace("#", ""), 10);
  if (wanted >= 1 && wanted <= 12 && (wanted <= 2 || App.funnel)) fallback = wanted;
  App.step = fallback;
  render();
}).catch(function (e) { render(); toast(e.message, "bad"); });
