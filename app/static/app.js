"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  box: "",          // box number as typed ("" for a box without a label)
  colour: "",       // label colour ("" when the app isn't set up with colours)
  boxId: null,      // Homebox id, used for boxes without a label
  labelPhoto: null, // {blob, url} from "Photograph the label", added to the box's photos
  labelling: null,  // {id, boxName} while giving a box a (new) label
  mode: "create",   // "create" (Add a box) or "alter" (Change a box)
  viewBack: "stepHome", // where Back on the box page goes
  boxInfo: null,    // response from /api/box
  photos: [],       // [{blob, url}]
  draft: null,      // response from /api/analyze
};
const PHOTO_EDGE = 2000; // resize before upload: plenty for Homebox, much faster on mobile data

// ---------- helpers ----------
let currentStep = "stepHome";
function show(step, push = true) {
  for (const s of document.querySelectorAll(".step")) s.hidden = s.id !== step;
  if (step === "stepHome" || step === "stepFind") $("boxBadge").hidden = true;
  if (push && step !== currentStep) history.pushState({ step }, "");
  currentStep = step;
  window.scrollTo(0, 0);
}
window.addEventListener("popstate", (e) => {
  stopMic();
  const step = e.state?.step || "stepHome";
  // Don't step back into a half-finished save; go home instead.
  show(["stepReview", "stepCapture"].includes(step) && currentStep === "stepDone" ? "stepHome" : step, false);
});
function goHome() { stopMic(); if (state.labelling) setLabellingMode(false); show("stepHome"); }
function setMode(mode) {
  state.mode = mode;
  const alter = mode === "alter";
  $("boxStepTitle").textContent = alter ? "Change a box" : "Add a box";
  $("noLabelBtn").hidden = alter;
  $("labelLaterBtn").textContent = alter ? "Pick an unlabelled box" : "Label an unlabelled box";
  $("startBtn").textContent = alter ? "Open" : "Start";
  $("boxNumber").value = "";
  resetLabelResult();
  show("stepBox");
}
function busy(text) { $("busyText").textContent = text || "Working…"; $("busy").hidden = !text; }
let toastTimer;
function toast(msg, error = false) {
  const t = $("toast"); t.textContent = msg; t.hidden = false; t.classList.toggle("error", error);
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 6000);
}
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  let body = null;
  try { body = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) throw new Error((body && body.detail) || `Request failed (${r.status})`);
  return body;
}
function store(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ } }
function load(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } }
function forget(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } }
const draftKey = () => `box-draft-${state.boxId || [state.colour, state.box].filter(Boolean).join("-") || "new-unlabelled"}`;
function saveDraft() {
  store(draftKey(), { transcript: $("transcript").value, items: state.draft ? collectItems() : null,
                      summary: state.draft?.box_summary || "" });
}
function escapeHtml(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

// ---------- step 1: box ----------
function renderColours() {
  const colours = window.APP_CONFIG?.colours || [];
  $("colourField").hidden = !colours.length;
  const pick = $("colourPick");
  pick.innerHTML = "";
  for (const c of colours) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "colour-chip";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(c.name === state.colour));
    b.innerHTML = `<span class="swatch"></span><span></span>`;
    b.querySelector(".swatch").style.background = c.css;
    b.lastChild.textContent = c.name;
    b.onclick = () => { state.colour = c.name; store("last-colour", c.name); renderColours(); };
    pick.appendChild(b);
  }
}
function boxQuery() { return state.colour ? `?colour=${encodeURIComponent(state.colour)}` : ""; }
function needsColour() { return (window.APP_CONFIG?.colours || []).length > 0; }
function addBoxFields(fd) {
  if (state.boxId) fd.append("box_id", state.boxId);
  else if (state.box) { fd.append("box_number", state.box); fd.append("colour", state.colour); }
}

// Start a box by its label (colour + number typed or read from the photo)
async function startBox(number) {
  number = String(number || "").trim();
  if (needsColour() && !state.colour) { toast("Pick the label colour first."); return; }
  if (!number) { toast("Enter the box number first."); return; }
  if (state.labelling) return saveLabel(number);
  busy("Checking Homebox…");
  let info;
  try { info = await api(`/api/box/${encodeURIComponent(number)}${boxQuery()}`); }
  catch (e) { busy(); toast(e.message, true); return; }
  busy();
  if (state.mode === "alter") {
    if (!info.exists) { toast(`${info.boxName} isn't in Homebox yet. Use “Add a box” to add it.`); return; }
    state.box = number; state.boxId = null;
    resetLabelResult();
    openView(info, "stepBox");
    return;
  }
  state.box = number; state.boxId = null;
  store("last-box", { number, colour: state.colour });
  openBox(info);
}

