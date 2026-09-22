const PDF_URL = "https://elections.bc.ca/docs/lecfa/Registered-Candidates-LEGE-2026-10-17.pdf";

const state = {
  data: [],
  filtered: [],
  page: 1,
  pageSize: 100,
  sortKey: "jurisdiction",
  sortDir: 1
};

const els = {
  search: document.querySelector("#search"),
  jurisdiction: document.querySelector("#jurisdictionFilter"),
  office: document.querySelector("#officeFilter"),
  affiliation: document.querySelector("#affiliationFilter"),
  pageSize: document.querySelector("#pageSize"),
  electedOnly: document.querySelector("#electedOnly"),
  clear: document.querySelector("#clearFilters"),
  body: document.querySelector("#candidateBody"),
  count: document.querySelector("#resultCount"),
  context: document.querySelector("#resultContext"),
  prev: document.querySelector("#prevPage"),
  next: document.querySelector("#nextPage"),
  pageIndicator: document.querySelector("#pageIndicator"),
  empty: document.querySelector("#emptyState"),
  table: document.querySelector("#candidateTable"),
  statCandidates: document.querySelector("#statCandidates"),
  statJurisdictions: document.querySelector("#statJurisdictions"),
  statAffiliations: document.querySelector("#statAffiliations"),
  statElected: document.querySelector("#statElected"),
  resultsStatus: document.querySelector("#resultsStatus")
};

const norm = value => String(value ?? "").toLocaleLowerCase().trim();
const uniqueSorted = (arr) => [...new Set(arr.filter(v => String(v || "").trim()))]
  .sort((a,b) => String(a).localeCompare(String(b), undefined, {sensitivity:"base"}));

function populateSelect(select, values, blankLabel) {
  select.innerHTML = `<option value="">${blankLabel}</option>`;
  const frag = document.createDocumentFragment();
  values.forEach(value => {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = value;
    frag.appendChild(opt);
  });
  select.appendChild(frag);
}

function truthyElected(v) {
  const x = norm(v);
  return ["yes","y","true","1","elected","winner","won"].includes(x);
}

function initialize(data) {
  state.data = data;
  state.filtered = data.slice();

  populateSelect(els.jurisdiction, uniqueSorted(data.map(d => d.jurisdiction)), "All jurisdictions");
  populateSelect(els.office, uniqueSorted(data.map(d => d.office)), "All offices");
  populateSelect(els.affiliation, uniqueSorted(data.map(d => d.affiliation)), "All affiliations");

  const jurisdictions = new Set(data.map(d => d.jurisdiction).filter(Boolean)).size;
  const affiliations = new Set(data.map(d => d.affiliation).filter(Boolean)).size;
  const elected = data.filter(d => truthyElected(d.elected)).length;

  els.statCandidates.textContent = data.length.toLocaleString();
  els.statJurisdictions.textContent = jurisdictions.toLocaleString();
  els.statAffiliations.textContent = affiliations.toLocaleString();
  els.statElected.textContent = elected.toLocaleString();

  if (elected > 0) {
    els.resultsStatus.textContent = `${elected.toLocaleString()} elected candidate${elected === 1 ? "" : "s"} currently marked in the dataset.`;
  }

  applyFilters();
}

function applyFilters() {
  const q = norm(els.search.value);
  const jurisdiction = els.jurisdiction.value;
  const office = els.office.value;
  const affiliation = els.affiliation.value;
  const electedOnly = els.electedOnly.checked;

  state.filtered = state.data.filter(d => {
    if (jurisdiction && d.jurisdiction !== jurisdiction) return false;
    if (office && d.office !== office) return false;
    if (affiliation && d.affiliation !== affiliation) return false;
    if (electedOnly && !truthyElected(d.elected)) return false;

    if (q) {
      const haystack = [
        d.candidate, d.jurisdiction, d.office, d.affiliation,
        d.financialAgent, d.result, d.notes
      ].map(norm).join(" | ");
      if (!haystack.includes(q)) return false;
    }
    return true;
  });

  state.page = 1;
  sortFiltered();
  render();
}

function sortFiltered() {
  const key = state.sortKey;
  const dir = state.sortDir;
  state.filtered.sort((a,b) => {
    const av = String(a[key] ?? "");
    const bv = String(b[key] ?? "");
    return av.localeCompare(bv, undefined, {numeric:true, sensitivity:"base"}) * dir;
  });
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&","&amp;")
    .replaceAll("<","&lt;")
    .replaceAll(">","&gt;")
    .replaceAll('"',"&quot;")
    .replaceAll("'","&#039;");
}

