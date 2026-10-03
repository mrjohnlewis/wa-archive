"use strict";
// WhatsApp Archive viewer. Message content only ever goes into the page as text nodes (textContent).

const $ = (s) => document.querySelector(s);
const PAGE = 80;
const MAX_DOM = 600;          // messages kept in the DOM; far side is trimmed while scrolling
const EDGE = 800;
const POLL_MS = 10000;        // how often to check whether an ingest changed the archive             // px from top/bottom that triggers loading more
const GROUPISH = new Set(["group", "community", "status", "status_thread"]);
const MEDIA_LABEL = { image: "📷 Photo", video: "🎥 Video", gif: "GIF", audio: "🎤 Voice message", sticker: "Sticker", document: "📄 Document" };
const canOpus = document.createElement("audio").canPlayType('audio/ogg; codecs="opus"') !== "";

const state = { chats: [], chat: null, msgs: [], hasOlder: false, hasNewer: false, loading: false, searchSeq: 0,
                stick: false };  // stick: keep the newest message in view while images load

function keepAtBottom() {
  if (state.stick) { const box = $("#messages"); box.scrollTop = box.scrollHeight; }
}

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k === "style") e.style.cssText = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids) if (kid !== null && kid !== undefined && kid !== false) e.append(kid);
  return e;
}

async function api(path) {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}

// ------------------------------------------------------------------ formatting
const d = (ts) => new Date(ts * 1000);
const dayKey = (ts) => d(ts).toDateString();
const fmtTime = (ts) => d(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const fmtDay = (ts) => d(ts).toLocaleDateString([], { weekday: "short", day: "numeric", month: "long", year: "numeric" });
const fmtDate = (ts) => d(ts).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
function fmtShort(ts) {
  if (!ts) return "";
  return dayKey(ts) === new Date().toDateString() ? fmtTime(ts) : fmtDate(ts);
}
function fmtSize(n) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}
function hue(s) {
  let h = 0;
  for (const c of s || "") h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}
function linkify(text) {
  const frag = document.createDocumentFragment();
  const re = /\bhttps?:\/\/[^\s<>"]+/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    frag.append(text.slice(last, m.index));
    frag.append(el("a", { href: m[0], target: "_blank", rel: "noopener noreferrer", text: m[0] }));
    last = m.index + m[0].length;
  }
  frag.append(text.slice(last));
  return frag;
}
function marked(snippet) {
  // FTS snippet uses \x01 / \x02 around matches.
  const frag = document.createDocumentFragment();
  const parts = (snippet || "").split(/(\x01[^\x02]*\x02)/);
  for (const p of parts) {
    if (p.startsWith("\x01")) frag.append(el("mark", { text: p.slice(1, -1) }));
    else frag.append(p);
  }
  return frag;
}

// ------------------------------------------------------------------ chat list
async function loadChats() {
  state.chats = await api(`/api/chats?hidden=${$("#show-hidden").checked ? 1 : 0}`);
  renderChatList();
}

function renderChatList() {
  const list = $("#chat-list");
  list.replaceChildren(...state.chats.map((c) =>
    el("button", { class: "chat-item" + (state.chat && state.chat.id === c.id ? " active" : ""), "data-id": c.id,
                   onclick: () => openChat(c.id) },
      el("div", { class: "row" }, el("span", { class: "name", text: c.name }),
         el("span", { class: "muted", text: fmtShort(c.last_ts) })),
      el("div", { class: "preview", text: c.preview || "" }))));
}

// ------------------------------------------------------------------ messages
function systemText(m) {
  if (m.type === 6) return m.text ? `Group name changed to “${m.text}”` : "Group update";
  if (m.type === 10) return `📞 ${m.text || "Call"}`;
  return m.text || "Update";
}

