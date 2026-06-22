
const state = {
  systemKey: "",
  kind: "all",
  query: "",
};

let indexData = null;
let systems = [];
let relations = [];
let currentRecords = [];
let recordByKey = new Map();
let systemByKey = new Map();
let objectIndex = new Map();
const systemCache = new Map();
const dataScriptCache = new Map();

const systemList = document.getElementById("system-list");
const queryInput = document.getElementById("query-input");
const resultBody = document.getElementById("result-body");
const emptyState = document.getElementById("empty-state");
const activeSystemTitle = document.getElementById("active-system-title");
const summaryLine = document.getElementById("summary-line");
const dialog = document.getElementById("object-dialog");

function dataScriptPath(path) {
  return path.replace(/\.json$/, ".js");
}

function dataKeyFromPath(path) {
  const fileName = path.split("/").pop() || "";
  const key = fileName.replace(/\.json$/, "");
  try {
    return decodeURIComponent(key);
  } catch {
    return key;
  }
}

function scriptPayload(path) {
  const root = window.HWDB_DATA || {};
  if (path.endsWith("hwdb-index.json")) {
    return root.index;
  }
  return root.systems ? root.systems[dataKeyFromPath(path)] : null;
}

async function loadDataScript(path) {
  const scriptPath = dataScriptPath(path);
  if (!dataScriptCache.has(scriptPath)) {
    dataScriptCache.set(scriptPath, new Promise((resolve, reject) => {
      const script = document.createElement("script");
      const timeout = window.setTimeout(() => {
        script.remove();
        reject(new Error(`Timed out while loading ${scriptPath}`));
      }, 15000);
      script.onload = () => {
        window.clearTimeout(timeout);
        resolve();
      };
      script.onerror = () => {
        window.clearTimeout(timeout);
        reject(new Error(`Unable to load ${scriptPath}`));
      };
      script.async = true;
      script.src = scriptPath;
      document.head.appendChild(script);
    }));
  }
  await dataScriptCache.get(scriptPath);
  const payload = scriptPayload(path);
  if (!payload) {
    throw new Error(`Data script did not register ${path}`);
  }
  return payload;
}

async function loadData(path) {
  if (location.protocol === "file:") {
    return loadDataScript(path);
  }
  try {
    const response = await fetch(path);
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText} while loading ${path}`);
    }
    return response.json();
  } catch (error) {
    try {
      return await loadDataScript(path);
    } catch {
      throw error;
    }
  }
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function compact(value) {
  return value === undefined || value === null || value === "" ? "-" : value;
}

function objectHref(key) {
  return `#object=${encodeURIComponent(key)}`;
}

function objectLink(key, label, className = "object-link") {
  if (!key || !objectIndex.has(key)) {
    return esc(compact(label || key));
  }
  return `<a class="${className}" href="${objectHref(key)}">${esc(label || key)}</a>`;
}

function recordText(record) {
  return [
    record.key,
    record.kind_label,
    record.label,
    record.description,
    record.notes,
    record.source_key,
    record.category,
    record.pid_id,
    record.subsystem_label,
    record.source_label,
    record.target_label,
    record.relation_type,
    (record.fields || []).map((field) => `${field.name} ${field.description}`).join(" "),
    (record.artifacts || []).map((artifact) => `${artifact.name} ${artifact.description}`).join(" "),
  ].filter(Boolean).join(" ").toLowerCase();
}

function badgeClass(record) {
  if (record.kind === "system" || record.kind === "subsystem" || record.kind === "relation") {
    return record.kind;
  }
  if (record.is_batch || record.category === "batch") {
    return "batch";
  }
  if (record.category === "sensor") {
    return "sensor";
  }
  return "component";
}

function renderSystems() {
  systemList.innerHTML = systems.map((system) => `
    <a class="system-button ${system.key === state.systemKey ? "active" : ""}" href="#system=${encodeURIComponent(system.key)}" data-system="${esc(system.key)}">
      <span class="system-name">${esc(system.name)}</span>
      <span class="system-meta">${esc(compact(system.pid_prefix))} PID prefix · ${system.stats.subsystems} subsystems · ${system.stats.components} components</span>
    </a>
  `).join("");
}

async function loadSystem(systemKey) {
  if (!systemCache.has(systemKey)) {
    systemCache.set(systemKey, loadData(`data/systems/${encodeURIComponent(systemKey)}.json`));
  }
  return systemCache.get(systemKey);
}

function setActiveDataset(payload) {
  currentRecords = payload.records;
  relations = payload.relations;
  recordByKey = new Map(currentRecords.map((record) => [record.key, record]));
}

function matchingRecords() {
  const query = state.query.trim().toLowerCase();
  return currentRecords
    .filter((record) => state.kind === "all" || record.kind === state.kind)
    .filter((record) => !query || recordText(record).includes(query))
    .sort((left, right) => {
      const order = {system: 0, subsystem: 1, component: 2, relation: 3};
      return (order[left.kind] - order[right.kind]) || left.label.localeCompare(right.label);
    });
}