// Open a box we know by its Homebox id (boxes without a label)
async function startBoxById(id, toView = false, back = "stepHome") {
  busy("Checking Homebox…");
  let info;
  try { info = await api(`/api/box-by-id/${encodeURIComponent(id)}`); }
  catch (e) { busy(); toast(e.message, true); return; }
  busy();
  state.box = ""; state.boxId = id;
  if (toView) openView(info, back); else openBox(info);
}

function startUnlabelled() {
  state.box = ""; state.boxId = null;
  openBox({ boxName: "New box without a label", exists: false, unlabelled: true, items: [] });
}

function openBox(info) {
  state.boxInfo = info;
  $("boxBadge").textContent = info.boxName;
  $("boxBadge").hidden = false;

  const note = $("existingNote");
  if (info.exists && info.items.length) {
    const list = info.items.slice(0, 8).map((i) => `<li>${escapeHtml(i.name)} ×${i.quantity}</li>`).join("");
    const more = info.items.length > 8 ? `<li>…and ${info.items.length - 8} more</li>` : "";
    note.innerHTML = `<strong>${escapeHtml(info.boxName)} already has ${info.items.length} item(s).</strong>
      Anything you add now is added to it; Claude will skip what's already recorded.<ul>${list}${more}</ul>`;
    note.hidden = false;
  } else if (info.exists) {
    note.textContent = `${info.boxName} exists in Homebox but has no items yet.`;
    note.hidden = false;
  } else if (info.unlabelled) {
    note.textContent = "It will be saved as the next “Unlabelled” box. Once it has a label, use “Label an unlabelled box” to give it its number.";
    note.hidden = false;
  } else {
    note.hidden = true;
  }

  clearPhotos();
  if (state.labelPhoto) { state.photos.push(state.labelPhoto); state.labelPhoto = null; renderPhotos(); }
  resetLabelResult();
  state.draft = null;
  const saved = load(draftKey());
  $("transcript").value = saved?.transcript || "";
  show("stepCapture");
}

// ---------- step 1: read the label from a photo ----------
function resetLabelResult() {
  $("labelResult").hidden = true;
  $("unlabelledBtn").hidden = true;
  if (state.labelPhoto) { URL.revokeObjectURL(state.labelPhoto.url); state.labelPhoto = null; }
}
async function readLabel(file) {
  resetLabelResult();
  const blob = await resizePhoto(file);
  const fd = new FormData();
  fd.append("photo", blob, "label.jpg");
  busy("Reading the label…");
  let res;
  try { res = await api("/api/read-label", { method: "POST", body: fd }); }
  catch (e) { busy(); toast(e.message, true); return; }
  busy();
  state.labelPhoto = { blob, url: URL.createObjectURL(blob) };
  $("labelThumb").src = state.labelPhoto.url;
  const { label, box } = res;
  const panel = $("labelResult");
  panel.classList.toggle("unsure", !label.has_label || label.confidence !== "high" || (needsColour() && !label.colour));
  if (label.has_label) {
    if (label.colour) { state.colour = label.colour; store("last-colour", label.colour); renderColours(); }
    $("boxNumber").value = label.number;
    const name = box ? box.boxName : [label.colour, label.number].filter(Boolean).join(" ");
    let text = `<strong>${escapeHtml(name)}</strong>`;
    if (!state.labelling) {
      const none = state.mode === "alter" ? " · not in Homebox yet" : " · new box";
      text += box && box.exists ? ` · ${box.items.length} item(s)` : box ? none : "";
    }
    $("labelText").innerHTML = text;
    const checks = [];
    if (needsColour() && !label.colour) checks.push("Couldn't tell the label colour. Pick it below.");
    else if (label.confidence !== "high") checks.push("Not sure about this. Check the colour and number below.");
    if (label.note) checks.push(label.note);
    $("labelNote").textContent = checks.join(" ") || `Tap ${$("startBtn").textContent} if that's right.`;
  } else {
    $("labelText").innerHTML = "<strong>No label found</strong>";
    $("labelNote").textContent = label.note || "";
    $("unlabelledBtn").hidden = !!state.labelling || state.mode === "alter";
  }
  panel.hidden = false;
}

