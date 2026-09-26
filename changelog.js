const HISTORY_URL = "candidate-change-history.json";

const els = {
  log: document.querySelector("#changeLog"),
  search: document.querySelector("#changeSearch"),
  type: document.querySelector("#changeType"),
  clear: document.querySelector("#clearChangeFilters"),
  count: document.querySelector("#changeResultCount"),
  context: document.querySelector("#changeResultContext"),
  statSnapshots: document.querySelector("#statSnapshots"),
  statEvents: document.querySelector("#statEvents"),
  statLatestCount: document.querySelector("#statLatestCount"),
  statLatestDate: document.querySelector("#statLatestDate")
};

let history = [];

const esc = value => String(value ?? "")
  .replaceAll("&","&amp;")
  .replaceAll("<","&lt;")
  .replaceAll(">","&gt;")
  .replaceAll('"',"&quot;")
  .replaceAll("'","&#039;");

const norm = value => String(value ?? "").toLocaleLowerCase().trim();

function entryDate(entry) {
  return entry.sourceDate || String(entry.observedAtUtc || "").slice(0, 10) || "Unknown date";
}

function prettyDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const d = new Date(value + "T12:00:00Z");
  return d.toLocaleDateString("en-CA", { year:"numeric", month:"long", day:"numeric", timeZone:"UTC" });
}

function eventCount(entry) {
  return (entry.added?.length || 0)
    + (entry.removed?.length || 0)
    + (entry.statusChanges?.length || 0)
    + (entry.modified?.length || 0);
}

function flattenEvents(entry) {
  const events = [];
  (entry.added || []).forEach(x => events.push({ type:"added", label:"ADDED", ...x }));
  (entry.removed || []).forEach(x => events.push({ type:"removed", label:"REMOVED", ...x }));
  (entry.statusChanges || []).forEach(x => events.push({
    type:"status", label:"STATUS", ...x,
    detail: `${x.from || "—"} → ${x.to || "—"}`
  }));
  (entry.modified || []).forEach(x => {
    const fields = Object.entries(x.changes || {});
    if (!fields.length) {
      events.push({ type:"modified", label:"FIELD", ...x, detail:"Record modified" });
      return;
    }
    fields.forEach(([field, change]) => events.push({
      type:"modified", label:"FIELD", ...x,
      field,
      detail: `${field}: ${change?.from || "—"} → ${change?.to || "—"}`
    }));
  });
  return events;
}

function eventHtml(event) {
  const sourcePage = event.sourcePage
    ? `<span class="change-meta">Source page ${esc(event.sourcePage)}</span>`
    : "";
  const detail = event.detail
    ? `<div class="change-detail">${esc(event.detail)}</div>`
    : "";
  return `
    <article class="change-item change-${event.type}">
      <div class="change-badge">${esc(event.label)}</div>
      <div class="change-copy">
        <div class="change-name">${esc(event.candidate || "Unknown candidate")}</div>
        <div class="change-sub">${esc(event.jurisdiction || "Unknown jurisdiction")}${event.office ? " · " + esc(event.office) : ""}</div>
        ${detail}
        ${sourcePage}
      </div>
    </article>`;
}

function render() {
  const q = norm(els.search.value);
  const type = els.type.value;

  const groups = history
    .slice()
    .sort((a,b) => String(b.observedAtUtc || entryDate(b)).localeCompare(String(a.observedAtUtc || entryDate(a))))
    .map(entry => {
      const events = flattenEvents(entry).filter(ev => {
        if (type && ev.type !== type) return false;
        if (!q) return true;
        return norm([
          ev.candidate, ev.jurisdiction, ev.office, ev.affiliation,
          ev.financialAgent, ev.detail, ev.field
        ].join(" | ")).includes(q);
      });
      return { entry, events };
    })
    .filter(group => group.events.length || (!q && !type && eventCount(group.entry) === 0));

  const visibleEvents = groups.reduce((n,g) => n + g.events.length, 0);
  els.count.textContent = `${visibleEvents.toLocaleString()} change${visibleEvents === 1 ? "" : "s"}`;
  els.context.textContent = ` · across ${groups.length.toLocaleString()} revision${groups.length === 1 ? "" : "s"}`;

  if (!groups.length) {
    els.log.innerHTML = `
      <div class="empty panel">
        <div class="empty-icon">⌕</div>
        <strong>No recorded changes match those filters.</strong>
        <span>Clear a filter or try another search.</span>
      </div>`;
    return;
  }

  els.log.innerHTML = groups.map(({entry,events}) => {
    const added = entry.added?.length || 0;
    const removed = entry.removed?.length || 0;
    const status = entry.statusChanges?.length || 0;
    const modified = entry.modified?.length || 0;
    const date = entryDate(entry);
    const previous = Number.isFinite(entry.previousCandidateCount) ? entry.previousCandidateCount.toLocaleString() : "—";
    const current = Number.isFinite(entry.candidateCount) ? entry.candidateCount.toLocaleString() : "—";
    const net = Number(entry.netCandidateDelta || 0);
    const netText = net > 0 ? `+${net}` : String(net);

    return `
      <section class="change-day">
        <div class="change-day-head">
          <div>
            <div class="change-date">${esc(prettyDate(date))}</div>
            <div class="change-revision-meta">
              ${entry.reconstructed ? '<span class="reconstructed-tag">Reconstructed</span>' : '<span class="observed-tag">Observed live</span>'}
              <span>Candidate count: ${previous} → ${current}</span>
              <span>Net: ${esc(netText)}</span>
            </div>
          </div>
          <div class="change-count-pills">
            <span class="pill add">+${added} added</span>
            <span class="pill remove">−${removed} removed</span>
            <span class="pill status">${status} status</span>
            <span class="pill field">${modified} field</span>
          </div>
        </div>
        <div class="change-items">
          ${events.length ? events.map(eventHtml).join("") : '<div class="no-events">Snapshot recorded with no candidate-record changes.</div>'}
        </div>
      </section>`;
  }).join("");
}

function initialize(data) {
  history = Array.isArray(data) ? data : [];
  const totalEvents = history.reduce((n,e) => n + eventCount(e), 0);
  const latest = history.slice().sort((a,b) =>
    String(b.observedAtUtc || entryDate(b)).localeCompare(String(a.observedAtUtc || entryDate(a)))
  )[0];

  els.statSnapshots.textContent = history.length.toLocaleString();
  els.statEvents.textContent = totalEvents.toLocaleString();
  els.statLatestCount.textContent = latest?.candidateCount?.toLocaleString?.() || "—";
  els.statLatestDate.textContent = latest ? prettyDate(entryDate(latest)).replace(/, \d{4}$/, "") : "—";

  render();
}

let searchTimer;
els.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(render, 100);
});
els.type.addEventListener("change", render);
els.clear.addEventListener("click", () => {
  els.search.value = "";
  els.type.value = "";
  render();
});

fetch(HISTORY_URL, {cache:"no-store"})
  .then(r => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  })
  .then(initialize)
  .catch(err => {
    console.error(err);
    els.count.textContent = "Change history failed to load";
    els.context.textContent = "";
    els.log.innerHTML = '<div class="empty panel"><strong>Could not load candidate-change-history.json.</strong></div>';
  });
