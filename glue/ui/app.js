/**
 * app.js — логика UI, взаимодействие с Python API через pywebview
 */

let allPackages  = [];   // [{name, status, tx,ty,...}]
let currentPkg   = null; // имя выбранного пакета
let isDirty      = false; // есть ли несохранённые изменения

// ── Инициализация ──────────────────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", () => {
  document.getElementById("viewport-overlay").style.display = "flex";
  setStatus("Loading 3D viewer...");

  // Инициализируем метку step dir как только API готов
  window.addEventListener("pywebviewready", async () => {
    try {
      const r = await pywebview.api.get_step_dir();
      updateStepDirLabel(r.step_dir || "");
    } catch(e) {}
  });

  // Закрываем контекстное меню по клику в любом месте
  document.addEventListener("click", () => {
    document.getElementById("ctx-menu").style.display = "none";
  });

  // Enter в диалоге имени — подтверждение
  document.getElementById("name-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") nameDialogOK();
    if (e.key === "Escape") nameDialogCancel();
  });
});

function requireApi() {
  if (typeof pywebview === "undefined" || !pywebview.api) {
    setStatus("pywebview API unavailable", true);
    return false;
  }
  return true;
}

// Автооткрытие если путь передан через sys.argv
window.autoOpenLibrary = (path) => {
  openLibrary(path);
};

// ── Открытие библиотеки ────────────────────────────────────────────────────
async function openLibrary(path = "") {
  console.log("[openLibrary] called, path=", path);
  setStatus("Loading library...");
  let result;
  try {
    result = await pywebview.api.open_library(path);
  } catch(e) {
    setStatus("Error calling API: " + e, true);
    return;
  }

  if (result.error) {
    if (result.error !== "cancelled")
      setStatus("Error: " + result.error, true);
    return;
  }

  allPackages = result.packages;
  document.getElementById("lib-name").textContent =
    result.library_path.split(/[\\/]/).pop();

  renderPackageList(allPackages);
  currentPkg = null;
  document.getElementById("btn-apply").disabled = true;
  clearOrientFields();
  setStatus(`Loaded: ${allPackages.length} packages`);
}

// ── Список пакетов ─────────────────────────────────────────────────────────
function renderPackageList(pkgs) {
  const ul = document.getElementById("pkg-list");
  ul.innerHTML = "";
  pkgs.forEach(pkg => {
    const li = document.createElement("li");
    li.dataset.name = pkg.name;

    const dot = document.createElement("span");
    dot.className = "dot " + {
      no_model:       "red",
      no_orientation: "yellow",
      ok:             "green",
    }[pkg.status];
    li.appendChild(dot);

    li.appendChild(document.createTextNode(pkg.name));

    if (pkg.status !== "no_model") {
      li.classList.add(pkg.status === "ok" ? "ok" : "no-orientation");
    } else {
      li.classList.add("no-model");
    }
    li.addEventListener("click", () => selectPackage(pkg.name));

    // Контекстное меню — правая кнопка мыши
    li.addEventListener("contextmenu", (e) => showContextMenu(e, pkg.name));

    if (pkg.name === currentPkg) li.classList.add("selected");
    ul.appendChild(li);
  });
}

function filterPackages() {
  const q = document.getElementById("search-input").value.toLowerCase();
  const filtered = allPackages.filter(p => p.name.toLowerCase().includes(q));
  renderPackageList(filtered);
}

// ── Default viewport state ─────────────────────────────────────────────────
function resetViewport() {
  currentPkg = null;
  isDirty    = false;
  // Очищаем 3D-сцену
  if (typeof Viewer !== "undefined") {
    Viewer.loadFootprint({wires:[], smds:[], pads:[], circles:[], holes:[]});
    Viewer.loadComponentGLB(null);
  }
  document.getElementById("viewport-overlay").style.display = "flex";
  document.getElementById("btn-apply").disabled = true;
  document.getElementById("btn-auto").disabled  = true;
  clearOrientFields();
  document.getElementById("drawing-url").value = "";
  document.getElementById("model-url").value   = "";
  document.querySelectorAll("#pkg-list li").forEach(li => li.classList.remove("selected"));
}