// ---------- step 1b: boxes without a label ----------
async function showUnlabelled() {
  busy("Loading…");
  let res;
  try { res = await api("/api/unlabelled"); }
  catch (e) { busy(); toast(e.message, true); return; }
  busy();
  const list = $("unlabelledList");
  list.innerHTML = res.boxes.length ? "" : '<p class="hint">No unlabelled boxes. Everything has a label.</p>';
  for (const b of res.boxes) {
    const div = document.createElement("div");
    div.className = "ubox";
    div.innerHTML = `<h3></h3><p></p><div class="row"><button class="secondary" type="button">Open</button>
      <button class="primary" type="button">Give it a label</button></div>`;
    div.querySelector("h3").textContent = b.boxName;
    div.querySelector("p").textContent = b.itemCount
      ? b.items.join(", ") + (b.itemCount > b.items.length ? ` and ${b.itemCount - b.items.length} more` : "")
      : "No items yet";
    const [open, label] = div.querySelectorAll("button");
    open.onclick = () => startBoxById(b.id, true, "stepUnlabelled");
    label.onclick = () => startLabelling(b);
    list.appendChild(div);
  }
  show("stepUnlabelled");
}
function startLabelling(b) {
  state.labelling = { id: b.id, boxName: b.boxName };
  $("labellingNote").innerHTML = b.unlabelled === false
    ? `New label for <strong>${escapeHtml(b.boxName)}</strong>. Photograph the new label or type it in.`
    : `Giving <strong>${escapeHtml(b.boxName)}</strong> its label. Photograph the new label or type it in.`;
  setLabellingMode(true);
  show("stepBox");
}
function setLabellingMode(on) {
  if (!on) state.labelling = null;
  $("labellingNote").hidden = !on;
  $("otherActions").hidden = on;
  $("cancelLabelling").hidden = !on;
  $("startBtn").textContent = on ? "Save label" : state.mode === "alter" ? "Open" : "Start";
  $("boxNumber").value = "";
  resetLabelResult();
  $("boxBadge").hidden = true;
}
async function saveLabel(number) {
  const fd = new FormData();
  fd.append("box_number", number);
  fd.append("colour", state.colour);
  busy("Saving the label…");
  let info;
  try { info = await api(`/api/box-by-id/${encodeURIComponent(state.labelling.id)}/label`, { method: "POST", body: fd }); }
  catch (e) { busy(); toast(e.message, true); return; }
  busy();
  const old = state.labelling.boxName;
  forget(`box-draft-${state.labelling.id}`);
  const photo = state.labelPhoto; state.labelPhoto = null;
  setLabellingMode(false);
  toast(`${old} is now ${info.boxName}.`);
  state.box = number; state.boxId = null;
  store("last-box", { number, colour: state.colour });
  if (state.mode === "alter") {
    if (photo) URL.revokeObjectURL(photo.url);
    openView(info, "stepHome");
  } else {
    state.labelPhoto = photo;
    openBox(info);
  }
}

