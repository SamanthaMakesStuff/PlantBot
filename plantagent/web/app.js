"use strict";
// All text from the model is put on the page with textContent, never innerHTML.

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const S = { name: "Your plant", lastId: 0, tab: "activity", busy: false, state: null, loaded: {} };

const LABELS = {
  birth: "Was born", remember: "Remembered something", diary: "Wrote in the diary", wish: "Made a wish",
  persona: "Changed something about themselves", blocked: "Tried something that isn't allowed",
  thought: "Had a quiet thought", sleep: "Slept", error: "Ran into a problem",
  document: "Was given a document", read: "Read a document", notes: "Made reading notes",
  recall: "Looked back through their memories", pictures: "Looked at pictures",
  wikipedia: "Looked something up on Wikipedia", learn: "Learnt something",
  wish_yes: "You said yes to a wish", wish_not_now: "You said not now to a wish", wish_open: "You reopened a wish",
};

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  let res;
  try { res = await fetch(path, opts); } catch (e) { throw new Error("Can't reach the plant. Is it running?"); }
  let data = {};
  try { data = await res.json(); } catch (e) { /* keep empty */ }
  if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Please log in."); }
  if (!res.ok) throw new Error(data.message || "Something went wrong.");
  return data;
}

// ---------------------------------------------------------------- time
function parseStamp(t) {
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(t || "");
  return m ? new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5]) : null;
}
function when(t) {
  const d = parseStamp(t);
  if (!d) return t || "";
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 86400000);
  if (days === 0) return "Today, " + hm;
  if (days === 1) return "Yesterday, " + hm;
  return d.toLocaleDateString([], { day: "numeric", month: "short" }) + ", " + hm;
}

let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

// ---------------------------------------------------------------- login
function showLogin() {
  $("app").hidden = true;
  $("login").hidden = false;
  $("password").focus();
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  try {
    await api("/api/login", { password: $("password").value });
    $("password").value = "";
    await start();
  } catch (err) { $("login-error").textContent = err.message; }
});

$("btn-logout").addEventListener("click", async () => {
  try { await api("/api/logout", {}); } catch (e) { /* already out */ }
  showLogin();
});

// ---------------------------------------------------------------- header
function renderState(s) {
  S.state = s;
  S.name = s.name;
  $("name").textContent = s.name;
  document.title = s.name;
  let sub = s.variety + ".";
  if (s.night) sub += " Asleep.";
  else if (s.mood) sub += " Feeling " + s.mood + ".";
  $("subtitle").textContent = sub;
  $("avatar").classList.toggle("asleep", !!s.night);
  $("text").placeholder = "Write to " + s.name;
  $("empty-chat").textContent = "Say hello to " + s.name + ". Everything you write here is kept in the chat log.";
  if (s.call_budget > 0) {
    $("usage").hidden = false;
    const pct = Math.min(100, Math.round((s.calls / s.call_budget) * 100));
    document.querySelector("#usage .meter span").style.width = pct + "%";
    document.querySelector("#usage .meter").setAttribute("aria-valuenow", String(pct));
    $("usage-text").textContent = s.calls + " of " + s.call_budget + " calls today";
  } else {
    $("usage").hidden = true;
  }
}
async function refreshState() { renderState(await api("/api/state")); }

// ---------------------------------------------------------------- chat
function addMessage(role, text, notes) {
  $("empty-chat").hidden = true;
  const m = el("div", "msg " + (role === "user" ? "you" : "plant"));
  m.appendChild(document.createTextNode(text));
  if (notes && notes.length) {
    const box = el("div", "notes");
    notes.forEach((n) => box.appendChild(el("p", "", n)));
    m.appendChild(box);
  }
  const list = $("messages");
  list.appendChild(m);
  list.scrollTop = list.scrollHeight;
  return m;
}
function addNotice(text) {
  const n = el("p", "notice", text);
  $("messages").appendChild(n);
  $("messages").scrollTop = $("messages").scrollHeight;
  return n;
}

async function loadChat() {
  const { messages } = await api("/api/chat");
  const box = $("messages");
  box.querySelectorAll(".msg, .notice").forEach((n) => n.remove());
  messages.forEach((m) => addMessage(m.role, m.content));
  $("empty-chat").hidden = messages.length > 0;
}

