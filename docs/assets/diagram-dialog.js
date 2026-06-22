
const hwdbDialogScript = document.currentScript
  || Array.from(document.scripts).find((script) => script.src && script.src.endsWith("assets/diagram-dialog.js"));
const hwdbSiteRoot = hwdbDialogScript ? new URL("../", hwdbDialogScript.src) : new URL("./", location.href);
const hwdbSystemCache = new Map();
const hwdbDataScriptCache = new Map();

let hwdbIndexData = null;
let hwdbSystemByKey = new Map();
let hwdbObjectIndex = new Map();

function hwdbDataUrl(path) {
  return new URL(path, hwdbSiteRoot).href;
}

function hwdbDataScriptPath(path) {
  return path.replace(/\.json$/, ".js");
}

function hwdbDataKeyFromPath(path) {
  const fileName = new URL(path, location.href).pathname.split("/").pop() || "";
  const key = fileName.replace(/\.json$/, "");
  try {
    return decodeURIComponent(key);
  } catch {
    return key;
  }
}

function hwdbScriptPayload(path) {
  const root = window.HWDB_DATA || {};
  if (path.endsWith("hwdb-index.json")) {
    return root.index;
  }
  return root.systems ? root.systems[hwdbDataKeyFromPath(path)] : null;
}

async function hwdbLoadDataScript(path) {
  const scriptPath = hwdbDataScriptPath(path);
  if (!hwdbDataScriptCache.has(scriptPath)) {
    hwdbDataScriptCache.set(scriptPath, new Promise((resolve, reject) => {
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
  await hwdbDataScriptCache.get(scriptPath);
  const payload = hwdbScriptPayload(path);
  if (!payload) {
    throw new Error(`Data script did not register ${path}`);
  }
  return payload;
}

async function hwdbLoadData(path) {
  const url = hwdbDataUrl(path);
  if (location.protocol === "file:") {
    return hwdbLoadDataScript(url);
  }
  try {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText} while loading ${url}`);
    }
    return response.json();
  } catch (error) {
    try {
      return await hwdbLoadDataScript(url);
    } catch {
      throw error;
    }
  }
}

async function hwdbEnsureIndex() {
  if (!hwdbIndexData) {
    hwdbIndexData = await hwdbLoadData("data/hwdb-index.json");
    hwdbSystemByKey = new Map(hwdbIndexData.systems.map((system) => [system.key, system]));
    hwdbObjectIndex = new Map(Object.entries(hwdbIndexData.object_index));
  }
  return hwdbIndexData;
}

async function hwdbLoadSystem(systemKey) {
  if (!hwdbSystemCache.has(systemKey)) {
    hwdbSystemCache.set(systemKey, hwdbLoadData(`data/systems/${encodeURIComponent(systemKey)}.json`));
  }
  return hwdbSystemCache.get(systemKey);
}

function hwdbEsc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function hwdbCompact(value) {
  return value === undefined || value === null || value === "" ? "-" : value;
}

function hwdbBadgeClass(record) {
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

function hwdbObjectLink(key, label) {
  if (!key || !hwdbObjectIndex.has(key)) {
    return hwdbEsc(hwdbCompact(label || key));
  }
  return `<a class="hwdb-object-link" href="#object=${encodeURIComponent(key)}" data-object-key="${hwdbEsc(key)}">${hwdbEsc(label || key)}</a>`;
}

function hwdbEnsureDialog() {
  let dialog = document.getElementById("hwdb-object-dialog");
  if (!dialog) {
    dialog = document.createElement("dialog");
    dialog.id = "hwdb-object-dialog";
    dialog.className = "hwdb-object-dialog";
    dialog.addEventListener("click", (event) => {
      if (event.target.closest("[data-dialog-close]")) {
        dialog.close();
      }
    });
    document.body.appendChild(dialog);
  }
  return dialog;
}

function hwdbDetailItems(record) {
  const systemLabel = hwdbSystemByKey.get(record.system_key)?.name || record.system_label || record.system_key;
  const subsystemKey = record.subsystem_key || (record.kind === "subsystem" ? record.key : "");
  const items = [
    ["Database Key", `<code>${hwdbEsc(record.key)}</code>`],
    ["System", hwdbObjectLink(record.system_key, systemLabel)],
    ["Type", hwdbEsc(record.kind_label)],
    ["Category", hwdbEsc(hwdbCompact(record.category))],
    ["PID / Subsystem ID", hwdbEsc(hwdbCompact(record.pid_id || record.pid_prefix))],
    ["Source", hwdbEsc(hwdbCompact(record.source_key))],
  ];
  if (record.subsystem_label) {
    items.splice(2, 0, ["Subsystem", hwdbObjectLink(subsystemKey, record.subsystem_label)]);
  }
  return items.map(([label, value]) => `
    <div class="hwdb-detail-item"><span>${label}</span>${value}</div>
  `).join("");
}

function hwdbTableRows(rows, columns) {
  if (!rows.length) {
    return "";
  }
  return `
    <table class="hwdb-mini-table">
      <thead><tr>${columns.map((column) => `<th>${hwdbEsc(column.label)}</th>`).join("")}</tr></thead>
      <tbody>
        ${rows.map((row) => `
          <tr>${columns.map((column) => `<td>${column.render ? column.render(row) : hwdbEsc(hwdbCompact(row[column.key]))}</td>`).join("")}</tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function hwdbRelatedRows(record, relations) {
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

function hwdbRelationDetail(record) {
  if (record.kind !== "relation") {
    return "";
  }
  return `
    <section class="hwdb-dialog-section">
      <h3>Relation</h3>
      <div class="hwdb-detail-grid">
        <div class="hwdb-detail-item"><span>Source</span>${hwdbObjectLink(record.source_node, record.source_label)}</div>
        <div class="hwdb-detail-item"><span>Target</span>${hwdbObjectLink(record.target_node, record.target_label)}</div>
        <div class="hwdb-detail-item"><span>Relation Type</span>${hwdbEsc(record.relation_type)}</div>
        <div class="hwdb-detail-item"><span>Cardinality</span>${hwdbEsc(hwdbCompact(record.cardinality))}</div>
      </div>
    </section>
  `;
}

function hwdbRecordMap(payload) {
  return new Map(payload.records.map((record) => [record.key, record]));
}

function hwdbRelationLabel(edge) {
  return (edge.relation_type || edge.type || "related_to").replace(/_/g, " ");
}

function hwdbGraphEdges(payload) {
  const recordsByKey = hwdbRecordMap(payload);
  const edges = (payload.relations || []).map((relation) => ({
    key: relation.key,
    source: relation.source_node,
    target: relation.target_node,
    type: relation.relation_type,
    relation_type: relation.relation_type,
    label: hwdbRelationLabel(relation),
    cardinality: relation.cardinality,
    synthetic: false,
  }));

  payload.records.forEach((record) => {
    if (record.kind === "subsystem" && record.system_key && record.key !== record.system_key) {
      edges.push({
        key: `synthetic:system:${record.system_key}:${record.key}`,
        source: record.system_key,
        target: record.key,
        type: "subsystem",
        relation_type: "subsystem",
        label: "has subsystem",
        synthetic: true,
      });
    }
    if (record.kind === "component" && record.subsystem_key) {
      edges.push({
        key: `synthetic:subsystem:${record.subsystem_key}:${record.key}`,
        source: record.subsystem_key,
        target: record.key,
        type: "member",
        relation_type: "member",
        label: "in subsystem",
        synthetic: true,
      });
    }
  });

  return edges.filter((edge) => recordsByKey.has(edge.source) && recordsByKey.has(edge.target));
}

function hwdbEdgePriority(edge) {
  const order = {
    contains: 0,
    subsystem: 1,
    member: 2,
    connects_to: 3,
    interfaces_with: 4,
    reads_out: 5,
    powers: 6,
    timed_by: 7,
    depends_on: 8,
  };
  return order[edge.type] ?? 20;
}

function hwdbNeighborhood(payload, record, depth = 1, maxNodes = 34) {
  const recordsByKey = hwdbRecordMap(payload);
  const allEdges = hwdbGraphEdges(payload);

  if (record.kind === "relation") {
    const nodes = [record.source_node, record.target_node].filter((key) => recordsByKey.has(key));
    return {
      focusKey: record.key,
      nodeKeys: new Set(nodes),
      levels: new Map(nodes.map((key) => [key, key === record.source_node ? -1 : 1])),
      edges: [{
        key: record.key,
        source: record.source_node,
        target: record.target_node,
        type: record.relation_type,
        relation_type: record.relation_type,
        label: hwdbRelationLabel(record),
        cardinality: record.cardinality,
        synthetic: false,
        focus: true,
      }].filter((edge) => recordsByKey.has(edge.source) && recordsByKey.has(edge.target)),
      recordsByKey,
      truncated: false,
    };
  }

  const adjacency = new Map();
  allEdges.forEach((edge) => {
    if (!adjacency.has(edge.source)) {
      adjacency.set(edge.source, []);
    }
    if (!adjacency.has(edge.target)) {
      adjacency.set(edge.target, []);
    }
    adjacency.get(edge.source).push(edge);
    adjacency.get(edge.target).push(edge);
  });

  adjacency.forEach((edges) => {
    edges.sort((left, right) => {
      const priority = hwdbEdgePriority(left) - hwdbEdgePriority(right);
      if (priority !== 0) {
        return priority;
      }
      return left.label.localeCompare(right.label);
    });
  });

  const nodeKeys = new Set([record.key]);
  const levels = new Map([[record.key, 0]]);
  const queue = [record.key];
  for (let index = 0; index < queue.length; index += 1) {
    const current = queue[index];
    const currentLevel = levels.get(current) || 0;
    if (Math.abs(currentLevel) >= depth) {
      continue;
    }
    for (const edge of adjacency.get(current) || []) {
      const outbound = edge.source === current;
      const neighbor = outbound ? edge.target : edge.source;
      const nextLevel = currentLevel + (outbound ? 1 : -1);
      if (Math.abs(nextLevel) > depth) {
        continue;
      }
      const existing = levels.get(neighbor);
      if (existing === undefined || Math.abs(nextLevel) < Math.abs(existing)) {
        levels.set(neighbor, nextLevel);
        nodeKeys.add(neighbor);
        queue.push(neighbor);
      }
    }
  }

  let truncated = false;
  if (nodeKeys.size > maxNodes) {
    truncated = true;
    const sorted = Array.from(nodeKeys).sort((left, right) => {
      if (left === record.key) {
        return -1;
      }
      if (right === record.key) {
        return 1;
      }
      const leftLevel = levels.get(left) || 0;
      const rightLevel = levels.get(right) || 0;
      const distance = Math.abs(leftLevel) - Math.abs(rightLevel);
      if (distance !== 0) {
        return distance;
      }
      if (leftLevel !== rightLevel) {
        return leftLevel - rightLevel;
      }
      return (recordsByKey.get(left)?.label || left).localeCompare(recordsByKey.get(right)?.label || right);
    });
    nodeKeys.clear();
    sorted.slice(0, maxNodes).forEach((key) => nodeKeys.add(key));
  }

  const edges = allEdges.filter((edge) => nodeKeys.has(edge.source) && nodeKeys.has(edge.target));
  return {focusKey: record.key, nodeKeys, levels, edges, recordsByKey, truncated};
}

function hwdbWrapLabel(label, maxChars = 24, maxLines = 3) {
  const words = String(label || "").split(/\s+/).filter(Boolean);
  const lines = [];
  let current = "";
  words.forEach((word) => {
    const next = current ? `${current} ${word}` : word;
    if (next.length > maxChars && current) {
      lines.push(current);
      current = word;
    } else {
      current = next;
    }
  });
  if (current) {
    lines.push(current);
  }
  if (lines.length > maxLines) {
    const clipped = lines.slice(0, maxLines);
    clipped[maxLines - 1] = `${clipped[maxLines - 1].replace(/\.+$/, "")}...`;
    return clipped;
  }
  return lines.length ? lines : ["-"];
}

function hwdbNodeText(label, x, y, width) {
  const lines = hwdbWrapLabel(label);
  const startY = y - ((lines.length - 1) * 7);
  return lines.map((line, index) => (
    `<text x="${x + width / 2}" y="${startY + index * 15}" text-anchor="middle" class="hwdb-graph-node-label">${hwdbEsc(line)}</text>`
  )).join("");
}

function hwdbGraphNodeClass(record, focusKey) {
  const classes = ["hwdb-graph-node", hwdbBadgeClass(record)];
  if (record.key === focusKey) {
    classes.push("focus");
  }
  return classes.join(" ");
}

function hwdbEdgeClass(edge) {
  if (edge.type === "contains" || edge.type === "subsystem" || edge.type === "member") {
    return "hwdb-graph-edge hierarchy";
  }
  return "hwdb-graph-edge dependency";
}

function hwdbRenderNeighborhoodSvg(payload, record, depth) {
  const graph = hwdbNeighborhood(payload, record, depth);
  const nodeWidth = 210;
  const nodeHeight = 62;
  const columnGap = 110;
  const rowGap = 42;
  const pad = 40;
  const levels = Array.from(new Set(Array.from(graph.nodeKeys).map((key) => graph.levels.get(key) || 0))).sort((a, b) => a - b);
  const levelIndex = new Map(levels.map((level, index) => [level, index]));
  const nodesByLevel = new Map(levels.map((level) => [level, []]));

  Array.from(graph.nodeKeys).forEach((key) => {
    const level = graph.levels.get(key) || 0;
    nodesByLevel.get(level).push(key);
  });
  nodesByLevel.forEach((keys) => {
    keys.sort((left, right) => {
      if (left === record.key) {
        return -1;
      }
      if (right === record.key) {
        return 1;
      }
      return (graph.recordsByKey.get(left)?.label || left).localeCompare(graph.recordsByKey.get(right)?.label || right);
    });
  });

  const maxRows = Math.max(1, ...Array.from(nodesByLevel.values()).map((keys) => keys.length));
  const width = Math.max(640, pad * 2 + levels.length * nodeWidth + Math.max(0, levels.length - 1) * columnGap);
  const height = Math.max(300, pad * 2 + maxRows * nodeHeight + Math.max(0, maxRows - 1) * rowGap);
  const positions = new Map();

  levels.forEach((level) => {
    const keys = nodesByLevel.get(level);
    const x = pad + (levelIndex.get(level) || 0) * (nodeWidth + columnGap);
    const groupHeight = keys.length * nodeHeight + Math.max(0, keys.length - 1) * rowGap;
    const startY = pad + Math.max(0, (height - pad * 2 - groupHeight) / 2);
    keys.forEach((key, row) => {
      positions.set(key, {x, y: startY + row * (nodeHeight + rowGap)});
    });
  });

  const edgeMarkup = graph.edges.map((edge) => {
    const source = positions.get(edge.source);
    const target = positions.get(edge.target);
    if (!source || !target) {
      return "";
    }
    let sx;
    let sy;
    let tx;
    let ty;
    let path;
    if (Math.abs(source.x - target.x) < 2) {
      sx = source.x + nodeWidth / 2;
      sy = source.y < target.y ? source.y + nodeHeight : source.y;
      tx = target.x + nodeWidth / 2;
      ty = source.y < target.y ? target.y : target.y + nodeHeight;
      const bend = source.y < target.y ? 36 : -36;
      path = `M ${sx} ${sy} C ${sx + bend} ${sy + 24}, ${tx + bend} ${ty - 24}, ${tx} ${ty}`;
    } else {
      const forward = source.x < target.x;
      sx = forward ? source.x + nodeWidth : source.x;
      sy = source.y + nodeHeight / 2;
      tx = forward ? target.x : target.x + nodeWidth;
      ty = target.y + nodeHeight / 2;
      const curve = Math.max(42, Math.abs(tx - sx) * 0.45);
      path = `M ${sx} ${sy} C ${sx + (forward ? curve : -curve)} ${sy}, ${tx + (forward ? -curve : curve)} ${ty}, ${tx} ${ty}`;
    }
    const label = `${edge.label}${edge.cardinality ? ` (${edge.cardinality})` : ""}`;
    const labelX = Math.round((sx + tx) / 2);
    const labelY = Math.round((sy + ty) / 2) - 8;
    const linkAttrs = edge.synthetic ? "" : ` href="#object=${encodeURIComponent(edge.key)}" data-object-key="${hwdbEsc(edge.key)}"`;
    const labelText = `<text x="${labelX}" y="${labelY}" text-anchor="middle" class="hwdb-graph-edge-label">${hwdbEsc(label)}</text>`;
    const edgeBody = `<path d="${path}" class="${hwdbEdgeClass(edge)}" marker-end="url(#hwdb-arrow)"></path>${labelText}`;
    return edge.synthetic ? edgeBody : `<a${linkAttrs}>${edgeBody}</a>`;
  }).join("");

  const nodeMarkup = Array.from(graph.nodeKeys).map((key) => {
    const node = graph.recordsByKey.get(key);
    const position = positions.get(key);
    if (!node || !position) {
      return "";
    }
    const labelY = position.y + nodeHeight / 2 + 5;
    return `
      <a href="#object=${encodeURIComponent(key)}" data-object-key="${hwdbEsc(key)}">
        <g class="${hwdbGraphNodeClass(node, graph.focusKey)}" transform="translate(${position.x}, ${position.y})">
          <rect width="${nodeWidth}" height="${nodeHeight}" rx="0" ry="0"></rect>
          <text x="12" y="17" class="hwdb-graph-node-kind">${hwdbEsc(node.kind_label || node.kind)}</text>
          ${hwdbNodeText(node.label, 0, labelY, nodeWidth)}
        </g>
      </a>
    `;
  }).join("");

  const empty = graph.nodeKeys.size === 0
    ? '<p class="hwdb-neighborhood-empty">No local graph is available for this object.</p>'
    : "";
  const truncated = graph.truncated
    ? '<p class="hwdb-neighborhood-note">Showing the nearest objects first. Increase precision by selecting a closer node.</p>'
    : "";
  return `
    <p class="hwdb-neighborhood-summary">${graph.nodeKeys.size} objects · ${graph.edges.length} links · depth ${depth}</p>
    ${empty || `<svg class="hwdb-neighborhood-svg" role="img" aria-label="Local object graph" viewBox="0 0 ${width} ${height}" style="width:${width}px;height:${height}px">
      <defs>
        <marker id="hwdb-arrow" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
          <path d="M 0 0 L 10 4 L 0 8 z" class="hwdb-graph-arrow"></path>
        </marker>
      </defs>
      <g class="hwdb-graph-edges">${edgeMarkup}</g>
      <g class="hwdb-graph-nodes">${nodeMarkup}</g>
    </svg>`}
    ${truncated}
  `;
}

function hwdbNeighborhoodSection(payload, record, depth = 1) {
  return `
    <section class="hwdb-dialog-section hwdb-neighborhood-section">
      <div class="hwdb-neighborhood-head">
        <h3>Local Diagram</h3>
        <div class="hwdb-graph-toolbar" aria-label="Local graph depth">
          <button type="button" data-graph-depth="1" class="active">1 level</button>
          <button type="button" data-graph-depth="2">2 levels</button>
          <button type="button" data-graph-depth="3">3 levels</button>
        </div>
      </div>
      <div class="hwdb-neighborhood-frame" data-neighborhood-frame>
        ${hwdbRenderNeighborhoodSvg(payload, record, depth)}
      </div>
    </section>
  `;
}

function hwdbWireNeighborhoodControls(dialog, payload, record) {
  const frame = dialog.querySelector("[data-neighborhood-frame]");
  if (!frame) {
    return;
  }
  dialog.querySelectorAll("[data-graph-depth]").forEach((button) => {
    button.addEventListener("click", () => {
      dialog.querySelectorAll("[data-graph-depth]").forEach((item) => {
        item.classList.toggle("active", item === button);
      });
      frame.innerHTML = hwdbRenderNeighborhoodSvg(payload, record, Number(button.dataset.graphDepth || "1"));
    });
  });
}

async function hwdbRecordContext(key) {
  await hwdbEnsureIndex();
  const indexed = hwdbObjectIndex.get(key);
  if (!indexed) {
    throw new Error(`No database object found for ${key}`);
  }
  const payload = await hwdbLoadSystem(indexed.system_key);
  const record = payload.records.find((item) => item.key === key);
  if (!record) {
    throw new Error(`No record found for ${key}`);
  }
  return {record, payload};
}

async function hwdbOpenObject(key) {
  const dialog = hwdbEnsureDialog();
  dialog.innerHTML = `
    <div class="hwdb-dialog-shell">
      <div class="hwdb-dialog-head">
        <p class="hwdb-dialog-kicker">Database Object</p>
        <h2>Loading...</h2>
      </div>
    </div>
  `;
  if (!dialog.open) {
    dialog.showModal();
  }
  try {
    const {record, payload} = await hwdbRecordContext(key);
    const fields = hwdbTableRows(record.fields || [], [
      {key: "name", label: "Field"},
      {key: "data_type", label: "Type"},
      {key: "description", label: "Description"},
    ]);
    const artifacts = hwdbTableRows(record.artifacts || [], [
      {key: "name", label: "Artifact"},
      {key: "artifact_type", label: "Type"},
      {key: "description", label: "Description"},
    ]);
    const related = hwdbRelatedRows(record, payload.relations || []);
    const relatedTable = hwdbTableRows(related, [
      {key: "relation_type", label: "Type", render: (row) => hwdbObjectLink(row.key, row.relation_type)},
      {key: "source", label: "Source", render: (row) => hwdbObjectLink(row.source_key, row.source)},
      {key: "target", label: "Target", render: (row) => hwdbObjectLink(row.target_key, row.target)},
    ]);
    dialog.innerHTML = `
      <div class="hwdb-dialog-shell">
        <div class="hwdb-dialog-head">
          <span class="hwdb-dialog-badge ${hwdbBadgeClass(record)}">${hwdbEsc(record.kind_label)}</span>
          <h2>${hwdbEsc(record.label)}</h2>
        </div>
        <div class="hwdb-dialog-body">
          ${hwdbNeighborhoodSection(payload, record)}
          <section class="hwdb-dialog-section">
            <div class="hwdb-detail-grid">${hwdbDetailItems(record)}</div>
          </section>
          ${record.description ? `<section class="hwdb-dialog-section"><h3>Description</h3><p>${hwdbEsc(record.description)}</p></section>` : ""}
          ${record.notes ? `<section class="hwdb-dialog-section"><h3>Notes</h3><p>${hwdbEsc(record.notes)}</p></section>` : ""}
          ${hwdbRelationDetail(record)}
          ${fields ? `<section class="hwdb-dialog-section"><h3>Fields</h3>${fields}</section>` : ""}
          ${artifacts ? `<section class="hwdb-dialog-section"><h3>Artifacts</h3>${artifacts}</section>` : ""}
          ${relatedTable ? `<section class="hwdb-dialog-section"><h3>Related Relations</h3>${relatedTable}</section>` : ""}
        </div>
        <div class="hwdb-dialog-actions">
          <button type="button" data-dialog-close>Close</button>
        </div>
      </div>
    `;
    hwdbWireNeighborhoodControls(dialog, payload, record);
  } catch (error) {
    dialog.innerHTML = `
      <div class="hwdb-dialog-shell">
        <div class="hwdb-dialog-head">
          <p class="hwdb-dialog-kicker">Database Object</p>
          <h2>Unable to load object</h2>
        </div>
        <div class="hwdb-dialog-body">
          <p>${hwdbEsc(error.message)}</p>
        </div>
        <div class="hwdb-dialog-actions">
          <button type="button" data-dialog-close>Close</button>
        </div>
      </div>
    `;
  }
}

function hwdbFindAnchor(node) {
  let current = node;
  while (current && current.nodeType === 1) {
    if (current.localName && current.localName.toLowerCase() === "a") {
      return current;
    }
    current = current.parentNode;
  }
  return null;
}

function hwdbHrefValue(link) {
  return link.getAttribute("href")
    || link.getAttributeNS("http://www.w3.org/1999/xlink", "href")
    || (link.href && (typeof link.href === "string" ? link.href : link.href.baseVal))
    || "";
}

function hwdbObjectKeyFromHref(href, base) {
  if (!href) {
    return "";
  }
  try {
    const url = new URL(href, base || location.href);
    return new URLSearchParams(url.hash.slice(1)).get("object") || "";
  } catch {
    const marker = "#object=";
    const index = href.indexOf(marker);
    return index >= 0 ? decodeURIComponent(href.slice(index + marker.length)) : "";
  }
}

function hwdbHandleObjectClick(event) {
  const explicit = event.target.closest ? event.target.closest("[data-object-key]") : null;
  const link = explicit || hwdbFindAnchor(event.target);
  if (!link) {
    return;
  }
  const base = link.ownerDocument?.location?.href || location.href;
  const key = link.dataset?.objectKey || hwdbObjectKeyFromHref(hwdbHrefValue(link), base);
  if (!key) {
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  hwdbOpenObject(key);
}

function hwdbAttachFrame(frame) {
  try {
    const doc = frame.contentDocument;
    if (!doc || doc.__hwdbDialogAttached) {
      return;
    }
    doc.__hwdbDialogAttached = true;
    doc.addEventListener("click", hwdbHandleObjectClick, true);
  } catch {
    // Cross-origin or not-yet-loaded frames are ignored; generated diagrams are same-origin.
  }
}

function hwdbScanDiagramFrames() {
  document.querySelectorAll("iframe.diagram-object").forEach((frame) => {
    if (!frame.dataset.hwdbDialogFrame) {
      frame.dataset.hwdbDialogFrame = "1";
      frame.addEventListener("load", () => hwdbAttachFrame(frame));
    }
    hwdbAttachFrame(frame);
  });
}

document.addEventListener("click", hwdbHandleObjectClick, true);

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", hwdbScanDiagramFrames);
} else {
  hwdbScanDiagramFrames();
}

window.addEventListener("load", hwdbScanDiagramFrames);

new MutationObserver(hwdbScanDiagramFrames).observe(document.documentElement, {
  childList: true,
  subtree: true,
});

window.HWDB_DIALOG = {open: hwdbOpenObject};