// ---------- step 2: photos ----------
async function resizePhoto(file) {
  try {
    const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    const scale = Math.min(1, PHOTO_EDGE / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close?.();
    return await new Promise((res) => canvas.toBlob((b) => res(b || file), "image/jpeg", 0.85));
  } catch {
    return file; // browser can't decode it; send as-is and let the server deal with it
  }
}
async function addPhotos(files) {
  const max = window.APP_CONFIG?.maxPhotos || 8;
  for (const file of files) {
    if (state.photos.length >= max) { toast(`Up to ${max} photos per box.`); break; }
    const blob = await resizePhoto(file);
    state.photos.push({ blob, url: URL.createObjectURL(blob) });
  }
  renderPhotos();
}
function renderPhotos() {
  const grid = $("photoGrid");
  grid.innerHTML = "";
  state.photos.forEach((p, i) => {
    const div = document.createElement("div");
    div.className = "photo";
    div.innerHTML = `<img alt="Photo ${i + 1}"><button type="button" aria-label="Remove photo">✕</button>`;
    div.querySelector("img").src = p.url;
    div.querySelector("button").onclick = () => { URL.revokeObjectURL(p.url); state.photos.splice(i, 1); renderPhotos(); };
    grid.appendChild(div);
  });
}
function clearPhotos() { state.photos.forEach((p) => URL.revokeObjectURL(p.url)); state.photos = []; renderPhotos(); }

// ---------- step 2: voice ----------
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognizer = null, recording = false;
function setupMic() {
  if (!SR) {
    $("micBtn").hidden = true;
    $("micHint").textContent = "This browser can't transcribe speech here. Tap the box below and use your keyboard's microphone button instead.";
    return;
  }
  if (!window.isSecureContext) {
    $("micBtn").hidden = true;
    $("micHint").textContent = "Voice input needs HTTPS. Use your keyboard's microphone button for now (see README).";
    return;
  }
  $("micBtn").onclick = () => (recording ? stopMic() : startMic());
}
function startMic() {
  recognizer = new SR();
  recognizer.continuous = true;
  recognizer.interimResults = true;
  recognizer.lang = navigator.language || "en-GB";
  recognizer.onresult = (ev) => {
    let interim = "";
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      const r = ev.results[i];
      if (r.isFinal) {
        const ta = $("transcript");
        const sep = ta.value && !/\s$/.test(ta.value) ? " " : "";
        ta.value += sep + r[0].transcript.trim();
        saveDraft();
      } else {
        interim += r[0].transcript;
      }
    }
    $("interim").textContent = interim;
  };
  recognizer.onerror = (ev) => {
    if (ev.error === "not-allowed" || ev.error === "service-not-allowed") {
      toast("Microphone access was blocked. Allow it in your browser settings.");
      stopMic();
    }
  };
  // Phones stop listening after a pause; keep going until the user taps stop.
  recognizer.onend = () => { if (recording) { try { recognizer.start(); } catch { stopMic(); } } };
  try { recognizer.start(); } catch (e) { toast("Couldn't start the microphone."); return; }
  recording = true;
  $("micBtn").classList.add("recording");
  $("micBtn").setAttribute("aria-pressed", "true");
  $("micLabel").textContent = "Listening… tap to stop";
}
function stopMic() {
  recording = false;
  try { recognizer?.stop(); } catch { /* ignore */ }
  $("micBtn").classList.remove("recording");
  $("micBtn").setAttribute("aria-pressed", "false");
  $("micLabel").textContent = "Tap to talk";
  $("interim").textContent = "";
}

// ---------- step 2 -> 3: analyse ----------
async function analyse() {
  stopMic();
  const transcript = $("transcript").value.trim();
  if (!state.photos.length && !transcript) { toast("Add a photo or describe what's in the box."); return; }
  const fd = new FormData();
  addBoxFields(fd);
  fd.append("transcript", transcript);
  state.photos.forEach((p, i) => fd.append("photos", p.blob, `photo-${i + 1}.jpg`));
  busy("Claude is building the inventory… (usually under a minute)");
  try {
    state.draft = await api("/api/analyze", { method: "POST", body: fd });
  } catch (e) { busy(); toast(e.message, true); return; }
  busy();
  renderReview();
  saveDraft();
  show("stepReview");
}

// ---------- step 3: review ----------
function renderReview() {
  const d = state.draft;
  $("reviewTitle").textContent = `Check the list (${d.items.length})`;
  $("summary").textContent = d.box_summary || "";
  const q = $("questions");
  if (d.questions?.length) {
    q.innerHTML = "<strong>Worth checking:</strong><ul>" + d.questions.map((x) => `<li>${escapeHtml(x)}</li>`).join("") + "</ul>";
    q.hidden = false;
  } else q.hidden = true;
  $("items").innerHTML = "";
  d.items.forEach(addItemCard);
  if (!d.items.length) toast("Claude didn't find any new items. Add some by hand or go back.");
}
function addItemCard(item = {}) {
  const node = $("itemTpl").content.firstElementChild.cloneNode(true);
  node.querySelector(".name").value = item.name || "";
  node.querySelector(".qty").value = item.quantity || 1;
  node.querySelector(".desc").value = item.description || "";
  node.querySelector(".tags").value = (item.tags || []).join(", ");
  const meta = [];
  if (item.source) meta.push({ spoken: "from what you said", photo: "seen in a photo", both: "said and seen" }[item.source]);
  if (item.confidence === "low") { meta.push("unsure, please check"); node.classList.add("low"); }
  node.querySelector(".meta").textContent = meta.join(" · ");
  node.querySelector(".remove").onclick = () => { node.remove(); saveDraft(); };
  node.addEventListener("input", saveDraft);
  $("items").appendChild(node);
  return node;
}
function collectItems() {
  return [...document.querySelectorAll("#items .item")].map((n) => ({
    name: n.querySelector(".name").value.trim(),
    quantity: Math.max(1, parseInt(n.querySelector(".qty").value, 10) || 1),
    description: n.querySelector(".desc").value.trim(),
    tags: n.querySelector(".tags").value.split(",").map((t) => t.trim()).filter(Boolean),
  })).filter((i) => i.name);
}