async function selectPackage(name) {
  currentPkg = name;
  isDirty    = false;

  document.querySelectorAll("#pkg-list li").forEach(li => {
    li.classList.toggle("selected", li.dataset.name === name);
  });

  document.getElementById("viewport-overlay").style.display = "none";
  document.getElementById("btn-apply").disabled = false;
  document.getElementById("btn-auto").disabled  = true;

  // Сразу очищаем модель предыдущего компонента
  if (typeof Viewer !== "undefined") Viewer.loadComponentGLB(null);

  setStatus(`Loading ${name}...`);
  showLoading("Loading footprint data...");

  try {
    const fp = await pywebview.api.get_footprint(name);
    if (fp.error) { setStatus("Error: " + fp.error, true); return; }

    Viewer.loadFootprint(fp.geometry);
    Viewer.resetCamera();

    const o = fp.orientation;
    setOrientFields(o.tx, o.ty, o.tz, o.rx, o.ry, o.rz);
    Viewer.setComponentTransform(o.tx, o.ty, o.tz, o.rx, o.ry, o.rz);
    document.getElementById("drawing-url").value = o.drawing_url || "";
    document.getElementById("model-url").value   = o.model_url   || "";

    // GLB только если есть STEP-файл
    const pkg = allPackages.find(p => p.name === name);
    if (pkg && pkg.status !== "no_model") {
      setLoadingText("Tessellating STEP model...");
      try {
        const glbResult = await pywebview.api.get_component_glb(name);
        if (glbResult.error) {
          setStatus(`⚠ ${name}: model not loaded — ${glbResult.error}`);
        } else {
          try {
            const glbInfo = await Viewer.loadComponentGLB(glbResult.glb);
            document.getElementById("btn-auto").disabled = false;
            setStatus(`${name} loaded  [${glbInfo}]`);
          } catch(parseErr) {
            setStatus(`⚠ ${name}: GLB parse error — ${parseErr}`, true);
          }
        }
      } catch(e) {
        setStatus(`⚠ ${name}: GLB call failed — ${e}`, true);
      }
    } else {
      setStatus(`${name} — no 3D model`);
    }
  } finally {
    hideLoading();
  }
}

// ── Перезагрузка модели ────────────────────────────────────────────────────
async function reloadModel() {
  if (!currentPkg) return;
  showLoading("Re-reading STEP file...");
  setStatus("Reloading model...");
  try {
    const result = await pywebview.api.reload_model(currentPkg);
    if (result.error) {
      setStatus("⚠ Reload failed: " + result.error, true);
      return;
    }
    try {
      await Viewer.loadComponentGLB(result.glb);
      document.getElementById("btn-auto").disabled = false;
      setStatus(`✓ ${currentPkg} model reloaded`);
    } catch(parseErr) {
      setStatus(`⚠ GLB parse error — ${parseErr}`, true);
    }
  } catch(e) {
    setStatus("Error reloading: " + e, true);
  } finally {
    hideLoading();
  }
}

// ── Apply & Save ───────────────────────────────────────────────────────────
async function applyAndSave() {
  if (!currentPkg) return;

  const t = Viewer.getComponentTransform();
  const drawing_url = document.getElementById("drawing-url").value.trim();
  const model_url   = document.getElementById("model-url").value.trim();

  showLoading("Saving and tessellating...");
  setStatus("Saving...");

  try {
    const result = await pywebview.api.save_orientation(
      currentPkg, t.tx, t.ty, t.tz, t.rx, t.ry, t.rz,
      drawing_url, model_url
    );

    if (result.error) { setStatus("Error: " + result.error, true); return; }
    if (result.warning) {
      setStatus("⚠ " + result.warning);
    } else {
      setStatus(`✓ ${currentPkg} saved`);
    }

    const pkg = allPackages.find(p => p.name === currentPkg);
    if (pkg) {
      pkg.status = "ok";
      pkg.tx = t.tx; pkg.ty = t.ty; pkg.tz = t.tz;
      pkg.rx = t.rx; pkg.ry = t.ry; pkg.rz = t.rz;
    }
    _refreshList();
    isDirty = false;
  } finally {
    hideLoading();
  }
}

// ── Контекстное меню ───────────────────────────────────────────────────────
let _ctxPkg = null;