function renderMedia(m) {
  const md = m.media;
  const box = el("div", { class: "media" });
  if (md.status !== "present" || !md.sha) {
    const miss = el("div", { class: "missing" });
    if (md.thumb) miss.append(el("img", { src: `/blob/${md.thumb}`, alt: "", loading: "lazy" }));
    miss.append(`${MEDIA_LABEL[md.kind] || "Media"} not available: it was never on the phone when a backup was taken.`);
    box.append(miss);
    return box;
  }
  const src = `/blob/${md.sha}`;
  const poster = md.thumb ? `/blob/${md.thumb}` : null;
  if (md.kind === "image" || md.kind === "sticker") {
    const sized = md.kind === "image" && md.aspect;
    const img = el("img", { src, alt: "", loading: "lazy",
                            class: md.kind === "sticker" ? "sticker" : (sized ? "sized" : null),
                            style: sized ? `aspect-ratio: ${md.aspect}` : null });
    img.addEventListener("load", keepAtBottom);
    if (md.kind === "image") img.addEventListener("click", () => openLightbox(src));
    retryOnError(img, src);
    box.append(img);
  } else if (md.kind === "gif") {
    const v = el("video", { src, muted: true, loop: true, playsinline: true, preload: "metadata", poster });
    v.muted = true;
    v.addEventListener("click", () => (v.paused ? v.play() : v.pause()));
    box.append(v);
  } else if (md.kind === "video") {
    box.append(el("video", { src, controls: true, preload: "none", poster, playsinline: true }));
  } else if (md.kind === "audio") {
    if ((md.ext === "opus" || md.ext === "ogg") && !canOpus) {
      box.append(el("div", { class: "doc" }, "🎤 ",
        el("a", { href: `${src}?name=voice-note.${md.ext}`, text: "Download voice note" }),
        el("span", { class: "err", text: " (this browser can't play Opus audio)" })));
    } else {
      box.append(el("audio", { src, controls: true, preload: "none" }));
    }
  } else {
    const name = m.title || m.text || `document.${md.ext || "bin"}`;
    box.append(el("a", { class: "doc", href: `${src}?name=${encodeURIComponent(name)}` },
      "📄 ", el("span", { text: name }), el("span", { class: "muted", text: ` ${fmtSize(md.size)}` })));
  }
  return box;
}

function retryOnError(img, src) {
  let tries = 0;
  img.addEventListener("error", () => {
    if (tries++ >= 6) { img.replaceWith(el("div", { class: "err", text: "Couldn't load this image from iCloud." })); return; }
    img.alt = "Downloading from iCloud…";
    setTimeout(() => { img.src = `${src}?retry=${tries}`; }, 3000);
  });
}

function renderMessage(m) {
  const kind = state.chat ? state.chat.kind : "direct";
  if (m.type === 6 || m.type === 10) {
    return el("div", { class: "system", "data-key": m.key, "data-ts": m.ts }, el("span", { text: systemText(m) }));
  }
  const bubble = el("div", { class: "bubble" + (m.revoked ? " revoked" : "") });
  if (m.revoked) bubble.append(el("div", { class: "banner", text: "🚫 Deleted for everyone · kept in your archive" }));
  if (!m.from_me && m.sender && GROUPISH.has(kind)) {
    bubble.append(el("div", { class: "sender", text: m.sender, style: `color: hsl(${hue(m.sender_jid)} 55% 45%)` }));
  }
  if (m.quote) {
    const q = m.quote;
    if (q.missing) {
      bubble.append(el("div", { class: "quote" }, el("div", { class: "qt", text: "Reply to a message that isn't in the archive" })));
    } else {
      const qbox = el("div", { class: "quote", onclick: () => jumpTo(q.key) },
        el("div", {}, el("div", { class: "qs", text: q.sender || "" }),
          el("div", { class: "qt", text: q.text || (q.media ? MEDIA_LABEL[q.media] : "") })));
      if (q.sha) qbox.append(el("img", { src: `/blob/${q.sha}`, alt: "", loading: "lazy" }));
      bubble.append(qbox);
    }
  }
  if (m.type === 14 && !m.revoked) {
    bubble.append(el("div", { class: "text muted", text: "🚫 This message was deleted before it was archived" }));
  }
  if (m.media) bubble.append(renderMedia(m));
  if (m.location) bubble.append(el("div", { class: "text", text: `📍 Location ${m.location.lat ?? "?"}, ${m.location.lon ?? "?"}` }));
  if (m.contact) bubble.append(el("div", { class: "text", text: `👤 ${m.contact}` }));

  let body = null;
  if (m.media) body = m.media.kind === "document" ? null : (m.title || m.text);
  else if (m.type === 7) {
    if (m.title) bubble.append(el("div", { class: "text", style: "font-weight:600", text: m.title }));
    body = m.text;
  } else if (m.type === 46 || m.type === 66) body = `📊 Poll${m.text ? ": " + m.text : ""}`;
  else if (m.type === 59) body = `📅 Event${m.text || m.title ? ": " + (m.text || m.title) : ""}`;
  else if (m.type !== 14) body = m.text || m.title;
  if (body) bubble.append(el("div", { class: "text" }, linkify(body)));
  if (!body && !m.media && !m.location && !m.contact && m.type !== 14 && !m.quote) {
    bubble.append(el("div", { class: "text muted", text: `Unsupported message (type ${m.type})` }));
  }

  const meta = el("div", { class: "meta-line" });
  if (m.edited) {
    meta.append(el("span", { class: "tag link", text: "edited", title: "Show earlier versions",
                             onclick: () => toggleVersions(bubble, m.key) }));
  }
  meta.append(el("span", { text: fmtTime(m.ts) }));
  bubble.append(meta);
  if (m.reactions && m.reactions.length) {
    bubble.append(el("div", { class: "reactions" }, ...m.reactions.map((r) =>
      el("span", { title: r.who.join(", "), text: r.count > 1 ? `${r.emoji} ${r.count}` : r.emoji }))));
  }
  return el("div", { class: "msg " + (m.from_me ? "out" : "in"), "data-key": m.key, "data-ts": m.ts }, bubble);
}