// ---------- step 3 -> 4: save ----------
async function save() {
  const items = collectItems();
  if (!items.length && !state.photos.length) { toast("Nothing to save yet."); return; }
  const fd = new FormData();
  addBoxFields(fd);
  fd.append("items", JSON.stringify(items));
  fd.append("transcript", $("transcript").value.trim());
  fd.append("box_summary", state.draft?.box_summary || "");
  state.photos.forEach((p, i) => fd.append("photos", p.blob, `photo-${i + 1}.jpg`));
  busy("Saving to Homebox…");
  let res;
  try { res = await api("/api/save", { method: "POST", body: fd }); }
  catch (e) { busy(); toast(e.message, true); return; }
  busy();
  forget(draftKey());
  if (!state.box) state.boxId = res.boxId; // a new box without a label is now known by its id
  $("doneTitle").textContent = `${res.boxName} saved`;
  $("doneText").textContent = `${res.itemsSaved} item(s) and ${res.photosSaved} photo(s) added${res.created ? " to a new box" : ""}.`;
  const link = $("doneLink");
  if (res.link) { link.href = res.link; link.hidden = false; } else link.hidden = true;
  $("nextBox").textContent = nextNumber() ? `Next box (${nextNumber()})` : "Next box";
  show("stepDone");
}

// ---------- box page ----------
function openView(info, back) {
  state.boxInfo = info;
  state.boxId = info.unlabelled ? info.id : state.boxId;
  state.viewBack = back || "stepHome";
  $("boxBadge").textContent = info.boxName;
  $("boxBadge").hidden = false;
  $("viewTitle").textContent = info.boxName;
  renderViewMeta();
  $("viewItems").innerHTML = "";
  info.items.forEach(addViewItem);
  if (!info.items.length) $("viewItems").innerHTML = '<p class="hint">No items recorded yet.</p>';
  $("viewLocate").hidden = !!info.unlabelled;
  $("locateResult").hidden = true;
  $("viewLabel").textContent = info.unlabelled ? "Give it a label" : "Change label";
  show("stepView");
}
function renderViewMeta() {
  const info = state.boxInfo;
  const bits = [`${info.items.length} item(s)`, `${info.photoCount} photo(s)`];
  $("viewMeta").innerHTML = escapeHtml(bits.join(" · ")) +
    (info.link ? ` · <a href="${escapeHtml(info.link)}" target="_blank" rel="noopener">Open in Homebox</a>` : "");
}
function addViewItem(item) {
  const node = $("viewItemTpl").content.firstElementChild.cloneNode(true);
  const fill = () => {
    node.querySelector(".vitem-name").textContent = item.name;
    node.querySelector(".vitem-qty").textContent = `×${item.quantity}`;
    node.querySelector(".vitem-desc").textContent = item.description || "";
    node.querySelector(".vitem-tags").textContent = (item.tags || []).join(", ");
  };
  const edit = node.querySelector(".vitem-edit");
  const open = (on) => {
    edit.hidden = !on; node.querySelector(".vitem-main").hidden = on;
    if (on) {
      edit.querySelector(".name").value = item.name;
      edit.querySelector(".qty").value = item.quantity;
      edit.querySelector(".desc").value = item.description || "";
      edit.querySelector(".tags").value = (item.tags || []).join(", ");
      edit.querySelector(".name").focus();
    }
  };
  node.querySelector(".vitem-main").onclick = () => open(true);
  edit.querySelector(".cancel").onclick = () => open(false);
  edit.querySelector(".save").onclick = async () => {
    const fd = new FormData();
    fd.append("name", edit.querySelector(".name").value.trim());
    fd.append("quantity", edit.querySelector(".qty").value || "1");
    fd.append("description", edit.querySelector(".desc").value.trim());
    fd.append("tags", edit.querySelector(".tags").value);
    busy("Saving…");
    try { Object.assign(item, await api(`/api/item/${encodeURIComponent(item.id)}`, { method: "PUT", body: fd })); }
    catch (e) { busy(); toast(e.message, true); return; }
    busy(); fill(); open(false);
  };
  edit.querySelector(".del").onclick = async () => {
    if (!confirm(`Delete “${item.name}” from ${state.boxInfo.boxName}?`)) return;
    busy("Deleting…");
    try { await api(`/api/item/${encodeURIComponent(item.id)}`, { method: "DELETE" }); }
    catch (e) { busy(); toast(e.message, true); return; }
    busy();
    node.remove();
    state.boxInfo.items = state.boxInfo.items.filter((i) => i.id !== item.id);
    renderViewMeta();
    toast(`Deleted ${item.name}.`);
  };
  fill();
  $("viewItems").appendChild(node);
}

