/* Streamlit CCv2: browser-local table preferences, never table contents. */
const instances = new WeakMap();
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const validWidth = (value) => typeof value === "number" && Number.isInteger(value) && value >= 40 && value <= 2000;
const validHeight = (value) => typeof value === "number" && Number.isInteger(value) && value >= 200 && value <= 5000;
const normalizedHeight = (value, fallback = null) => value === null || value === "content" || validHeight(value) ? value : fallback;
const VIEW_RETRY_DELAYS = [300, 800, 1800, 3500];

function schemaFor(columns) {
  const seen = new Set();
  return (Array.isArray(columns) ? columns : []).flatMap((column) => {
    if (!object(column) || typeof column.id !== "string" || !column.id || seen.has(column.id)) return [];
    seen.add(column.id);
    return [{ id: column.id, label: String(column.label ?? column.id), width: validWidth(column.width) ? column.width : undefined }];
  });
}

function defaultView(schema, raw) {
  const source = object(raw) && raw.version === 1 ? raw : {};
  const ids = schema.map((column) => column.id);
  const allowed = new Set(ids);
  const order = [...new Set((Array.isArray(source.columns) ? source.columns : []).filter((id) => allowed.has(id)))];
  ids.forEach((id) => { if (!order.includes(id)) order.push(id); });
  const hidden = new Set(Array.isArray(source.hidden) ? source.hidden : []);
  const rawWidths = object(source.widths) ? source.widths : {};
  const widths = Object.fromEntries(schema.flatMap((column) => {
    const width = own(rawWidths, column.id) && validWidth(rawWidths[column.id]) ? rawWidths[column.id] : column.width;
    return validWidth(width) ? [[column.id, width]] : [];
  }));
  const seenSort = new Set();
  const sort = (Array.isArray(source.sort) ? source.sort : []).flatMap((item) => {
    if (!object(item) || !allowed.has(item.column) || seenSort.has(item.column) || !["asc", "desc"].includes(item.direction)) return [];
    seenSort.add(item.column);
    return [{ column: item.column, direction: item.direction }];
  });
  return { version: 1, columns: order, hidden: order.filter((id) => hidden.has(id)), widths, sort,
    height: normalizedHeight(source.height) };
}

/** Reconcile a canonical preference with the current schema; unknown fields are dropped. */
export function normalizeView(raw, columns, defaults) {
  const schema = schemaFor(columns);
  const base = defaultView(schema, defaults);
  if (!object(raw) || raw.version !== 1) return base;
  const allowed = new Set(base.columns);
  const known = new Set((Array.isArray(raw.columns) ? raw.columns : []).filter((id) => allowed.has(id)));
  const order = [...known, ...base.columns.filter((id) => !known.has(id))];
  const hidden = new Set(Array.isArray(raw.hidden) ? raw.hidden : []);
  const defaultHidden = new Set(base.hidden);
  const rawWidths = object(raw.widths) ? raw.widths : {};
  const widths = Object.fromEntries(base.columns.flatMap((id) => {
    const width = own(rawWidths, id) && validWidth(rawWidths[id]) ? rawWidths[id] : base.widths[id];
    return validWidth(width) ? [[id, width]] : [];
  }));
  const seenSort = new Set();
  const rawSort = own(raw, "sort") ? raw.sort : base.sort;
  const sort = (Array.isArray(rawSort) ? rawSort : []).flatMap((item) => {
    if (!object(item) || !allowed.has(item.column) || seenSort.has(item.column) || !["asc", "desc"].includes(item.direction)) return [];
    seenSort.add(item.column);
    return [{ column: item.column, direction: item.direction }];
  });
  return {
    version: 1,
    columns: order,
    hidden: order.filter((id) => hidden.has(id) || (!known.has(id) && defaultHidden.has(id))),
    widths,
    sort,
    height: own(raw, "height") ? normalizedHeight(raw.height, base.height) : base.height,
  };
}

function cloneView(view) {
  return { version: 1, columns: [...view.columns], hidden: [...view.hidden], widths: Object.fromEntries(Object.entries(view.widths)), sort: view.sort.map((item) => ({ ...item })), height: normalizedHeight(view.height) };
}

function signature(view) {
  return JSON.stringify([view.columns, view.hidden, view.columns.map((id) => own(view.widths, id) ? view.widths[id] : null), view.sort, view.height ?? null]);
}