function setBusy(on) {
  S.busy = on;
  $("send").disabled = on;
  $("btn-think").disabled = on;
  $("btn-sleep").disabled = on;
  $("avatar").classList.toggle("thinking", on);
}

let attached = null;  // {name, data}

function isPicture(name) { return /\.(jpe?g|png|gif|webp|heic|heif)$/i.test(name || ""); }

function setAttached(a) {
  attached = a;
  $("attached").hidden = !a;
  $("attached-name").textContent = a ? a.name : "";
  $("send").textContent = a ? (isPicture(a.name) ? "Send picture" : "Send document") : "Send";
}

$("attach").addEventListener("click", () => $("file").click());
$("attached-remove").addEventListener("click", () => { setAttached(null); $("file").value = ""; });
$("file").addEventListener("change", () => {
  const f = $("file").files[0];
  if (!f) return;
  if (f.size > 50000000) { toast("That file is over 50 MB. Try a shorter one."); $("file").value = ""; return; }
  const reader = new FileReader();
  reader.onload = () => {
    const bytes = new Uint8Array(reader.result);
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    setAttached({ name: f.name, data: btoa(bin) });
    $("text").focus();
  };
  reader.onerror = () => toast("Couldn't read that file.");
  reader.readAsArrayBuffer(f);
});

$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const box = $("text");
  const text = box.value.trim();
  const doc = attached;
  if ((!text && !doc) || S.busy) return;
  box.value = "";
  autosize();
  addMessage("user", doc ? (text ? text + "\n" : "") + "[shared a " + (isPicture(doc.name) ? "picture" : "document") + ": " + doc.name + "]" : text);
  setAttached(null);
  $("file").value = "";
  setBusy(true);
  const typing = el("p", "soft small", S.name + (doc ? " is reading..." : " is thinking..."));
  $("messages").appendChild(typing);
  $("messages").scrollTop = $("messages").scrollHeight;
  try {
    const out = doc
      ? await api("/api/upload", { name: doc.name, data: doc.data, text })
      : await api("/api/chat", { text });
    typing.remove();
    addMessage("plant", out.reply, out.notes);
    if (out.document && out.document.kind !== "picture") {
      const d = out.document;
      const looks = d.scanned_pages ? " It's scanned, so they'll read its " + d.scanned_pages + " pages first." :
        d.pictures ? " They'll look at its " + d.pictures + " pictures first." : "";
      const skipped = d.pictures_skipped ? " " + d.pictures_skipped + " pictures are in a format they can't open." : "";
      toast((d.parts > 1
        ? "Long book, so it comes in " + d.parts + " parts. " + S.name + " read part 1 and will read the rest on their own, making notes as they go."
        : S.name + " will make reading notes on it in the background.") + looks + skipped);
    }
    if (S.tab === "library") loadLibrary().catch(() => {});
  } catch (err) {
    typing.remove();
    addNotice(err.message + (doc ? " The " + (isPicture(doc.name) ? "picture" : "document") + " wasn't shared. Attach it and try again." : " Your message wasn't saved. Send it again to retry."));
    if (doc) setAttached(doc); else box.value = text;
    if (!doc) autosize();
  }
  setBusy(false);
  refreshState().catch(() => {});
  pollActivity().catch(() => {});
  if (S.tab !== "activity" && S.tab !== "library") refreshTab(S.tab);
});