// ---------- find it in a photo ----------
// boxes: [{id, boxName}]. ids: the img, marks, text and result elements to fill.
async function locate(file, boxes, ids) {
  const blob = await resizePhoto(file);
  const fd = new FormData();
  fd.append("box_ids", boxes.map((b) => b.id).join(","));
  fd.append("photo", blob, "room.jpg");
  busy(boxes.length === 1 ? `Looking for ${boxes[0].boxName}…` : `Looking for ${boxes.length} boxes…`);
  let res;
  try { res = await api("/api/locate", { method: "POST", body: fd }); }
  catch (e) { busy(); toast(e.message, true); return; }
  busy();
  const img = $(ids.img);
  if (img.src) URL.revokeObjectURL(img.src);
  img.src = URL.createObjectURL(blob);
  const marks = $(ids.marks);
  marks.innerHTML = "";
  const found = res.results.filter((r) => r.found), missing = res.results.filter((r) => !r.found);
  for (const r of found) {
    const [x1, y1, x2, y2] = r.box;
    const m = document.createElement("div");
    m.className = "locate-mark" + (r.confidence === "high" ? "" : " unsure");
    Object.assign(m.style, { left: `${x1 * 100}%`, top: `${y1 * 100}%`, width: `${(x2 - x1) * 100}%`, height: `${(y2 - y1) * 100}%` });
    m.innerHTML = "<span></span>";
    m.firstChild.textContent = r.boxName.replace(/^Box\s+/i, "");
    marks.appendChild(m);
  }
  let html = "";
  if (found.length) {
    html += "<ul>" + found.map((r) => `<li><strong>${escapeHtml(r.boxName)}:</strong> ${escapeHtml(r.where)}` +
      (r.confidence === "high" ? "" : " <em>(not certain, check the label)</em>") + "</li>").join("") + "</ul>";
  }
  if (missing.length) {
    html += `<p><strong>Not in this photo:</strong> ${escapeHtml(missing.map((r) => r.boxName).join(", "))}.</p>`;
  }
  if (!found.length) {
    html += `<p>${res.seen.length ? `Labels I could read: ${escapeHtml(res.seen.join(", "))}.` : "Try a closer photo of where it might be."}</p>`;
  }
  $(ids.text).innerHTML = html;
  $(ids.result).hidden = false;
  $(ids.result).scrollIntoView({ behavior: "smooth", block: "start" });
}
const VIEW_LOCATE = { img: "locateImg", marks: "locateMarks", text: "locateText", result: "locateResult" };
const FIND_LOCATE = { img: "findLocateImg", marks: "findLocateMarks", text: "findLocateText", result: "findLocateResult" };