function renderResults() {
  const system = systemByKey.get(state.systemKey);
  const rows = matchingRecords();
  activeSystemTitle.textContent = system ? system.name : "Database Query";
  summaryLine.textContent = `${rows.length} rows from ${system ? system.name : "the database"}`;
  resultBody.innerHTML = rows.map((record) => {
    const context = record.kind === "relation"
      ? `${objectLink(record.source_node, record.source_label)} <span class="relation-arrow">-&gt;</span> ${objectLink(record.target_node, record.target_label)}`
      : (record.subsystem_label
        ? objectLink(record.subsystem_key, record.subsystem_label)
        : objectLink(record.system_key, record.system_label || system?.name || record.system_key));
    return `
      <tr data-key="${esc(record.key)}">
        <td><span class="badge ${badgeClass(record)}">${esc(record.kind_label)}</span></td>
        <td>
          ${objectLink(record.key, record.label)}
          <span class="object-key">${esc(record.key)}</span>
        </td>
        <td>${context}</td>
        <td>${esc(compact(record.source_key))}</td>
      </tr>
    `;
  }).join("");
  emptyState.hidden = rows.length !== 0;
}

function renderKindFilter() {
  document.querySelectorAll("[data-kind]").forEach((button) => {
    button.classList.toggle("active", button.dataset.kind === state.kind);
  });
}

async function selectSystem(systemKey, updateHash = true) {
  if (!systemByKey.has(systemKey)) {
    return;
  }
  state.systemKey = systemKey;
  renderSystems();
  summaryLine.textContent = "Loading system records...";
  const payload = await loadSystem(systemKey);
  setActiveDataset(payload);
  renderResults();
  if (updateHash) {
    history.replaceState(null, "", `#system=${encodeURIComponent(systemKey)}`);
  }
}

function detailItems(record) {
  const systemLabel = systemByKey.get(record.system_key)?.name || record.system_label || record.system_key;
  const subsystemKey = record.subsystem_key || (record.kind === "subsystem" ? record.key : "");
  const items = [
    ["Database Key", `<code>${esc(record.key)}</code>`],
    ["System", objectLink(record.system_key, systemLabel)],
    ["Type", esc(record.kind_label)],
    ["Category", esc(compact(record.category))],
    ["PID / Subsystem ID", esc(compact(record.pid_id || record.pid_prefix))],
    ["Source", esc(compact(record.source_key))],
  ];
  if (record.subsystem_label) {
    items.splice(2, 0, ["Subsystem", objectLink(subsystemKey, record.subsystem_label)]);
  }
  return items.map(([label, value]) => `
    <div class="detail-item"><span>${label}</span>${value}</div>
  `).join("");
}