function autosize() {
  const t = $("text");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 144) + "px";
}
$("text").addEventListener("input", autosize);
$("text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});

// ---------------------------------------------------------------- inside
function emptyNote(text) { return el("p", "soft empty", text); }

async function loadActivity(first) {
  const out = await api("/api/activity?since=" + (first ? 0 : S.lastId));
  if (out.events.length === 0) return;
  S.lastId = out.last_id;
  const panel = $("panel-activity");
  let ul = panel.querySelector("ul");
  if (!ul) {
    panel.textContent = "";
    ul = el("ul", "vine");
    panel.appendChild(ul);
  }
  out.events.forEach((ev) => {
    const li = el("li", "k-" + ev.kind);
    const what = el("div", "what", LABELS[ev.kind] || ev.kind);
    what.appendChild(el("span", "time", when(ev.t)));
    li.appendChild(what);
    if (ev.text) li.appendChild(el("p", "says", ev.text));
    ul.insertBefore(li, ul.firstChild);  // newest first
  });
}
function pollActivity() { return loadActivity(false); }

async function loadDiary() {
  const { entries } = await api("/api/diary");
  const p = $("panel-diary");
  p.textContent = "";
  if (!entries.length) { p.appendChild(emptyNote(S.name + " hasn't written in the diary yet. Entries appear after a chat, a quiet moment or sleep.")); return; }
  entries.slice().reverse().forEach((e) => {
    const page = el("div", "page");
    page.appendChild(el("div", "time", when(e.t)));
    page.appendChild(el("div", "text", e.text));
    p.appendChild(page);
  });
}

async function loadMemory() {
  const { items } = await api("/api/memory");
  const p = $("panel-memory");
  p.textContent = "";
  if (!items.length) { p.appendChild(emptyNote(S.name + " hasn't remembered anything yet. Tell them something about you.")); return; }
  const ul = el("ul", "facts");
  items.slice().reverse().forEach((i) => {
    const li = el("li");
    li.appendChild(el("span", "time", when(i.t)));
    li.appendChild(document.createTextNode(i.text));
    ul.appendChild(li);
  });
  p.appendChild(ul);
}

async function loadNotebook() {
  const { items } = await api("/api/notebook");
  const p = $("panel-notebook");
  p.textContent = "";
  if (!items.length) { p.appendChild(emptyNote(S.name + " hasn't written anything in their learning notebook yet. When they learn something interesting, it goes here.")); return; }
  const ul = el("ul", "facts");
  items.slice().reverse().forEach((i) => {
    const li = el("li");
    li.appendChild(el("span", "time", when(i.t)));
    li.appendChild(document.createTextNode(i.text));
    ul.appendChild(li);
  });
  p.appendChild(ul);
}

async function setWish(id, status, verb) {
  try {
    const out = await api("/api/wishes", { id, status });
    renderWishes(out.items);
    toast(verb);
  } catch (err) { toast(err.message); }
}

function renderWishes(items) {
  const p = $("panel-wishes");
  p.textContent = "";
  const open = items.filter((w) => w.status === "open");
  $("wish-count").hidden = open.length === 0;
  $("wish-count").textContent = String(open.length);
  if (!items.length) { p.appendChild(emptyNote(S.name + " hasn't wished for anything yet. Wishes are things they'd like to be able to do.")); return; }
  const group = (title, list, decided) => {
    if (!list.length) return;
    if (title) p.appendChild(el("p", "group-title", title));
    list.slice().reverse().forEach((w) => {
      const d = el("div", "wish");
      d.appendChild(el("p", "text", w.text));
      const row = el("div", "row");
      if (!decided) {
        const yes = el("button", "yes", "Say yes");
        yes.type = "button";
        yes.addEventListener("click", () => setWish(w.id, "yes", "Said yes"));
        const no = el("button", "", "Not now");
        no.type = "button";
        no.addEventListener("click", () => setWish(w.id, "not_now", "Said not now"));
        row.append(yes, no);
      } else {
        row.appendChild(el("span", "status" + (w.status === "yes" ? "" : " no"), w.status === "yes" ? "You said yes" : "You said not now"));
        const re = el("button", "", "Reopen");
        re.type = "button";
        re.addEventListener("click", () => setWish(w.id, "open", "Reopened"));
        row.appendChild(re);
      }
      d.appendChild(row);
      p.appendChild(d);
    });
  };
  group("", open, false);
  group("Decided", items.filter((w) => w.status !== "open"), true);
}
async function loadLibrary() {
  const { items } = await api("/api/library");
  const p = $("panel-library");
  p.textContent = "";
  if (!items.length) { p.appendChild(emptyNote("Nothing here yet. Use Attach in the chat to share a document with " + S.name + ".")); return; }
  items.forEach((d) => {
    const row = el("div", "lib");
    row.appendChild(el("p", "title", d.title));
    row.appendChild(el("p", "time", when(d.added) + (d.kind === "picture" ? ", a picture you sent" : d.words ? ", about " + d.words.toLocaleString() + " words" : "") + (d.parts > 1 ? ", " + d.parts + " parts" : "")
      + (d.pictures && d.kind !== "picture" ? ", " + d.pictures + " pictures" : "") + (d.scanned_pages ? ", " + d.scanned_pages + " scanned pages" : "")));
    if (d.pictures_todo) row.appendChild(el("p", "summary soft", d.kind === "picture" ? "Looking at it..." : (d.scanned_pages ? (d.pictures ? "Reading scanned pages and pictures" : "Reading scanned pages") : "Looking at pictures") + " (" + d.pictures_todo + " to go)..."));
    else if (d.summary) row.appendChild(el("p", "summary", d.summary));
    else if (d.notes_todo.length) row.appendChild(el("p", "summary soft", "Making reading notes..."));
    p.appendChild(row);
  });
}

async function loadAbout() {
  const a = await api("/api/about");
  const p = $("panel-about");
  p.textContent = "";
  const words = (v) => (Array.isArray(v) ? v.join(", ") : v || "");
  const section = (title, value, first) => {
    if (!words(value)) return;
    const box = el("div", "about");
    box.appendChild(el("p", "group-title", title));
    box.appendChild(el("p", "text", words(value)));
    if (first !== undefined && words(first) && words(first) !== words(value)) box.appendChild(el("p", "soft small", "At first: " + words(first)));
    p.appendChild(box);
  };
  section(a.variety + (a.born ? ", born " + a.born : ""), a.personality, a.at_birth.personality);
  section("How they speak", a.voice, a.at_birth.voice);
  section("Why they chose this plant", a.why_this_plant);
  section("Likes", a.likes, a.at_birth.likes);
  section("Fears", a.fears, a.at_birth.fears);
  section("Their first words", a.first_words);
  p.appendChild(el("p", "group-title changed", "What they have made their own since"));
  const labels = { mood: "Mood", interests: "Interests", goals: "Goals", voice_notes: "Notes on their voice" };
  Object.keys(labels).forEach((k) => {
    const row = el("div", "about");
    row.appendChild(el("p", "label", labels[k]));
    row.appendChild(el("p", "text", a.now[k] || "Nothing yet"));
    if (a.at_birth[k] && a.at_birth[k] !== a.now[k]) row.appendChild(el("p", "soft small", "At first: " + a.at_birth[k]));
    p.appendChild(row);
  });
}

async function loadWishes() { renderWishes((await api("/api/wishes")).items); }

function refreshTab(tab) {
  const fn = { activity: () => loadActivity(false), diary: loadDiary, memory: loadMemory, wishes: loadWishes, library: loadLibrary, notebook: loadNotebook, about: loadAbout }[tab];
  return fn().catch((e) => toast(e.message));
}

function selectTab(tab) {
  S.tab = tab;
  document.querySelectorAll(".tabs [role=tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  ["activity", "diary", "memory", "wishes", "library", "notebook", "about"].forEach((t) => { $("panel-" + t).hidden = t !== tab; });
  refreshTab(tab);
}
document.querySelectorAll(".tabs [role=tab]").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));

