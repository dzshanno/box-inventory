"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  box: null,        // box number as typed
  boxInfo: null,    // response from /api/box
  photos: [],       // [{blob, url}]
  draft: null,      // response from /api/analyze
};
const PHOTO_EDGE = 2000; // resize before upload: plenty for Homebox, much faster on mobile data

// ---------- helpers ----------
function show(step) {
  for (const s of document.querySelectorAll(".step")) s.hidden = s.id !== step;
  window.scrollTo(0, 0);
}
function busy(text) { $("busyText").textContent = text || "Working…"; $("busy").hidden = !text; }
let toastTimer;
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
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
const draftKey = () => `box-draft-${state.box}`;
function saveDraft() {
  store(draftKey(), { transcript: $("transcript").value, items: state.draft ? collectItems() : null,
                      summary: state.draft?.box_summary || "" });
}
function escapeHtml(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

// ---------- step 1: box ----------
async function startBox(number) {
  number = String(number || "").trim();
  if (!number) { toast("Enter the box number first."); return; }
  busy("Checking Homebox…");
  try {
    state.box = number;
    state.boxInfo = await api(`/api/box/${encodeURIComponent(number)}`);
  } catch (e) { busy(); toast(e.message); return; }
  busy();
  store("last-box", number);
  $("boxBadge").textContent = state.boxInfo.boxName;
  $("boxBadge").hidden = false;

  const note = $("existingNote");
  if (state.boxInfo.exists && state.boxInfo.items.length) {
    const list = state.boxInfo.items.slice(0, 8).map((i) => `<li>${escapeHtml(i.name)} ×${i.quantity}</li>`).join("");
    const more = state.boxInfo.items.length > 8 ? `<li>…and ${state.boxInfo.items.length - 8} more</li>` : "";
    note.innerHTML = `<strong>${escapeHtml(state.boxInfo.boxName)} already has ${state.boxInfo.items.length} item(s).</strong>
      Anything you add now is added to it; Claude will skip what's already recorded.<ul>${list}${more}</ul>`;
    note.hidden = false;
  } else if (state.boxInfo.exists) {
    note.textContent = `${state.boxInfo.boxName} exists in Homebox but has no items yet.`;
    note.hidden = false;
  } else {
    note.hidden = true;
  }

  clearPhotos();
  state.draft = null;
  const saved = load(draftKey());
  $("transcript").value = saved?.transcript || "";
  show("stepCapture");
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
  fd.append("box_number", state.box);
  fd.append("transcript", transcript);
  state.photos.forEach((p, i) => fd.append("photos", p.blob, `photo-${i + 1}.jpg`));
  busy("Claude is building the inventory… (usually under a minute)");
  try {
    state.draft = await api("/api/analyze", { method: "POST", body: fd });
  } catch (e) { busy(); toast(e.message); return; }
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
  fd.append("box_number", state.box);
  fd.append("items", JSON.stringify(items));
  fd.append("transcript", $("transcript").value.trim());
  fd.append("box_summary", state.draft?.box_summary || "");
  state.photos.forEach((p, i) => fd.append("photos", p.blob, `photo-${i + 1}.jpg`));
  busy("Saving to Homebox…");
  let res;
  try { res = await api("/api/save", { method: "POST", body: fd }); }
  catch (e) { busy(); toast(e.message); return; }
  busy();
  forget(draftKey());
  $("doneTitle").textContent = `${res.boxName} saved`;
  $("doneText").textContent = `${res.itemsSaved} item(s) and ${res.photosSaved} photo(s) added${res.created ? " to a new box" : ""}.`;
  const link = $("doneLink");
  if (res.link) { link.href = res.link; link.hidden = false; } else link.hidden = true;
  $("nextBox").textContent = nextNumber() ? `Next box (${nextNumber()})` : "Next box";
  show("stepDone");
}

// ---------- wiring ----------
function nextNumber() {
  return /^\d+$/.test(state.box) ? String(parseInt(state.box, 10) + 1) : "";
}
$("startBtn").onclick = () => startBox($("boxNumber").value);
$("boxNumber").addEventListener("keydown", (e) => { if (e.key === "Enter") startBox($("boxNumber").value); });
$("photoInput").onchange = (e) => { addPhotos([...e.target.files]); e.target.value = ""; };
$("photoPick").onchange = (e) => { addPhotos([...e.target.files]); e.target.value = ""; };
$("transcript").addEventListener("input", saveDraft);
$("analyseBtn").onclick = analyse;
$("backToBox").onclick = () => { stopMic(); $("boxBadge").hidden = true; show("stepBox"); };
$("backToCapture").onclick = () => show("stepCapture");
$("addItem").onclick = () => addItemCard({}).querySelector(".name").focus();
$("saveBtn").onclick = save;
$("nextBox").onclick = () => { $("boxNumber").value = nextNumber(); $("boxBadge").hidden = true; show("stepBox"); $("boxNumber").focus(); };
$("sameBox").onclick = () => startBox(state.box);

(async function init() {
  setupMic();
  const last = load("last-box");
  if (last) $("boxHint").textContent = `Last box: ${last}`;
  try {
    window.APP_CONFIG = await api("/api/config");
    if (window.APP_CONFIG.parentLocation) {
      $("boxHint").textContent += `${last ? " · " : ""}Boxes are saved in “${window.APP_CONFIG.parentLocation}”.`;
    }
  } catch (e) { toast(e.message); }
})();