function pauseDelivery(state) {
  const pending = state.delivery;
  if (!pending) return;
  if (pending.frame !== null) state.window.cancelAnimationFrame(pending.frame);
  if (pending.timer !== null) state.window.clearTimeout(pending.timer);
  pending.frame = null;
  pending.timer = null;
}

function acknowledgeDelivery(state, currentSignature) {
  const pending = state.delivery;
  if (pending && pending.attempts > 0 && state.renderVersion > pending.startedRender && currentSignature === pending.signature) {
    pauseDelivery(state);
    state.delivery = null;
  }
}

function scheduleDelivery(state) {
  const pending = state.delivery;
  if (!state.active || !pending || pending.frame !== null || pending.timer !== null || pending.attempts > VIEW_RETRY_DELAYS.length) return;
  const send = () => {
    if (!state.active || state.delivery !== pending) return;
    pending.frame = null;
    pending.timer = null;
    pending.attempts += 1;
    // Streamlit's CCv2 setJsonValue schedules a fragment rerun even when the
    // canonical value is unchanged. Retry only until Python echoes data.current.
    state.emit("view", cloneView(pending.view));
    if (state.delivery === pending) scheduleDelivery(state);
  };
  if (pending.attempts === 0) pending.frame = state.window.requestAnimationFrame(send);
  else {
    // Different table keys use different delays so simultaneous restorations
    // don't repeatedly compete for the same fragment rerun.
    const jitter = [...state.storageKey].reduce((hash, char) => (hash * 31 + char.charCodeAt(0)) % 211, pending.attempts * 37);
    pending.timer = state.window.setTimeout(send, VIEW_RETRY_DELAYS[pending.attempts - 1] + jitter);
  }
}

function publishView(state, view, deferred = false) {
  pauseDelivery(state);
  state.delivery = { view: cloneView(view), signature: signature(view), attempts: 0, startedRender: state.renderVersion, frame: null, timer: null };
  if (!deferred) {
    state.delivery.attempts = 1;
    state.emit("view", cloneView(view));
  }
  scheduleDelivery(state);
}