function showContextMenu(e, pkgName) {
  e.preventDefault();
  e.stopPropagation();
  _ctxPkg = pkgName;
  const menu = document.getElementById("ctx-menu");
  menu.style.display = "block";
  // Не выходить за границы экрана
  const mw = 160, mh = 170;
  const x  = Math.min(e.clientX, window.innerWidth  - mw);
  const y  = Math.min(e.clientY, window.innerHeight - mh);
  menu.style.left = x + "px";
  menu.style.top  = y + "px";
}

async function ctxRename() {
  const oldName = _ctxPkg;
  if (!oldName) return;
  const newName = await showNameDialog("RENAME FOOTPRINT", oldName);
  if (!newName || newName === oldName) return;
  if (allPackages.some(p => p.name === newName)) {
    setStatus(`Error: '${newName}' already exists`, true); return;
  }
  showLoading("Renaming...");
  try {
    const r = await pywebview.api.rename_package(oldName, newName);
    if (r.error) { setStatus("Error: " + r.error, true); return; }
    const pkg = allPackages.find(p => p.name === oldName);
    if (pkg) pkg.name = newName;
    if (currentPkg === oldName) currentPkg = newName;
    _refreshList();
    setStatus(`Renamed: ${oldName} → ${newName}`);
  } finally { hideLoading(); }
}

async function ctxDelete() {
  const name = _ctxPkg;
  if (!name) return;
  if (!confirm(`Delete footprint "${name}" and its 3D files?`)) return;
  showLoading("Deleting...");
  try {
    const r = await pywebview.api.delete_package(name);
    if (r.error) { setStatus("Error: " + r.error, true); return; }
    allPackages = allPackages.filter(p => p.name !== name);
    if (currentPkg === name) {
      resetViewport();
    }
    _refreshList();
    setStatus(`Deleted: ${name}`);
  } finally { hideLoading(); }
}

async function ctxDuplicate() {
  const srcName = _ctxPkg;
  if (!srcName) return;
  const newName = await showNameDialog("DUPLICATE — TYPE NEW NAME", srcName);
  if (!newName) return;
  if (allPackages.some(p => p.name === newName)) {
    setStatus(`Error: '${newName}' already exists`, true); return;
  }
  showLoading("Duplicating...");
  try {
    const r = await pywebview.api.duplicate_package(srcName, newName);
    if (r.error) { setStatus("Error: " + r.error, true); return; }
    // Обновляем список с сервера
    const pkg = allPackages.find(p => p.name === srcName);
    if (pkg) {
      allPackages.push({ ...pkg, name: newName });
      allPackages.sort((a, b) => a.name.localeCompare(b.name));
    }
    _refreshList();
    setStatus(`Duplicated: ${srcName} → ${newName}`);
  } finally { hideLoading(); }
}

async function ctxLoadStep() {
  const name = _ctxPkg;
  if (!name) return;
  showLoading("Opening file dialog...");
  try {
    const r = await pywebview.api.load_step_file(name);
    if (r.error) {
      if (r.error !== "cancelled") setStatus("Error: " + r.error, true);
      return;
    }
    if (r.warning) setStatus(`⚠ ${r.warning}`);
    else setStatus(`✓ Model loaded for ${name}`);
    // Обновляем статус в списке
    const pkg = allPackages.find(p => p.name === name);
    if (pkg && pkg.status === "no_model") pkg.status = "no_orientation";
    _refreshList();
    // Если это текущий пакет — перезагружаем модель
    if (currentPkg === name) await reloadModel();
  } finally { hideLoading(); }
}

async function ctxDeleteStep() {
  const name = _ctxPkg;
  if (!name) return;
  showLoading("Deleting 3D files...");
  try {
    const r = await pywebview.api.delete_step(name);
    if (r.error) { setStatus("Error: " + r.error, true); return; }
    const pkg = allPackages.find(p => p.name === name);
    if (pkg) pkg.status = "no_model";
    _refreshList();
    // Если текущий — убираем модель из вьюера
    if (currentPkg === name) {
      Viewer.loadComponentGLB(null);
      document.getElementById("btn-auto").disabled = true;
    }
    setStatus(`✓ 3D model removed from ${name}`);
  } finally { hideLoading(); }
}

// ── Диалог ввода имени ─────────────────────────────────────────────────────
let _nameResolve = null;