async function toggleVersions(bubble, key) {
  const existing = bubble.querySelector(".versions");
  if (existing) { existing.remove(); return; }
  const versions = await api(`/api/messages/${encodeURIComponent(key)}/versions`);
  bubble.append(el("div", { class: "versions" }, ...versions.map((v) =>
    el("div", {}, el("span", { class: "tag", text: `${v.kind} (run ${v.run}): ` }),
       v.kind === "revoke" ? "deleted for everyone" : (v.text || v.title || "")))));
}

function fixSeparators() {
  const box = $("#messages");
  box.querySelectorAll(".day").forEach((n) => n.remove());
  let prev = null;
  for (const node of [...box.children]) {
    const ts = Number(node.dataset.ts);
    if (!ts) continue;
    const k = dayKey(ts);
    if (k !== prev) node.before(el("div", { class: "day" }, el("span", { text: fmtDay(ts) })));
    prev = k;
  }
}

function msgNodes() {
  return [...$("#messages").children].filter((n) => n.dataset.key);
}

// ------------------------------------------------------------------ opening & scrolling
async function openChat(id, opts = {}) {
  const box = $("#messages");
  const info = state.chats.find((c) => c.id === id) || (await api(`/api/chats/${id}`));
  state.chat = info;
  document.body.classList.add("chat-open");
  $("#calls").hidden = true;
  box.hidden = false;
  $("#empty").hidden = true;
  $("#chat-head").hidden = false;
  $("#chat-head").classList.remove("calls-mode");
  $("#chat-title").textContent = info.name;
  $("#chat-sub").textContent = info.count
    ? `${info.count.toLocaleString()} messages · ${fmtDate(info.first_ts)} – ${fmtDate(info.last_ts)}` : info.kind;
  $("#chat-search").value = "";
  history.replaceState(null, "", `#chat=${id}`);
  renderChatList();

  let q = `limit=${PAGE}`;
  if (opts.aroundKey) q += `&around=${encodeURIComponent(opts.aroundKey)}`;
  else if (opts.aroundTs) q += `&around_ts=${opts.aroundTs}`;
  state.loading = true;
  const page = await api(`/api/chats/${id}/messages?${q}`);
  state.loading = false;
  state.msgs = page.messages;
  state.hasOlder = page.has_older;
  state.hasNewer = page.has_newer;
  box.replaceChildren(...page.messages.map(renderMessage));
  fixSeparators();
  const target = page.target && box.querySelector(`[data-key="${CSS.escape(page.target)}"]`);
  $("#new-msgs").hidden = true;
  state.stick = !(target && (opts.aroundKey || opts.aroundTs));
  if (state.stick) keepAtBottom();
  else highlight(target);
}

function highlight(node) {
  node.scrollIntoView({ block: "center" });
  node.classList.add("target");
  setTimeout(() => node.classList.remove("target"), 4000);
}

function jumpTo(key) {
  const node = $("#messages").querySelector(`[data-key="${CSS.escape(key)}"]`);
  if (node) highlight(node);
  else openChat(state.chat.id, { aroundKey: key });
}

