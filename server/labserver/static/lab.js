"use strict";
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pcUrl = (id) => "/api/pcs/" + encodeURIComponent(id);

function svg(body) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
}
const ICON = {
  message: svg('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'),
  lock: svg('<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>'),
  unlock: svg('<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/>'),
  monitor: svg('<rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>'),
  off: svg('<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>'),
};

async function api(path, method = "GET", body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Lab-Admin": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) {          // sesi habis: kembali ke halaman login
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
    throw new Error("Sesi berakhir, silakan login lagi");
  }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
  return res.json();
}
function setErr(text) { $("#err").textContent = text || ""; }
const changed = () => document.dispatchEvent(new Event("lab:changed"));
async function act(path, method, body) {
  try { const out = await api(path, method, body); setErr(""); changed(); return out; }
  catch (e) { setErr(e.message); return null; }
}

function dur(sec) {
  if (sec == null) return "";
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return h ? `${h}j ${m}m` : m ? `${m}m ${s}dtk` : `${s}dtk`;
}
const dateTime = (ts) => new Date(ts * 1000).toLocaleString("id-ID");
const shotUrl = (pc) => `${pcUrl(pc.id)}/screenshot?t=${Math.floor(pc.shot)}`;

function lockStatus(pc) {
  const src = { manual: "manual", global: "semua PC", schedule: "jadwal" }[pc.source] || pc.source;
  const next = pc.changes_in == null ? "tanpa batas" : `${pc.locked ? "buka" : "kunci"} dalam ${dur(pc.changes_in)}`;
  return `${pc.locked ? "Terkunci" : "Terbuka"} · ${src} · ${next}`;
}
function renameText(pc) {
  const r = pc.rename;
  if (!r) return "";
  return r.state === "pending" ? `Nama Windows → ${r.name}: menunggu PC`
    : r.state === "applied" ? `Nama Windows → ${r.name}: menunggu restart PC`
    : `Ganti nama Windows ke ${r.name} gagal: ${r.error || "tidak diketahui"}`;
}

// menu durasi kunci/buka -------------------------------------------------------
const DURATIONS = [[15, "15 menit"], [30, "30 menit"], [60, "1 jam"], [120, "2 jam"], [240, "4 jam"], [null, "Tanpa batas waktu"]];
function durationItems(mode, target, withClear) {
  const head = mode === "lock" ? "Kunci selama" : "Buka kunci selama";
  return `<li><h6 class="dropdown-header">${head}</h6></li>` +
    DURATIONS.map(([m, label]) => `<li><a class="dropdown-item" href="#" data-mode="${mode}" data-pc="${esc(target)}" data-min="${m ?? ""}">${label}</a></li>`).join("") +
    (withClear ? `<li><hr class="dropdown-divider"></li><li><a class="dropdown-item" href="#" data-clear="${esc(target)}">Ikut jadwal</a></li>` : "");
}

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-mode],[data-clear],[data-msg],[data-cmd],[data-rename],[data-wname],[data-delete]");
  if (!t) return;
  e.preventDefault();
  const d = t.dataset;
  if (d.mode) {
    let minutes = d.min === "" ? null : Number(d.min);
    if (d.ask) {                      // "Waktu tertentu": tanya jumlah menit
      const answer = prompt(`${d.mode === "lock" ? "Kunci" : "Buka kunci"} semua komputer selama berapa menit? (1 - 10080)`, "30");
      if (answer === null) return;
      minutes = Number(answer.trim());
      if (!Number.isInteger(minutes) || minutes < 1 || minutes > 10080) { alert("Isi menit dengan bilangan bulat 1 sampai 10080."); return; }
    }
    return act(d.pc === "*" ? `/api/all/${d.mode}` : `${pcUrl(d.pc)}/${d.mode}`, "POST", { minutes });
  }
  if (d.clear) return act(d.clear === "*" ? "/api/all/clear" : `${pcUrl(d.clear)}/clear`, "POST");
  if (d.msg) return openMessage(d.msg, d.label);
  if (d.cmd && d.pc === "*") {
    const label = d.cmd === "shutdown" ? "mematikan" : "me-restart";
    if (confirm(`Yakin ${label} SEMUA komputer yang sedang online? Pekerjaan yang belum disimpan di komputer-komputer itu akan hilang.`))
      return act("/api/all/command", "POST", { action: d.cmd });
    return;
  }
  if (d.cmd) {
    const label = d.cmd === "shutdown" ? "mematikan" : "me-restart";
    if (confirm(`Yakin ${label} ${d.pc}? Pekerjaan yang belum disimpan di PC itu akan hilang.`))
      return act(`${pcUrl(d.pc)}/command`, "POST", { action: d.cmd });
    return;
  }
  if (d.rename) {
    const name = prompt(`Nama tampilan untuk ${d.rename} (kosongkan untuk memakai nama asli):`, d.name);
    if (name !== null) return act(`${pcUrl(d.rename)}/name`, "PUT", { name });
    return;
  }
  if (d.wname) {
    const name = prompt(`Nama Windows baru untuk ${d.wname} (maks 15 huruf/angka/tanda -; berlaku setelah restart; PC tampil sebagai entri baru tetapi nama tampilan, pengaturan, dan log ikut pindah). Kosongkan untuk membatalkan permintaan:`, d.current);
    if (name === null) return;
    if (!name.trim()) return act(`${pcUrl(d.wname)}/windows-name`, "DELETE");
    const restart = confirm("Restart PC otomatis setelah nama diganti?\n\nOK = restart otomatis, Batal = nama berlaku pada restart berikutnya.");
    return act(`${pcUrl(d.wname)}/windows-name`, "PUT", { name: name.trim(), restart });
  }
  if (d.delete && confirm(`Hapus ${d.delete} dari daftar? PC akan terdaftar lagi bila agent-nya masih berjalan.`))
    return act(pcUrl(d.delete), "DELETE").then((ok) => { if (ok) location.href = "/"; });
});

