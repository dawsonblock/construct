// Construction AI approval UI — v0.4.3 security hardening.
//
// All dynamic content is inserted via textContent / createElement, never
// innerHTML. This prevents XSS from invoice references, evidence values, check
// names, or any other server-returned data. The page is served with a strict
// CSP (script-src 'self', style-src 'self', connect-src 'self') that blocks
// inline scripts, inline handlers, and external connections.
"use strict";

const q = new URLSearchParams(location.search);
const id = q.get("id");

// The server derives the organization from this key. The page never sends an
// organization id, because a caller-supplied one would not be trusted anyway.
function key() {
  let k = sessionStorage.getItem("cai_key");
  if (!k) {
    k = prompt("Organization API key");
    if (k) sessionStorage.setItem("cai_key", k);
  }
  return k || "";
}

function headers(extra) {
  return Object.assign({ authorization: "Bearer " + key() }, extra || {});
}

// Safe element constructor: tag + text content + optional class.
function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}

function setMeta(text) {
  document.querySelector("#meta").textContent = text;
}

function clearChecks() {
  const c = document.querySelector("#checks");
  while (c.firstChild) c.removeChild(c.firstChild);
}

function renderChecks(exceptions) {
  clearChecks();
  const container = document.querySelector("#checks");
  if (exceptions && exceptions.length) {
    container.appendChild(el("p", "Exceptions: " + exceptions.join(", "), "bad"));
  } else {
    container.appendChild(el("p", "No verification exceptions.", "ok"));
  }
}

function renderPacket(packet) {
  const container = document.querySelector("#packet");
  while (container.firstChild) container.removeChild(container.firstChild);

  const checks = (packet.verification && packet.verification.checks) || {};
  const evidence = packet.evidence || [];

  if (Object.keys(checks).length || evidence.length) {
    const grid = el("div", null, "grid");
    for (const [name, ok] of Object.entries(checks)) {
      const pill = el("div", null, "pill");
      pill.textContent = (ok ? "\u2713 " : "\u2717 ") + name;
      grid.appendChild(pill);
    }
    container.appendChild(grid);

    container.appendChild(el("h3", "Evidence (" + evidence.length + ")"));
    const pre = el("pre");
    for (const e of evidence) {
      const line = e.field + ": " + JSON.stringify(e.value) +
        "\n  " + e.source_type + "/" + e.source_id +
        " \u2022 confidence " + e.confidence +
        " \u2022 authority " + e.authority;
      pre.appendChild(document.createTextNode(line + "\n\n"));
    }
    container.appendChild(pre);
  } else {
    container.textContent = "No packet loaded.";
  }
}

async function load() {
  if (!id) return;
  let r = await fetch("/approvals/" + id, { headers: headers() });
  if (r.status === 401) {
    sessionStorage.removeItem("cai_key");
    setMeta("Authentication failed \u2014 reload to re-enter the API key.");
    return;
  }
  if (!r.ok) {
    setMeta("Approval not found.");
    return;
  }
  const a = await r.json();
  document.querySelector("#subject").textContent = a.reference || a.subject_id;
  let metaText = "$" + (a.amount != null ? a.amount : "") + " \u2022 " + a.status;
  if (a.decided_by) metaText += " \u2022 by " + a.decided_by;
  setMeta(metaText);
  renderChecks(a.exceptions || []);

  let p = await fetch("/approvals/" + id + "/packet", { headers: headers() });
  if (p.ok) {
    const x = await p.json();
    renderPacket(x);
  }
}

async function session() {
  let s = sessionStorage.getItem("cai_session");
  if (s) return s;
  const subject = prompt("Approver identity (dev subject)");
  if (!subject) return null;
  let r = await fetch("/auth/session", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ credential: subject }),
  });
  if (!r.ok) {
    const body = await r.json();
    alert((body && body.detail) || "login failed");
    return null;
  }
  const body = await r.json();
  const t = body.session_token;
  if (t) sessionStorage.setItem("cai_session", t);
  return t || null;
}

async function act(x) {
  const t = await session();
  if (!t) return;
  let r = await fetch("/approvals/" + id + "/" + x, {
    method: "POST",
    headers: { authorization: "Bearer " + t, "content-type": "application/json" },
    body: JSON.stringify({ reason: "" }),
  });
  if (r.status === 401) {
    sessionStorage.removeItem("cai_session");
    alert("Session expired \u2014 reload to log in again.");
    return;
  }
  if (!r.ok) {
    const body = await r.json();
    alert((body && body.detail) || "decision rejected");
  }
  load();
}

// Event listeners (no inline handlers — required by the strict CSP).
document.addEventListener("DOMContentLoaded", function () {
  document.querySelector("#btn-approve").addEventListener("click", function () { act("approve"); });
  document.querySelector("#btn-hold").addEventListener("click", function () { act("hold"); });
  document.querySelector("#btn-reject").addEventListener("click", function () { act("reject"); });
  load();
});