function tableRows(rows, columns) {
  if (!rows.length) {
    return "";
  }
  return `
    <table class="mini-table">
      <thead><tr>${columns.map((column) => `<th>${esc(column.label)}</th>`).join("")}</tr></thead>
      <tbody>
        ${rows.map((row) => `
          <tr>${columns.map((column) => `<td>${column.render ? column.render(row) : esc(compact(row[column.key]))}</td>`).join("")}</tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function relatedRows(record) {
  if (record.kind === "relation") {
    return [];
  }
  return relations
    .filter((relation) => relation.source_node === record.key || relation.target_node === record.key)
    .map((relation) => ({
      key: relation.key,
      relation_type: relation.relation_type,
      source_key: relation.source_node,
      source: relation.source_label,
      target_key: relation.target_node,
      target: relation.target_label,
    }));
}

function renderRelationDetail(record) {
  if (record.kind !== "relation") {
    return "";
  }
  return `
    <section class="dialog-section">
      <h3>Relation</h3>
      <div class="detail-grid">
        <div class="detail-item"><span>Source</span>${objectLink(record.source_node, record.source_label)}</div>
        <div class="detail-item"><span>Target</span>${objectLink(record.target_node, record.target_label)}</div>
        <div class="detail-item"><span>Relation Type</span>${esc(record.relation_type)}</div>
        <div class="detail-item"><span>Cardinality</span>${esc(compact(record.cardinality))}</div>
      </div>
    </section>
  `;
}

function renderDialog(record) {
  const fields = tableRows(record.fields || [], [
    {key: "name", label: "Field"},
    {key: "data_type", label: "Type"},
    {key: "description", label: "Description"},
  ]);
  const artifacts = tableRows(record.artifacts || [], [
    {key: "name", label: "Artifact"},
    {key: "artifact_type", label: "Type"},
    {key: "description", label: "Description"},
  ]);
  const related = relatedRows(record);
  const relatedTable = tableRows(related, [
    {key: "relation_type", label: "Type", render: (row) => objectLink(row.key, row.relation_type)},
    {key: "source", label: "Source", render: (row) => objectLink(row.source_key, row.source)},
    {key: "target", label: "Target", render: (row) => objectLink(row.target_key, row.target)},
  ]);
  const link = `${location.origin}${location.pathname}#object=${encodeURIComponent(record.key)}`;
  dialog.innerHTML = `
    <div class="dialog-shell">
      <div class="dialog-head">
        <span class="badge ${badgeClass(record)}">${esc(record.kind_label)}</span>
        <h2 class="dialog-title">${esc(record.label)}</h2>
      </div>
      <div class="dialog-body">
        <section class="dialog-section">
          <div class="detail-grid">${detailItems(record)}</div>
        </section>
        ${record.description ? `<section class="dialog-section"><h3>Description</h3><p>${esc(record.description)}</p></section>` : ""}
        ${record.notes ? `<section class="dialog-section"><h3>Notes</h3><p>${esc(record.notes)}</p></section>` : ""}
        ${renderRelationDetail(record)}
        ${fields ? `<section class="dialog-section"><h3>Fields</h3>${fields}</section>` : ""}
        ${artifacts ? `<section class="dialog-section"><h3>Artifacts</h3>${artifacts}</section>` : ""}
        ${relatedTable ? `<section class="dialog-section"><h3>Related Relations</h3>${relatedTable}</section>` : ""}
        <section class="dialog-section"><h3>Stable Link</h3><p><a class="object-link" href="#object=${encodeURIComponent(record.key)}">${esc(link)}</a></p></section>
      </div>
      <div class="dialog-actions">
        <button type="button" id="close-dialog">Close</button>
      </div>
    </div>
  `;
  dialog.querySelector("#close-dialog").addEventListener("click", () => dialog.close());
  dialog.querySelectorAll("a[href^='#object=']").forEach((linkNode) => {
    linkNode.addEventListener("click", async (event) => {
      const params = new URLSearchParams(linkNode.hash.slice(1));
      const key = params.get("object");
      if (key && objectIndex.has(key)) {
        event.preventDefault();
        await openRecord(key, true);
      }
    });
  });
}

async function openRecord(key, updateHash = true) {
  const indexed = objectIndex.get(key);
  if (!indexed) {
    return;
  }
  if (indexed.system_key !== state.systemKey) {
    await selectSystem(indexed.system_key, false);
  }
  const record = recordByKey.get(key);
  if (!record) {
    return;
  }
  renderDialog(record);
  if (!dialog.open) {
    dialog.showModal();
  }
  if (updateHash) {
    history.replaceState(null, "", `#object=${encodeURIComponent(key)}`);
  }
}

async function syncFromHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  const objectKey = params.get("object");
  const systemKey = params.get("system");
  if (objectKey && objectIndex.has(objectKey)) {
    await openRecord(objectKey, false);
    return;
  }
  if (systemKey && systemByKey.has(systemKey)) {
    await selectSystem(systemKey, false);
    return;
  }
  if (!state.systemKey && systems.length) {
    await selectSystem(systems[0].key, false);
  }
}

function showLoadError(error) {
  activeSystemTitle.textContent = "Database Query";
  summaryLine.textContent = "Unable to load explorer data.";
  emptyState.hidden = false;
  emptyState.textContent = `${error.message}. Open this page through GitHub Pages or run: python3 -m http.server 8765 --directory docs, then browse to http://127.0.0.1:8765/explorer.html.`;
}

async function init() {
  try {
    indexData = await loadData("data/hwdb-index.json");
    systems = indexData.systems;
    systemByKey = new Map(systems.map((system) => [system.key, system]));
    objectIndex = new Map(Object.entries(indexData.object_index));
    renderSystems();
    renderKindFilter();
    await syncFromHash();
  } catch (error) {
    showLoadError(error);
  }
}

systemList.addEventListener("click", async (event) => {
  const link = event.target.closest("[data-system]");
  if (!link) {
    return;
  }
  event.preventDefault();
  await selectSystem(link.dataset.system);
});

resultBody.addEventListener("click", async (event) => {
  const objectNode = event.target.closest("a[href^='#object=']");
  if (objectNode) {
    const params = new URLSearchParams(objectNode.hash.slice(1));
    const key = params.get("object");
    if (key && objectIndex.has(key)) {
      event.preventDefault();
      await openRecord(key);
      return;
    }
  }
  const row = event.target.closest("tr[data-key]");
  if (!row) {
    return;
  }
  event.preventDefault();
  await openRecord(row.dataset.key);
});

document.getElementById("kind-filter").addEventListener("click", (event) => {
  const button = event.target.closest("[data-kind]");
  if (!button) {
    return;
  }
  state.kind = button.dataset.kind;
  renderKindFilter();
  renderResults();
});

queryInput.addEventListener("input", () => {
  state.query = queryInput.value;
  renderResults();
});

window.addEventListener("hashchange", () => {
  syncFromHash();
});

init();