async function loadOlder() {
  if (state.loading || !state.hasOlder || !state.msgs.length) return;
  state.loading = true;
  const box = $("#messages");
  const first = state.msgs[0];
  const chatId = state.chat.id;
  try {
    const page = await api(`/api/chats/${chatId}/messages?limit=${PAGE}&before=${first.ts}:${first.rid}`);
    if (state.chat.id !== chatId) return;
    const h0 = box.scrollHeight, top0 = box.scrollTop;
    box.prepend(...page.messages.map(renderMessage));
    state.msgs = page.messages.concat(state.msgs);
    state.hasOlder = page.has_older;
    trim("end");
    fixSeparators();
    box.scrollTop = top0 + (box.scrollHeight - h0);
  } finally { state.loading = false; }
}

async function loadNewer() {
  if (state.loading || !state.hasNewer || !state.msgs.length) return;
  state.loading = true;
  const box = $("#messages");
  const last = state.msgs[state.msgs.length - 1];
  const chatId = state.chat.id;
  try {
    const page = await api(`/api/chats/${chatId}/messages?limit=${PAGE}&after=${last.ts}:${last.rid}`);
    if (state.chat.id !== chatId) return;
    box.append(...page.messages.map(renderMessage));
    state.msgs = state.msgs.concat(page.messages);
    state.hasNewer = page.has_newer;
    const h0 = box.scrollHeight;
    trim("start");
    fixSeparators();
    box.scrollTop -= h0 - box.scrollHeight;
  } finally { state.loading = false; }
}

function trim(side) {
  const extra = state.msgs.length - MAX_DOM;
  if (extra <= 0) return;
  const nodes = msgNodes();
  if (side === "end") {
    nodes.slice(-extra).forEach((n) => n.remove());
    state.msgs = state.msgs.slice(0, -extra);
    state.hasNewer = true;
  } else {
    nodes.slice(0, extra).forEach((n) => n.remove());
    state.msgs = state.msgs.slice(extra);
    state.hasOlder = true;
  }
}

function onScroll() {
  // Handled directly (not via requestAnimationFrame, which pauses in background tabs);
  // state.loading prevents overlapping fetches.
  const box = $("#messages");
  if (box.scrollHeight - box.scrollTop - box.clientHeight > 40) state.stick = false;  // user scrolled up
  if (box.scrollHeight - box.scrollTop - box.clientHeight < 40 && !state.hasNewer) $("#new-msgs").hidden = true;
  if (box.scrollTop < EDGE) loadOlder();
  else if (box.scrollHeight - box.scrollTop - box.clientHeight < EDGE) loadNewer();
}

// ------------------------------------------------------------------ search
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

async function runSearch(q, chatId, offset = 0, append = false) {
  const panel = $("#search-results");
  if (!q.trim()) {
    panel.hidden = true;
    $("#chat-list").hidden = false;
    return;
  }
  const seq = ++state.searchSeq;
  const params = new URLSearchParams({ q, limit: 50, offset });
  if (chatId) params.set("chat", chatId);
  const data = await api(`/api/search?${params}`);
  if (seq !== state.searchSeq) return;
  $("#chat-list").hidden = true;
  panel.hidden = false;
  if (!append) {
    const where = chatId ? ` in ${state.chat.name}` : "";
    panel.replaceChildren(el("div", { class: "results-head",
      text: data.error || (data.results.length ? `Results${where}` : `No results${where}`) }));
  }
  panel.querySelector(".more")?.remove();
  for (const r of data.results) {
    panel.append(el("button", { class: "result", onclick: () => openChat(r.chat_id, { aroundKey: r.key }) },
      el("div", { class: "meta" }, el("span", { text: r.chat }), el("span", { text: fmtDate(r.ts) })),
      el("div", { class: "snip" }, r.sender ? el("b", { text: `${r.sender}: ` }) : null, marked(r.snippet))));
  }
  if (data.has_more) {
    panel.append(el("button", { class: "result more", text: "More results…",
                                onclick: () => runSearch(q, chatId, offset + 50, true) }));
  }
}