function showNameDialog(title = "TYPE NAME", defaultName = "") {
  return new Promise(resolve => {
    _nameResolve = resolve;
    document.getElementById("name-dialog-title").textContent = title;
    const inp = document.getElementById("name-input");
    inp.value = "";
    inp.placeholder = defaultName || "";
    document.getElementById("name-error").textContent = "";
    document.getElementById("name-dialog").style.display = "flex";
    setTimeout(() => inp.focus(), 50);
  });
}

function nameDialogOK() {
  const val = document.getElementById("name-input").value.trim();
  if (!val) {
    document.getElementById("name-error").textContent = "Name cannot be empty";
    return;
  }
  document.getElementById("name-dialog").style.display = "none";
  if (_nameResolve) { _nameResolve(val); _nameResolve = null; }
}

function nameDialogCancel() {
  document.getElementById("name-dialog").style.display = "none";
  if (_nameResolve) { _nameResolve(null); _nameResolve = null; }
}

function onNameInput(e) {
  const input = e.target;
  const pos = input.selectionStart;
  // Оставляем только допустимые символы и переводим в верхний регистр
  const raw = input.value.toUpperCase();
  const clean = raw.replace(/[ /\\"'\\\\]/g, '');
  if (clean !== input.value) {
    input.value = clean;
    input.setSelectionRange(
      Math.min(pos, clean.length),
      Math.min(pos, clean.length)
    );
  }
  document.getElementById("name-error").textContent = "";
}

// ── Поля ориентации ────────────────────────────────────────────────────────
function setOrientFields(tx,ty,tz,rx,ry,rz) {
  document.getElementById("f-tx").value = round4(tx);
  document.getElementById("f-ty").value = round4(ty);
  document.getElementById("f-tz").value = round4(tz);
  document.getElementById("f-rx").value = round4(rx);
  document.getElementById("f-ry").value = round4(ry);
  document.getElementById("f-rz").value = round4(rz);
}

function clearOrientFields() {
  ["f-tx","f-ty","f-tz","f-rx","f-ry","f-rz"].forEach(id => {
    document.getElementById(id).value = "";
  });
}

function applyOrientFields() {
  const tx = +document.getElementById("f-tx").value || 0;
  const ty = +document.getElementById("f-ty").value || 0;
  const tz = +document.getElementById("f-tz").value || 0;
  const rx = +document.getElementById("f-rx").value || 0;
  const ry = +document.getElementById("f-ry").value || 0;
  const rz = +document.getElementById("f-rz").value || 0;
  Viewer.setComponentTransform(tx, ty, tz, rx, ry, rz);
  markDirty();
}

function onMetaChange() { markDirty(); }

// ── Utils ──────────────────────────────────────────────────────────────────
function setStatus(msg, isError = false) {
  const el = document.getElementById("status-msg");
  el.textContent = msg;
  el.style.color = isError ? "#f66" : "#888";
}

function showLoading(text = "Loading...") {
  document.getElementById("loading-text").textContent = text;
  document.getElementById("loading-overlay").classList.add("visible");
}
function setLoadingText(text) {
  document.getElementById("loading-text").textContent = text;
}
function hideLoading() {
  document.getElementById("loading-overlay").classList.remove("visible");
}

function markDirty() {
  isDirty = true;
  if (currentPkg) document.getElementById("btn-apply").disabled = false;
}

function round4(v) { return parseFloat((+v).toFixed(4)); }

function setTransformMode(mode) { Viewer.setTransformMode(mode); }
function resetCamera()          { Viewer.resetCamera(); }
function resetComponent() {
  Viewer.resetComponent();
  const t = Viewer.getComponentTransform();
  setOrientFields(t.tx, t.ty, t.tz, t.rx, t.ry, t.rz);
  markDirty();
}

function autoPlace() {
  if (!currentPkg) return;
  const ok = Viewer.autoPlace();
  if (!ok) { setStatus("⚠ No component loaded to auto-place"); return; }
  const t = Viewer.getComponentTransform();
  setOrientFields(t.tx, t.ty, t.tz, t.rx, t.ry, t.rz);
  markDirty();
  setStatus("Auto-placed: component centered on footprint");
}

// ── Load model для текущего футпринта (кнопка в тулбаре) ─────────────────────
async function loadModelForCurrent() {
  if (!currentPkg) { setStatus("Select a footprint first", true); return; }
  _ctxPkg = currentPkg;
  await ctxLoadStep();
}

// ── Retessellate ALL ──────────────────────────────────────────────────────
async function retessellateAll() {
  if (!confirm("Удалить все GLB и перетесселировать все STEP-файлы заново?")) return;
  showLoading("Tessellating all STEP files...");
  setStatus("Retessellating all...");
  try {
    const result = await pywebview.api.retessellate_all();
    if (result.error) { setStatus("Error: " + result.error, true); return; }
    let msg = `✓ Retessellated: ${result.done}/${result.total}`;
    if (result.errors && result.errors.length)
      msg += `  ⚠ ${result.errors.length} errors: ${result.errors[0]}`;
    setStatus(msg);
    // Перезагружаем текущую модель если была выбрана
    if (currentPkg) await reloadModel();
  } catch(e) {
    setStatus("Error: " + e, true);
  } finally {
    hideLoading();
  }
}

// ── Downstream ────────────────────────────────────────────────────────────
let _dsFolder = "";
let _dsFolderName = "";

async function downstream() {
  if (!requireApi()) return;
  if (!allPackages.length && !currentPkg) {
    setStatus("⚠ Open a library first", true);
    return;
  }
  const status = await pywebview.api.get_downstream_status();
  if (!status.has_lib) {
    setStatus("⚠ Open a library first", true);
    return;
  }
  _dsFolder     = status.folder;
  _dsFolderName = status.folder_name;
  _showDownstreamDialog(status.lib_name, status.folder_name);
}

function _showDownstreamDialog(libName, folderName) {
  const msg    = document.getElementById("ds-msg");
  const btnYes = document.getElementById("ds-btn-yes");
  if (folderName) {
    msg.innerHTML = `Are you sure you want to downstream changes from<br>
      <b>${libName}</b> to <b>${folderName}</b>?`;
    btnYes.disabled = false;
  } else {
    msg.innerHTML = `Library folder is not configured.<br>Please specify one.`;
    btnYes.disabled = true;
  }
  document.getElementById("downstream-dialog").style.display = "flex";
}

function downstreamClose() {
  document.getElementById("downstream-dialog").style.display = "none";
}

async function downstreamYes() {
  downstreamClose();
  await _runDownstream();
}

async function downstreamSpecify() {
  downstreamClose();
  showLoading("Selecting library folder...");
  let result;
  try {
    result = await pywebview.api.pick_lib_folder();
  } finally {
    hideLoading();
  }
  if (result.error) return; // cancelled
  _dsFolder     = result.folder;
  _dsFolderName = result.folder_name;
  await _runDownstream();
}

async function _runDownstream() {
  showLoading("Running downstream...");
  setStatus(`Downstreaming to ${_dsFolderName}...`);
  try {
    const result = await pywebview.api.run_downstream();
    if (result.error) {
      setStatus("⚠ Downstream failed: " + result.error, true);
    } else {
      setStatus(`✓ Downstream complete → ${_dsFolderName}`);
    }
  } catch(e) {
    setStatus("Error: " + e, true);
  } finally {
    hideLoading();
  }
}

// ── Step dir ───────────────────────────────────────────────────────────────
function updateStepDirLabel(path) {
  const el = document.getElementById("step-dir-label");
  if (path) {
    el.textContent = path;
    el.style.color = "#888";
  } else {
    el.textContent = "STEP DIR NOT DEFINED";
    el.style.color = "#f44";
  }
}

async function setStepDir() {
  if (!requireApi()) return;
  let result;
  try {
    result = await pywebview.api.set_step_dir();
  } catch(e) {
    setStatus("Error: " + e, true); return;
  }
  if (result.error) {
    if (result.error !== "cancelled") setStatus("Error: " + result.error, true);
    return;
  }
  updateStepDirLabel(result.step_dir);
  if (result.packages) {
    allPackages = result.packages;
    _refreshList();
  }
  setStatus(`Step dir set: ${result.step_count} STEP files found`);
}

// ── Внутренние хелперы ────────────────────────────────────────────────────
function _refreshList() {
  const q = document.getElementById("search-input").value.toLowerCase();
  const filtered = q
    ? allPackages.filter(p => p.name.toLowerCase().includes(q))
    : allPackages;
  renderPackageList(filtered);
}