// pesan --------------------------------------------------------------------------
let msgTarget = null, msgModal = null;
const msgPath = () => msgTarget === "*" ? "/api/all/message" : `${pcUrl(msgTarget)}/message`;
function openMessage(target, label) {
  msgTarget = target;
  msgModal = msgModal || new bootstrap.Modal($("#msgModal"));
  $("#msg-title").textContent = target === "*" ? "Pesan ke semua komputer" : `Pesan ke ${label || target}`;
  $("#msg-status").textContent = "";
  $("#msg-text").value = "";
  msgModal.show();
  setTimeout(() => $("#msg-text").focus(), 300);
}
async function msgCall(method, body, okText) {
  try { await api(msgPath(), method, body); $("#msg-status").textContent = okText; changed(); }
  catch (e) { $("#msg-status").textContent = "Gagal: " + e.message; }
}
$("#msg-send").onclick = () => {
  const min = $("#msg-min").value;
  msgCall("POST", { text: $("#msg-text").value, minutes: min === "" ? null : Number(min) }, "Terkirim ✓");
};
$("#msg-clear").onclick = () => msgCall("DELETE", undefined, "Pesan dihapus ✓");

function logHtml(rows, names) {
  return rows.map((r) => `<div><span class="t">${dateTime(r.ts)}</span>` +
    `<span class="pc">${esc(r.pc === "*" ? "Semua" : (names && names[r.pc]) || r.pc)}</span>${esc(r.detail)}</div>`).join("");
}

// unggah berkas besar per potongan, bisa lanjut bila koneksi putus (lihat labserver/uploads.py) -------------
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fatalError = (message) => Object.assign(new Error(message), { fatal: true });

function sendChunk(uid, offset, blob, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/uploads/${uid}?offset=${offset}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("X-Lab-Admin", "1");
    xhr.timeout = 120000;
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded);
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (e) { /* bukan JSON (galat proxy) */ }
      if (xhr.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname); return reject(fatalError("sesi berakhir")); }
      if (xhr.status === 200 || (xhr.status === 409 && body.resync)) return resolve(body.received);
      if (xhr.status >= 400 && xhr.status < 500) return reject(fatalError(body.error || xhr.statusText));
      reject(new Error(`HTTP ${xhr.status}`));          // 5xx dari proxy/server: coba lagi
    };
    xhr.onerror = () => reject(new Error("koneksi terputus"));
    xhr.ontimeout = () => reject(new Error("waktu habis"));
    xhr.send(blob);
  });
}

// target: {kind:"shared", path} atau {kind:"release", version}. onProgress(terkirim, total, catatan).
// control.cancelled = true menghentikan unggahan dan membuang potongan di server.
async function chunkedUpload(file, target, onProgress, control = {}) {
  const { id, chunk } = await api("/api/uploads", "POST", { ...target, size: file.size });
  const retries = 8;
  let offset = 0, failures = 0;
  while (offset < file.size) {
    if (control.cancelled) { api(`/api/uploads/${id}`, "DELETE").catch(() => {}); throw fatalError("dibatalkan"); }
    try {
      const base = offset;
      offset = await sendChunk(id, offset, file.slice(offset, offset + chunk), (n) => onProgress(base + n, file.size, ""));
      failures = 0;
      onProgress(offset, file.size, "");
    } catch (e) {
      if (e.fatal) { api(`/api/uploads/${id}`, "DELETE").catch(() => {}); throw e; }
      if (++failures > retries) throw new Error("koneksi terputus berulang kali: " + e.message);
      onProgress(offset, file.size, `koneksi bermasalah, mencoba lagi (${failures}/${retries})…`);
      await sleep(Math.min(20000, 1500 * failures));
      try { offset = (await api(`/api/uploads/${id}`)).received; } catch (e2) { /* masih putus: kirim ulang dari posisi terakhir */ }
    }
  }
  for (let attempt = 1; ; attempt++) {
    try { return await api(`/api/uploads/${id}/finish`, "POST"); }
    catch (e) {
      if (!(e instanceof TypeError) || attempt >= retries) throw e;      // hanya galat jaringan yang diulang
      onProgress(file.size, file.size, "menyelesaikan, mencoba lagi…");
      await sleep(1500 * attempt);
    }
  }
}