// ------------------------------------------------------------------ calls, lightbox
async function showCalls() {
  const calls = await api("/api/calls");
  state.chat = null;
  renderChatList();
  document.body.classList.add("chat-open");
  $("#empty").hidden = true;
  $("#messages").hidden = true;
  $("#chat-head").hidden = false;
  $("#chat-head").classList.add("calls-mode");
  $("#chat-title").textContent = "Calls";
  $("#chat-sub").textContent = `${calls.length} calls in the archive`;
  const pane = $("#calls");
  pane.hidden = false;
  pane.replaceChildren(...calls.map((c) => el("div", { class: "call" },
    el("span", { text: `${c.video ? "🎥" : "📞"} ${c.group || c.with.join(", ") || "Unknown"}${c.missed ? " (missed)" : ""}` }),
    el("span", { class: "muted", text: `${fmtDay(c.ts)} ${fmtTime(c.ts)} · ${Math.round((c.duration || 0) / 60)} min` }))));
}

function openLightbox(src) {
  const lb = $("#lightbox");
  lb.querySelector("img").src = src;
  lb.hidden = false;
}

// ------------------------------------------------------------------ live updates
const live = { version: null, messages: null, toastTimer: null };

function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(live.toastTimer);
  live.toastTimer = setTimeout(() => (t.hidden = true), 6000);
}

async function pollVersion() {
  if (document.hidden && live.version !== null) return;  // background tab: catch up when it becomes visible
  let v;
  try { v = await api("/api/version"); } catch (_) { return; }  // server stopped or restarting
  if (live.version === null) { Object.assign(live, { version: v.version, messages: v.messages }); return; }
  if (v.version === live.version) return;
  const added = v.messages - live.messages;
  Object.assign(live, { version: v.version, messages: v.messages });
  const before = state.chat && state.chats.find((c) => c.id === state.chat.id);
  await loadChats();
  if (v.backup_date) $("#archive-info").textContent = `Latest backup ${fmtDay(Date.parse(v.backup_date) / 1000)}`;
  toast(v.ingest_running ? `Ingest in progress · ${added.toLocaleString()} new messages so far`
                         : `Archive updated · ${added.toLocaleString()} new messages`);
  if (!state.chat || $("#messages").hidden) return;
  const now = state.chats.find((c) => c.id === state.chat.id);
  if (!now || (before && now.last_ts === before.last_ts && now.count === before.count)) return;
  Object.assign(state.chat, now);
  $("#chat-sub").textContent = `${now.count.toLocaleString()} messages · ${fmtDate(now.first_ts)} – ${fmtDate(now.last_ts)}`;
  if (state.stick) {
    state.hasNewer = true;
    await loadNewer();
    keepAtBottom();
  } else {
    state.hasNewer = true;  // the next scroll to the bottom fetches them
    $("#new-msgs").hidden = false;
  }
}

// ------------------------------------------------------------------ boot
async function boot() {
  $("#messages").addEventListener("scroll", onScroll, { passive: true });
  $("#show-hidden").addEventListener("change", loadChats);
  $("#calls-btn").addEventListener("click", showCalls);
  $("#back").addEventListener("click", () => document.body.classList.remove("chat-open"));
  $("#lightbox").addEventListener("click", () => ($("#lightbox").hidden = true));
  $("#new-msgs").addEventListener("click", () => { $("#new-msgs").hidden = true; openChat(state.chat.id); });
  setInterval(pollVersion, POLL_MS);
  document.addEventListener("visibilitychange", pollVersion);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#lightbox").hidden = true; });
  const search = debounce((e) => runSearch(e.target.value, null), 300);
  $("#search").addEventListener("input", search);
  const chatSearch = debounce((e) => runSearch(e.target.value, state.chat && state.chat.id), 300);
  $("#chat-search").addEventListener("input", chatSearch);
  $("#jump-date").addEventListener("change", (e) => {
    if (!e.target.value || !state.chat) return;
    const [y, mo, da] = e.target.value.split("-").map(Number);
    openChat(state.chat.id, { aroundTs: new Date(y, mo - 1, da).getTime() / 1000 });
  });
  try {
    const info = await api("/api/info");
    if (info.backup_date) $("#archive-info").textContent = `Latest backup ${fmtDay(Date.parse(info.backup_date) / 1000)}`;
  } catch (_) { /* informational only */ }
  await loadChats();
  await pollVersion();  // records the starting version
  const m = location.hash.match(/chat=(\d+)/);
  if (m) openChat(Number(m[1]));
}

boot();