// ---------- find ----------
let findTimer, findSeq = 0;
const findState = { quick: [], claude: null }; // result groups shown
function highlight(text, q) {
  // Mark the query words that appear in the text (exact substrings only; fuzzy matches stay unmarked)
  let out = escapeHtml(text);
  const skip = new Set(["the", "and", "for", "some", "my", "our", "of", "in", "a", "an", "box", "boxes", "with"]);
  for (const w of q.toLowerCase().split(/\s+/).filter((w) => w.length >= 2 && !skip.has(w))) {
    const safe = escapeHtml(w).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    out = out.replace(new RegExp(`(${safe})(?![^<]*>)`, "ig"), "<mark>$1</mark>");
  }
  return out;
}
function resultCard(g, q, withWhy) {
  const b = document.createElement("button");
  b.type = "button"; b.className = "result";
  const items = g.items.slice(0, 6).map((i) => `<li class="${withWhy && i.confidence === "low" ? "unsure" : ""}">` +
    `${highlight(i.name, q)} ×${i.quantity}${i.description ? ` · ${highlight(i.description, q)}` : ""}` +
    (withWhy && i.why ? ` <span class="why">— ${escapeHtml(i.why)}</span>` : "") + "</li>").join("");
  const more = g.items.length > 6 ? `<li>…and ${g.items.length - 6} more</li>` : "";
  b.innerHTML = `<strong>${highlight(g.boxName, q)}</strong>${items || more ? `<ul>${items}${more}</ul>` : ""}`;
  b.onclick = () => startBoxById(g.boxId, true, "stepFind");
  return b;
}
function renderFind() {
  const q = $("findInput").value.trim();
  const claudeGroups = findState.claude?.results || [];
  const claudeIds = new Set(claudeGroups.flatMap((g) => g.items.map((i) => i.id)));
  // Word matches not already shown under Claude's matches
  const quick = findState.quick
    .map((g) => ({ ...g, items: g.items.filter((i) => !claudeIds.has(i.id)) }))
    .filter((g) => g.items.length || (g.boxMatches && !claudeGroups.some((c) => c.boxId === g.boxId)));

  const cOut = $("findClaudeResults");
  cOut.innerHTML = "";
  if (findState.claude) {
    cOut.insertAdjacentHTML("beforeend", '<p class="section-title">Matches by meaning</p>');
    if (!claudeGroups.length) cOut.insertAdjacentHTML("beforeend", `<p class="hint">${escapeHtml(findState.claude.note || "Claude didn't find anything that fits.")}</p>`);
    claudeGroups.forEach((g) => cOut.appendChild(resultCard(g, q, true)));
  }
  const out = $("findResults");
  out.innerHTML = "";
  if (quick.length) {
    if (findState.claude) out.insertAdjacentHTML("beforeend", '<p class="section-title">Matching your words</p>');
    quick.forEach((g) => out.appendChild(resultCard(g, q, false)));
  } else if (!findState.claude && q.length >= 2) {
    out.innerHTML = `<p class="hint">No item uses those words. Try <strong>Search by meaning</strong>.</p>`;
  }

  // Summary + "spot these boxes". After a Claude search, that's the boxes it's confident about.
  const boxes = new Map();
  const confident = claudeGroups.filter((g) => g.items.some((i) => i.confidence !== "low"));
  (findState.claude ? confident : quick).forEach((g) => boxes.set(g.boxId, g));
  const labelled = [...boxes.values()].filter((g) => !g.unlabelled).slice(0, 12);
  $("findSummary").hidden = !boxes.size;
  const names = [...boxes.values()].map((g) => g.boxName);
  $("findSummary").textContent = names.length <= 4
    ? `In ${names.length > 1 ? names.slice(0, -1).join(", ") + " and " + names.at(-1) : names[0]}`
    : `In ${names.length} boxes`;
  $("findLocate").hidden = !labelled.length;
  $("findLocateLabel").textContent = labelled.length === 1 ? `Spot ${labelled[0].boxName} in a photo` :
    `Spot these ${labelled.length} boxes in a photo`;
  findState.locateBoxes = labelled.map((g) => ({ id: g.boxId, boxName: g.boxName }));
  $("findClaude").hidden = q.length < 2 || !!findState.claude;
  $("findHint").hidden = q.length >= 2;
}
async function runSearch() {
  const q = $("findInput").value.trim();
  const seq = ++findSeq;
  findState.claude = null;
  $("findLocateResult").hidden = true;
  if (q.length < 2) findState.quick = [];
  renderFind(); // clear any previous Claude answer straight away
  if (q.length < 2) return;
  let res;
  try { res = await api(`/api/search?q=${encodeURIComponent(q)}`); }
  catch (e) { toast(e.message, true); return; }
  if (seq !== findSeq) return; // a newer search is on its way
  findState.quick = res.results;
  renderFind();
}
async function runClaudeSearch() {
  const q = $("findInput").value.trim();
  if (q.length < 2) return;
  const seq = findSeq;
  busy("Claude is looking through everything…");
  let res;
  try { res = await api(`/api/search/claude?q=${encodeURIComponent(q)}`); }
  catch (e) { busy(); toast(e.message, true); return; }
  busy();
  if (seq !== findSeq) return;
  findState.claude = res;
  renderFind();
}