function element(state, tag, className, text) {
  const node = state.document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(state, label, className, action, ariaLabel) {
  const node = element(state, "button", className, label);
  node.type = "button";
  if (ariaLabel) { node.setAttribute("aria-label", ariaLabel); node.title = ariaLabel; }
  node.onclick = action;
  return node;
}

function setStorageOK(state, value) {
  state.storageOK = value;
}

function readSaved(state) {
  try {
    const raw = state.window.localStorage.getItem(state.storageKey);
    setStorageOK(state, true);
    if (raw === null || raw.length > 1000000) return null;
    try {
      const parsed = JSON.parse(raw);
      return object(parsed) && parsed.version === 1 ? normalizeView(parsed, state.schema, state.defaults) : null;
    } catch (_) { return null; }
  } catch (_) {
    setStorageOK(state, false);
    return null;
  }
}

function persist(state, view, remove = false) {
  try {
    if (remove) state.window.localStorage.removeItem(state.storageKey);
    else state.window.localStorage.setItem(state.storageKey, JSON.stringify(view));
    setStorageOK(state, true);
    return true;
  } catch (_) {
    setStorageOK(state, false);
    return false;
  }
}

function updateStatus(state) {
  state.dirty = signature(state.draft) !== signature(state.applied);
  state.draftStatus.textContent = state.dirty ? "Modifications non enregistrées." : "";
  state.status.textContent = state.storageOK === false
    ? "Appliquée pour cette session ; stockage du navigateur indisponible."
    : state.notice;
}

function moveItem(items, from, offset) {
  const next = from + offset;
  if (next < 0 || next >= items.length) return;
  [items[from], items[next]] = [items[next], items[from]];
}

function syncHeightControls(state) {
  const height = state.draft.height;
  const mode = height === null || height === undefined ? "default" : height === "content" ? "content" : "custom";
  state.heightMode.value = mode;
  state.heightCustom.hidden = mode !== "custom";
  state.heightInput.disabled = mode !== "custom";
  state.heightInput.value = mode === "custom" ? String(height) : "500";
}

function renderRows(state) {
  const fragment = state.document.createDocumentFragment();
  const labels = new Map(state.schema.map((column) => [column.id, column.label]));
  state.widthInputs = [];
  state.draft.columns.forEach((id, index) => {
    const label = labels.get(id) ?? id;
    const row = element(state, "tr");
    const visibleCell = element(state, "td", "tve-visible");
    const checkbox = element(state, "input");
    checkbox.type = "checkbox";
    checkbox.checked = !state.draft.hidden.includes(id);
    checkbox.setAttribute("aria-label", `Afficher ${label}`);
    checkbox.onchange = () => {
      const hidden = new Set(state.draft.hidden);
      if (checkbox.checked) hidden.delete(id); else hidden.add(id);
      state.draft.hidden = state.draft.columns.filter((column) => hidden.has(column));
      updateStatus(state);
    };
    visibleCell.appendChild(checkbox);
    row.append(visibleCell, element(state, "th", "tve-name", label));
    row.lastChild.scope = "row";

    const orderCell = element(state, "td");
    const order = element(state, "div", "tve-stepper");
    [-1, 1].forEach((offset) => {
      const control = button(state, offset < 0 ? "↑" : "↓", "tve-icon-button", () => {
        moveItem(state.draft.columns, index, offset);
        state.draft.hidden = state.draft.columns.filter((column) => state.draft.hidden.includes(column));
        renderRows(state);
        updateStatus(state);
      }, `${offset < 0 ? "Monter" : "Descendre"} ${label}`);
      control.disabled = offset < 0 ? index === 0 : index === state.draft.columns.length - 1;
      order.appendChild(control);
    });
    orderCell.appendChild(order);
    row.appendChild(orderCell);

    const widthCell = element(state, "td");
    const width = element(state, "input", "tve-width");
    width.type = "number";
    width.min = "40";
    width.max = "2000";
    width.step = "1";
    width.inputMode = "numeric";
    width.placeholder = "Auto";
    width.value = own(state.draft.widths, id) ? String(state.draft.widths[id]) : "";
    width.setAttribute("aria-label", `Largeur de ${label}, en pixels`);
    width.title = "De 40 à 2000 pixels. Vide : largeur par défaut.";
    width.oninput = () => {
      if (width.value === "") delete state.draft.widths[id];
      else Object.defineProperty(state.draft.widths, id, { value: Number.isFinite(width.valueAsNumber) ? width.valueAsNumber : width.value, enumerable: true, configurable: true, writable: true });
      updateStatus(state);
    };
    state.widthInputs.push(width);
    widthCell.appendChild(width);
    row.appendChild(widthCell);

    const sortCell = element(state, "td");
    const sortSelect = element(state, "select");
    sortSelect.setAttribute("aria-label", `Tri de ${label}`);
    [["", "Aucun"], ["asc", "Croissant"], ["desc", "Décroissant"]].forEach(([value, text]) => {
      const option = element(state, "option", "", text);
      option.value = value;
      sortSelect.appendChild(option);
    });
    const sortIndex = state.draft.sort.findIndex((item) => item.column === id);
    sortSelect.value = sortIndex < 0 ? "" : state.draft.sort[sortIndex].direction;
    sortSelect.onchange = () => {
      const priorIndex = state.draft.sort.findIndex((item) => item.column === id);
      if (!sortSelect.value) state.draft.sort = state.draft.sort.filter((item) => item.column !== id);
      else if (priorIndex < 0) state.draft.sort.push({ column: id, direction: sortSelect.value });
      else state.draft.sort[priorIndex] = { column: id, direction: sortSelect.value };
      renderRows(state);
      updateStatus(state);
    };
    sortCell.appendChild(sortSelect);
    row.appendChild(sortCell);

    const priorityCell = element(state, "td");
    if (sortIndex >= 0) {
      const priority = element(state, "div", "tve-stepper");
      priority.appendChild(element(state, "span", "tve-priority", String(sortIndex + 1)));
      if (state.draft.sort.length > 1) {
        [-1, 1].forEach((offset) => {
          const control = button(state, offset < 0 ? "↑" : "↓", "tve-icon-button", () => {
            moveItem(state.draft.sort, sortIndex, offset);
            renderRows(state);
            updateStatus(state);
          }, `${offset < 0 ? "Augmenter" : "Diminuer"} la priorité du tri de ${label}`);
          control.disabled = offset < 0 ? sortIndex === 0 : sortIndex === state.draft.sort.length - 1;
          priority.appendChild(control);
        });
      }
      priorityCell.appendChild(priority);
    }
    row.appendChild(priorityCell);
    fragment.appendChild(row);
  });
  state.tbody.replaceChildren(fragment);
  syncHeightControls(state);
}

function sortableValue(cell) {
  if (!cell) return { empty: true, number: null, text: "" };
  const explicit = cell.getAttribute("data-sort") ?? cell.querySelector("[data-sort]")?.getAttribute("data-sort");
  const primary = cell.querySelector(".pl-scenario-probability, .pl-competition-value, .pl-competition-name");
  const text = (explicit ?? primary?.getAttribute("title") ?? primary?.textContent ?? cell.textContent ?? "").trim();
  if (!text || /^(?:—|–|-|n\/a|nan|null)$/i.test(text)) return { empty: true, number: null, text };
  let candidate = text.replace(/[\s\u00a0\u202f]/g, "").replace(/−/g, "-").replace(/%$/, "");
  if (/^\(.*\)$/.test(candidate)) candidate = `-${candidate.slice(1, -1)}`;
  if (candidate.includes(",") && candidate.includes(".")) {
    candidate = candidate.lastIndexOf(",") > candidate.lastIndexOf(".")
      ? candidate.replace(/\./g, "").replace(",", ".") : candidate.replace(/,/g, "");
  } else candidate = candidate.replace(",", ".");
  const number = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(candidate) ? Number(candidate) : null;
  return { empty: false, number: Number.isFinite(number) ? number : null, text };
}

function captureRichTables(state, html) {
  state.richHTML = html;
  // This is application-generated, already escaped table markup, never preference data.
  state.rich.innerHTML = html;
  state.rich.hidden = !html;
  state.richTables = [...state.rich.querySelectorAll("table")].map((table) => {
    const rows = [...table.querySelectorAll("tr")].filter((row) => row.closest("table") === table).map((row) => ({
      node: row,
      cells: [...row.children].filter((cell) => ["TD", "TH"].includes(cell.tagName)).map((cell) => ({
        node: cell,
        display: cell.style.display,
        width: cell.style.width,
        minWidth: cell.style.minWidth,
        maxWidth: cell.style.maxWidth,
      })),
    }));
    const groups = [...table.tBodies].map((body) => ({
      body,
      rows: rows.filter((row) => row.node.parentElement === body).map((row, index) => ({ ...row, index })),
    }));
    return { table, rows, groups };
  });
}

function applyRichView(state) {
  const view = state.applied;
  if (state.rich) {
    state.rich.style.maxHeight = validHeight(view.height) ? `${view.height}px` : "";
    state.rich.style.overflowY = validHeight(view.height) ? "auto" : "";
  }
  const indices = new Map(state.schema.map((column, index) => [column.id, index]));
  const hidden = new Set(view.hidden);
  const collator = new Intl.Collator("fr", { numeric: true, sensitivity: "base" });
  state.richTables.forEach(({ rows, groups }) => {
    rows.forEach((row) => {
      const included = new Set();
      view.columns.forEach((id) => {
        const index = indices.get(id);
        const cell = row.cells[index];
        if (!cell) return;
        included.add(index);
        cell.node.style.display = hidden.has(id) ? "none" : cell.display;
        const width = view.widths[id];
        cell.node.style.width = validWidth(width) ? `${width}px` : cell.width;
        cell.node.style.minWidth = validWidth(width) ? `${width}px` : cell.minWidth;
        cell.node.style.maxWidth = validWidth(width) ? `${width}px` : cell.maxWidth;
        row.node.appendChild(cell.node);
      });
      row.cells.forEach((cell, index) => { if (!included.has(index)) row.node.appendChild(cell.node); });
    });
    groups.forEach(({ body, rows: bodyRows }) => {
      const values = new Map(bodyRows.map((row) => [row, view.sort.map((sort) => sortableValue(row.cells[indices.get(sort.column)]?.node))]));
      const ordered = [...bodyRows].sort((left, right) => {
        for (let index = 0; index < view.sort.length; index += 1) {
          const a = values.get(left)[index];
          const b = values.get(right)[index];
          if (a.empty !== b.empty) return a.empty ? 1 : -1;
          const result = a.number !== null && b.number !== null ? a.number - b.number : collator.compare(a.text, b.text);
          if (result) return view.sort[index].direction === "desc" ? -result : result;
        }
        return left.index - right.index;
      });
      ordered.forEach((row) => body.appendChild(row.node));
    });
  });
}

function applyView(state, view, notice, keepDraft = false) {
  state.applied = cloneView(view);
  if (!keepDraft) {
    state.draft = cloneView(view);
    renderRows(state);
  }
  state.notice = notice;
  applyRichView(state);
  updateStatus(state);
}

function reset(state) {
  persist(state, state.defaults, true);
  applyView(state, state.defaults, "Vue par défaut rétablie.");
  publishView(state, state.applied);
}

function createState(parentElement, data, emit) {
  const document = parentElement.ownerDocument;
  const state = { document, window: document.defaultView, emit, storageKey: String(data.storage_key ?? ""), notice: "", richTables: [], richHTML: null, storageListener: null, dirty: false, delivery: null, active: true, renderVersion: 0 };
  state.root = element(state, "div", "tve-root");
  state.details = element(state, "details", "tve-editor");
  state.details.appendChild(element(state, "summary", "", "Affichage du tableau"));
  const content = element(state, "div", "tve-content");
  content.appendChild(element(state, "p", "tve-help", "Les déplacements, largeurs et tris effectués directement dans le tableau sont temporaires. Réglez la hauteur et les colonnes ci-dessous, puis enregistrez votre vue pour vos prochaines visites sur ce navigateur."));
  const heightControls = element(state, "div", "tve-height-controls");
  const heightLabel = element(state, "label", "tve-height-label", "Hauteur du tableau");
  state.heightMode = element(state, "select");
  state.heightMode.setAttribute("aria-label", "Hauteur du tableau");
  [["default", "Par défaut"], ["custom", "Personnalisée"], ["content", "Adapter aux lignes chargées"]].forEach(([value, text]) => {
    const option = element(state, "option", "", text);
    option.value = value;
    state.heightMode.appendChild(option);
  });
  state.heightMode.onchange = () => {
    state.draft.height = state.heightMode.value === "content" ? "content"
      : state.heightMode.value === "custom" ? validHeight(state.draft.height) ? state.draft.height : 500 : null;
    syncHeightControls(state);
    updateStatus(state);
  };
  heightLabel.appendChild(state.heightMode);
  state.heightCustom = element(state, "label", "tve-height-custom", "Hauteur en pixels");
  state.heightInput = element(state, "input", "tve-height-input");
  Object.assign(state.heightInput, { type: "number", min: "200", max: "5000", step: "1", inputMode: "numeric", required: true });
  state.heightInput.setAttribute("aria-label", "Hauteur du tableau, en pixels");
  state.heightInput.oninput = () => {
    state.draft.height = Number.isFinite(state.heightInput.valueAsNumber) ? state.heightInput.valueAsNumber : state.heightInput.value;
    updateStatus(state);
  };
  state.heightCustom.appendChild(state.heightInput);
  heightControls.append(heightLabel, state.heightCustom);
  content.appendChild(heightControls);
  content.appendChild(element(state, "p", "tve-help", "Hauteur personnalisée : de 200 à 5000 pixels. Adapter aux lignes chargées n'ajoute pas les autres pages et ne change ni les filtres ni les données."));
  const scroll = element(state, "div", "tve-controls-scroll");
  const table = element(state, "table", "tve-controls");
  table.setAttribute("aria-label", "Préférences des colonnes du tableau");
  const thead = element(state, "thead");
  const heading = element(state, "tr");
  ["Visible", "Colonne", "Ordre", "Largeur (px)", "Tri", "Priorité du tri"].forEach((text) => {
    const cell = element(state, "th", "", text);
    cell.scope = "col";
    heading.appendChild(cell);
  });
  thead.appendChild(heading);
  state.tbody = element(state, "tbody");
  table.append(thead, state.tbody);
  scroll.appendChild(table);
  content.appendChild(scroll);
  content.appendChild(element(state, "p", "tve-help tve-width-help", "Largeur : de 40 à 2000 pixels ; vide pour la largeur par défaut. Le tri 1 est prioritaire. Le tri porte sur les lignes chargées dans ce tableau."));
  const actions = element(state, "div", "tve-actions");
  actions.appendChild(button(state, "Tout afficher", "tve-button", () => {
    state.draft.hidden = [];
    renderRows(state);
    updateStatus(state);
  }));
  actions.appendChild(button(state, "Enregistrer ma vue", "tve-button tve-primary", () => {
    if (state.heightMode.value === "custom" && !state.heightInput.reportValidity()) return;
    for (const input of state.widthInputs) {
      if (!input.reportValidity()) return;
    }
    const view = normalizeView(state.draft, state.schema, state.defaults);
    persist(state, view);
    applyView(state, view, "Vue enregistrée sur ce navigateur.");
    publishView(state, view);
  }));
  actions.appendChild(button(state, "Réinitialiser la vue", "tve-button", () => reset(state)));
  content.appendChild(actions);
  state.draftStatus = element(state, "p", "tve-draft-status");
  state.status = element(state, "p", "tve-status");
  state.status.setAttribute("role", "status");
  state.status.setAttribute("aria-live", "polite");
  content.append(state.draftStatus, state.status);
  state.details.appendChild(content);
  state.rich = element(state, "div", "tve-rich");
  state.rich.hidden = true;
  state.root.append(state.details, state.rich);
  parentElement.appendChild(state.root);
  return state;
}

function attachStorageListener(state) {
  if (state.storageListener) return;
  state.storageListener = (event) => {
    if (event.key !== state.storageKey && event.key !== null) return;
    let view = state.defaults;
    if (event.key !== null && event.newValue !== null) {
      if (event.newValue.length > 1000000) return;
      try {
        const parsed = JSON.parse(event.newValue);
        if (!object(parsed) || parsed.version !== 1) return;
        view = normalizeView(parsed, state.schema, state.defaults);
      } catch (_) { return; }
    }
    const changed = signature(view) !== signature(state.applied);
    applyView(state, view, event.newValue === null ? "Vue par défaut rétablie." : "Vue enregistrée sur ce navigateur.", state.dirty);
    if (changed) publishView(state, view);
  };
  state.window.addEventListener("storage", state.storageListener);
}

export default function render({ data = {}, parentElement, setStateValue }) {
  let state = instances.get(parentElement);
  const storageKey = String(data.storage_key ?? "");
  if (state && state.storageKey !== storageKey) {
    state.active = false;
    pauseDelivery(state);
    state.delivery = null;
    if (state.storageListener) state.window.removeEventListener("storage", state.storageListener);
    state.root.remove();
    state = null;
  }
  const firstMount = !state;
  if (!state) {
    state = createState(parentElement, data, setStateValue);
    instances.set(parentElement, state);
  }
  state.active = true;
  state.renderVersion += 1;
  state.emit = setStateValue;
  const schema = schemaFor(data.columns);
  const defaults = defaultView(schema, data.defaults);
  const schemaSignature = JSON.stringify([schema, defaults]);
  const schemaChanged = schemaSignature !== state.schemaSignature;
  state.schema = schema;
  state.defaults = defaults;
  state.schemaSignature = schemaSignature;
  const current = normalizeView(data.current ?? defaults, schema, defaults);
  const currentSignature = signature(current);
  if (firstMount) {
    state.resetToken = data.reset_token;
    const saved = readSaved(state);
    state.applied = cloneView(saved ?? current);
    state.draft = cloneView(state.applied);
    state.notice = saved ? "Vue enregistrée sur ce navigateur." : "";
    renderRows(state);
    if (saved && signature(saved) !== currentSignature) {
      // Hydration is delivered after mount, once the component's event bridge
      // is ready. Synchronous state emitted during initial render may be lost.
      publishView(state, saved, true);
    }
  } else if (data.reset_token !== state.resetToken) {
    state.resetToken = data.reset_token;
    reset(state);
  } else {
    if (schemaChanged) {
      state.applied = normalizeView(state.applied, schema, defaults);
      state.draft = normalizeView(state.draft, schema, defaults);
      if (state.delivery) {
        state.delivery.view = normalizeView(state.delivery.view, schema, defaults);
        state.delivery.signature = signature(state.delivery.view);
      }
    }
    acknowledgeDelivery(state, currentSignature);
    if (!state.delivery && currentSignature !== state.serverSignature && currentSignature !== signature(state.applied)) {
      state.applied = cloneView(current);
      state.draft = cloneView(current);
      state.notice = "";
      renderRows(state);
    } else if (schemaChanged) renderRows(state);
  }
  state.serverSignature = currentSignature;
  scheduleDelivery(state);
  const richHTML = typeof data.rich_table_html === "string" ? data.rich_table_html : "";
  if (richHTML !== state.richHTML || schemaChanged) captureRichTables(state, richHTML);
  applyRichView(state);
  updateStatus(state);
  attachStorageListener(state);
  const listener = state.storageListener;
  const renderVersion = state.renderVersion;
  return () => {
    if (state.renderVersion !== renderVersion) return;
    state.active = false;
    pauseDelivery(state);
    state.window.removeEventListener("storage", listener);
    if (state.storageListener === listener) state.storageListener = null;
  };
}