document.querySelectorAll(".nav button").forEach((b) => b.addEventListener("click", () => {
  $("app").dataset.pane = b.dataset.pane;
  document.querySelectorAll(".nav button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
}));

async function action(path, doing, done) {
  if (S.busy) return;
  setBusy(true);
  toast(doing);
  try {
    await api(path, {});
    toast(done);
  } catch (err) { toast(err.message); }
  setBusy(false);
  refreshState().catch(() => {});
  pollActivity().catch(() => {});
  if (S.tab !== "activity") refreshTab(S.tab);
}
$("btn-think").addEventListener("click", () => action("/api/think", S.name + " is having a quiet moment...", "Quiet moment done"));
$("btn-sleep").addEventListener("click", () => action("/api/sleep", S.name + " is going to sleep...", "Slept"));

// ---------------------------------------------------------------- start
let pollTimer;
async function start() {
  try {
    await refreshState();
  } catch (e) { return; }  // not logged in: api() already showed the login
  $("login").hidden = true;
  $("app").hidden = false;
  S.lastId = 0;
  $("panel-activity").textContent = "";
  await Promise.all([loadChat(), loadActivity(true), loadWishes()]).catch((e) => toast(e.message));
  if (!$("panel-activity").querySelector("ul")) $("panel-activity").appendChild(emptyNote("Nothing has happened yet."));
  clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (document.hidden || $("app").hidden) return;
    pollActivity().catch(() => {});
    if (!S.busy) refreshState().catch(() => {});
  }, 5000);
}

start();