function display(value, emptyClass="cell-empty") {
  return value ? esc(value) : `<span class="${emptyClass}">—</span>`;
}

function render() {
  const total = state.filtered.length;
  state.pageSize = Number(els.pageSize.value);
  const totalPages = Math.max(1, Math.ceil(total / state.pageSize));
  if (state.page > totalPages) state.page = totalPages;

  const start = (state.page - 1) * state.pageSize;
  const end = Math.min(start + state.pageSize, total);
  const pageRows = state.filtered.slice(start, end);

  els.body.innerHTML = pageRows.map(d => {
    const elected = truthyElected(d.elected);
    const sourcePage = d.sourcePage ? Number(d.sourcePage) : null;
    const source = sourcePage
      ? `<a class="source-link" target="_blank" rel="noopener" href="${PDF_URL}#page=${sourcePage}">PDF p.${sourcePage} ↗</a>`
      : `<span class="cell-empty">—</span>`;
    const result = d.result ? esc(d.result) : `<span class="cell-empty">—</span>`;
    const electedCell = d.elected
      ? `<span class="badge ${elected ? "elected" : ""}">${esc(d.elected)}</span>`
      : `<span class="cell-empty">—</span>`;

    return `<tr>
      <td data-label="Candidate"><span class="candidate-name">${esc(d.candidate)}</span></td>
      <td data-label="Jurisdiction">${display(d.jurisdiction)}</td>
      <td data-label="Office">${display(d.office)}</td>
      <td data-label="Affiliation">${d.affiliation ? esc(d.affiliation) : `<span class="affiliation-empty">Independent / none listed</span>`}</td>
      <td data-label="Financial Agent">${display(d.financialAgent)}</td>
      <td data-label="Result">${result}</td>
      <td data-label="Elected">${electedCell}</td>
      <td data-label="Source">${source}</td>
    </tr>`;
  }).join("");

  els.count.textContent = `${total.toLocaleString()} candidate${total === 1 ? "" : "s"}`;
  els.context.textContent = total ? ` · showing ${start + 1}-${end}` : "";
  els.pageIndicator.textContent = `Page ${state.page.toLocaleString()} of ${totalPages.toLocaleString()}`;
  els.prev.disabled = state.page <= 1;
  els.next.disabled = state.page >= totalPages;
  els.table.hidden = total === 0;
  els.empty.hidden = total !== 0;

  document.querySelectorAll(".sort").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.sort === state.sortKey);
    const arrow = btn.querySelector("span");
    if (btn.dataset.sort === state.sortKey) arrow.textContent = state.sortDir === 1 ? "↑" : "↓";
    else arrow.textContent = "↕";
  });
}

let searchTimer;
els.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(applyFilters, 120);
});
[els.jurisdiction, els.office, els.affiliation, els.electedOnly].forEach(el => {
  el.addEventListener("change", applyFilters);
});
els.pageSize.addEventListener("change", () => {
  state.page = 1;
  render();
});
els.clear.addEventListener("click", () => {
  els.search.value = "";
  els.jurisdiction.value = "";
  els.office.value = "";
  els.affiliation.value = "";
  els.electedOnly.checked = false;
  applyFilters();
});
els.prev.addEventListener("click", () => {
  if (state.page > 1) {
    state.page--;
    render();
    window.scrollTo({top: document.querySelector(".results-head").offsetTop - 10, behavior:"smooth"});
  }
});
els.next.addEventListener("click", () => {
  const totalPages = Math.max(1, Math.ceil(state.filtered.length / state.pageSize));
  if (state.page < totalPages) {
    state.page++;
    render();
    window.scrollTo({top: document.querySelector(".results-head").offsetTop - 10, behavior:"smooth"});
  }
});
document.querySelectorAll(".sort").forEach(btn => {
  btn.addEventListener("click", () => {
    const key = btn.dataset.sort;
    if (state.sortKey === key) state.sortDir *= -1;
    else {
      state.sortKey = key;
      state.sortDir = 1;
    }
    sortFiltered();
    state.page = 1;
    render();
  });
});

fetch("data.json", {cache:"no-store"})
  .then(r => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  })
  .then(initialize)
  .catch(err => {
    console.error(err);
    els.count.textContent = "Database failed to load";
    els.context.textContent = "";
    els.resultsStatus.textContent = "Check that data.json was uploaded beside index.html.";
  });