// ---------- wiring ----------
function nextNumber() {
  return /^\d+$/.test(state.box) ? String(parseInt(state.box, 10) + 1) : "";
}
$("startBtn").onclick = () => startBox($("boxNumber").value);
$("labelPhoto").onchange = (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) readLabel(f); };
$("unlabelledBtn").onclick = startUnlabelled;
$("noLabelBtn").onclick = startUnlabelled;
$("labelLaterBtn").onclick = showUnlabelled;
$("backFromUnlabelled").onclick = () => show("stepBox");
$("cancelLabelling").onclick = () => {
  const back = state.mode === "alter" && state.boxInfo?.exists ? state.boxInfo : null;
  setLabellingMode(false);
  if (back) openView(back, state.viewBack); else showUnlabelled();
};
$("homeLink").onclick = (e) => { e.preventDefault(); goHome(); };
$("goCreate").onclick = () => setMode("create");
$("goAlter").onclick = () => setMode("alter");
$("goFind").onclick = () => { show("stepFind"); $("findInput").focus(); };
$("boxHome").onclick = goHome;
$("findHome").onclick = goHome;
$("doneHome").onclick = goHome;
$("findInput").addEventListener("input", () => { clearTimeout(findTimer); findTimer = setTimeout(runSearch, 300); });
$("findForm").onsubmit = async (e) => {
  e.preventDefault(); clearTimeout(findTimer); $("findInput").blur();
  await runSearch();
  if (!findState.quick.length) runClaudeSearch(); // nothing uses those words: ask Claude straight away
};
$("findClaude").onclick = runClaudeSearch;
$("findLocatePhoto").onchange = (e) => {
  const f = e.target.files[0]; e.target.value = "";
  if (f) locate(f, findState.locateBoxes, FIND_LOCATE);
};
$("viewBack").onclick = () => show(state.viewBack);
$("viewAdd").onclick = () => { state.mode = "create"; openBox(state.boxInfo); };
$("viewLabel").onclick = () => {
  state.mode = "alter";
  startLabelling({ id: state.boxInfo.id, boxName: state.boxInfo.boxName, unlabelled: state.boxInfo.unlabelled });
};
$("locatePhoto").onchange = (e) => {
  const f = e.target.files[0]; e.target.value = "";
  if (f) locate(f, [{ id: state.boxInfo.id, boxName: state.boxInfo.boxName }], VIEW_LOCATE);
};
$("boxNumber").addEventListener("keydown", (e) => { if (e.key === "Enter") startBox($("boxNumber").value); });
$("photoInput").onchange = (e) => { addPhotos([...e.target.files]); e.target.value = ""; };
$("photoPick").onchange = (e) => { addPhotos([...e.target.files]); e.target.value = ""; };
$("transcript").addEventListener("input", saveDraft);
$("analyseBtn").onclick = analyse;
$("backToBox").onclick = () => { stopMic(); $("boxBadge").hidden = true; resetLabelResult(); show("stepBox"); };
$("backToCapture").onclick = () => show("stepCapture");
$("addItem").onclick = () => addItemCard({}).querySelector(".name").focus();
$("saveBtn").onclick = save;
$("nextBox").onclick = () => { $("boxNumber").value = nextNumber(); $("boxBadge").hidden = true; resetLabelResult(); show("stepBox"); };
$("sameBox").onclick = () => { state.mode = "create"; state.boxId ? startBoxById(state.boxId) : startBox(state.box); };

(async function init() {
  history.replaceState({ step: "stepHome" }, "");
  setupMic();
  let last = load("last-box");
  if (typeof last === "string") last = { number: last, colour: "" }; // saved by an older version
  if (last) $("boxHint").textContent = `Last box: ${[last.colour, last.number].filter(Boolean).join(" ")}`;
  try {
    window.APP_CONFIG = await api("/api/config");
    const names = (window.APP_CONFIG.colours || []).map((c) => c.name);
    const remembered = load("last-colour");
    state.colour = names.includes(remembered) ? remembered : (names.length === 1 ? names[0] : "");
    renderColours();
    if (window.APP_CONFIG.parentLocation) {
      $("boxHint").textContent += `${last ? " · " : ""}Boxes are saved in “${window.APP_CONFIG.parentLocation}”.`;
    }
  } catch (e) { toast(e.message, true); }
})();
