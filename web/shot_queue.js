import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// One browser-wide runner prevents two Shot Queue nodes submitting concurrently.
let runner = null;
const blank = () => ({ start_frame: "", end_frame: "", prompt: "", duration: 5 });
const pause = () => new Promise(resolve => setTimeout(resolve, 1000));

function previewURL(path) {
    path = path.trim().replace(/\\/g, "/");
    const annotation = path.match(/\s*\[(input|output|temp)\]$/);
    if (annotation) path = path.slice(0, annotation.index).trim();
    const slash = path.lastIndexOf("/");
    const params = new URLSearchParams({ filename: path.slice(slash + 1), subfolder: slash < 0 ? "" : path.slice(0, slash), type: annotation?.[1] ?? "input", preview: "webp;80" });
    return api.apiURL(`/view?${params}`);
}

async function waitForCompletion(id, state) {
    while (true) {
        if (state.stop) return false;
        const response = await api.fetchApi(`/history/${encodeURIComponent(id)}`);
        if (!response.ok) throw new Error(`History request failed (${response.status})`);
        const entry = (await response.json())[id];
        if (entry) {
            if (entry.status?.status_str === "error") throw new Error("Workflow failed; inspect ComfyUI execution errors.");
            if (entry.status?.completed === true && entry.status?.status_str === "success") return true;
            // Never infer success merely from one node producing outputs.
            if (entry.status?.completed === true) throw new Error("Workflow ended without success.");
        }
        await pause();
    }
}

app.registerExtension({
    name: "ShotQueue.Lightweight",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ShotQueue") return;
        const original = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            original?.apply(this, arguments);
            const node = this;
            const jsonWidget = node.widgets.find(w => w.name === "shots_json");
            const indexWidget = node.widgets.find(w => w.name === "shot_index");
            if (!jsonWidget || !indexWidget) return;
            node.properties ??= {};
            node.properties.clearBetweenShots ??= false;
            // Preserve original backend widgets/order for API and workflow serialization.
            jsonWidget.type = "hidden";
            jsonWidget.computeSize = () => [0, -4];
            if (jsonWidget.inputEl) jsonWidget.inputEl.style.display = "none";
            let shots = [];
            let busy = 0;
            let removed = false;
            let closeActivePicker = null;
            const root = document.createElement("div");
            root.className = "sq-root";
            root.innerHTML = `<style>
                .sq-root{box-sizing:border-box;background:#222;color:#eee;padding:10px;font:13px system-ui;display:flex;flex-direction:column;overflow:hidden;width:100%;height:100%}
                .sq-root *{box-sizing:border-box}.sq-root button{background:#444;color:#eee;border:1px solid #666;border-radius:4px;padding:5px 8px;cursor:pointer}
                .sq-root button:disabled{opacity:.4;cursor:default}.sq-toolbar,.sq-actions,.sq-path{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
                .sq-toolbar,.sq-status{flex:none}.sq-list{flex:1;min-height:0;overflow-y:auto}
                .sq-path{flex-wrap:nowrap}.sq-path input{min-width:0;flex:1}.sq-card{border:1px solid #555;border-radius:6px;padding:9px;margin-top:9px}
                .sq-current{border-color:#66aaff}.sq-root label{display:block;margin:8px 0 4px}.sq-root input,.sq-root textarea,.sq-root select{background:#171717;color:#eee;border:1px solid #666;border-radius:4px;padding:6px;width:100%}
                .sq-root textarea{height:76px;resize:vertical}.sq-status{margin-top:8px;white-space:pre-wrap;overflow-wrap:anywhere}.sq-actions{margin-bottom:6px}
                .sq-root .sq-cleanup{display:flex;align-items:center;gap:5px;margin:0}.sq-root .sq-cleanup input{width:auto}
                .sq-picker{margin-top:6px}.sq-picker img{display:block;max-width:100%;max-height:180px;object-fit:contain;margin:6px 0}
                </style>`;
            const toolbar = document.createElement("div"); toolbar.className = "sq-toolbar";
            const status = document.createElement("div"); status.className = "sq-status"; status.setAttribute("aria-live", "polite");
            const list = document.createElement("div"); list.className = "sq-list";
            root.append(toolbar, status, list);
            const button = (parent, text, action) => {
                const b = document.createElement("button"); b.type = "button"; b.textContent = text; b.onclick = action; parent.append(b); return b;
            };
            const changed = () => { jsonWidget.value = JSON.stringify(shots); node.graph?.setDirtyCanvas(true, true); };
            const editable = () => !runner && busy === 0;
            const message = text => { status.textContent = text; };
            function restore(value) {
                try {
                    const parsed = JSON.parse(value);
                    if (!Array.isArray(parsed) || parsed.some(s => !s || typeof s !== "object" || ["start_frame", "end_frame", "prompt"].some(k => typeof s[k] !== "string") || typeof s.duration !== "number" || !Number.isFinite(s.duration) || s.duration <= 0)) throw new Error("Invalid shot data");
                    shots = parsed;
                    render();
                } catch (e) {
                    shots = []; render();
                    message(`Cannot read saved queue: ${e.message}. Raw data has been preserved.`);
                }
            }
            async function browse(shot, key, input, browseButton) {
                if (!editable()) return;
                const picker = document.createElement("input"); picker.type = "file"; picker.accept = "image/*";
                picker.onchange = async () => {
                    const file = picker.files?.[0]; if (!file || removed || !editable()) return;
                    busy++; browseButton.disabled = true; message("Uploading image…");
                    try {
                        const data = new FormData();
                        // Unique filenames avoid overwriting an image used by another shot.
                        const uploadId = crypto.randomUUID?.() ?? Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
                        data.append("image", file, `${uploadId}_${file.name}`);
                        data.append("type", "input"); data.append("subfolder", "shot_queue");
                        const response = await api.fetchApi("/upload/image", { method: "POST", body: data });
                        if (!response.ok) throw new Error(`Upload failed (${response.status})`);
                        const uploaded = await response.json();
                        if (removed) return;
                        if (typeof uploaded.name !== "string") throw new Error("Upload response has no filename");
                        shot[key] = `${uploaded.subfolder ? uploaded.subfolder + "/" : ""}${uploaded.name}${uploaded.type && uploaded.type !== "input" ? ` [${uploaded.type}]` : ""}`;
                        input.value = shot[key]; changed(); message("Image path saved.");
                    } catch (e) { if (!removed) message(e.message); }
                    finally { busy--; browseButton.disabled = false; }
                };
                picker.click();
            }
            function render() {
                closeActivePicker?.();
                list.replaceChildren();
                shots.forEach((shot, i) => {
                    const card = document.createElement("div"); card.className = `sq-card ${i === indexWidget.value ? "sq-current" : ""}`;
                    const actions = document.createElement("div"); actions.className = "sq-actions";
                    const title = document.createElement("strong"); title.textContent = `Shot ${i + 1}`; actions.append(title);
                    button(actions, "Select", () => { if (!editable()) return; indexWidget.value = i; render(); changed(); });
                    button(actions, "Remove", () => {
                        if (!editable()) return;
                        const selected = shots[indexWidget.value]; shots.splice(i, 1);
                        const next = shots.indexOf(selected); indexWidget.value = next >= 0 ? next : Math.max(0, Math.min(i, shots.length - 1)); changed(); render();
                    });
                    for (const [text, delta] of [["Move Up", -1], ["Move Down", 1]]) {
                        const b = button(actions, text, () => {
                            if (!editable()) return;
                            const selected = shots[indexWidget.value]; [shots[i], shots[i + delta]] = [shots[i + delta], shots[i]];
                            indexWidget.value = shots.indexOf(selected); changed(); render();
                        }); b.disabled = i + delta < 0 || i + delta >= shots.length;
                    }
                    card.append(actions);
                    for (const [key, text] of [["start_frame", "Start Frame"], ["end_frame", "End Frame"]]) {
                        const label = document.createElement("label"); label.textContent = text;
                        const row = document.createElement("div"); row.className = "sq-path";
                        const input = document.createElement("input"); input.value = shot[key]; input.placeholder = "shot_queue/image.png or server path"; input.setAttribute("aria-label", `${text} Shot ${i + 1}`);
                        input.oninput = () => { if (editable()) { shot[key] = input.value; changed(); } else input.value = shot[key]; };
                        button(row, "Preview", () => {
                            if (removed) return;
                            if (!shot[key].trim()) { message("Choose an image path first."); return; }
                            closeActivePicker?.();
                            existing.style.display = "none"; useButton.style.display = "none"; cancelButton.style.display = "none"; closePreviewButton.style.display = "";
                            preview.src = previewURL(shot[key]); preview.style.display = "block";
                            pickerPanel.style.display = ""; closeActivePicker = closePicker;
                        });
                        row.append(input); const b = button(row, "Browse", () => browse(shot, key, input, b));
                        const pickerPanel = document.createElement("div"); pickerPanel.className = "sq-picker"; pickerPanel.style.display = "none";
                        const existing = document.createElement("select"); existing.setAttribute("aria-label", `Uploaded ${text} Shot ${i + 1}`);
                        const preview = document.createElement("img"); preview.alt = `Preview for ${text} Shot ${i + 1}`; preview.style.display = "none";
                        preview.onerror = () => { if (pickerPanel.style.display !== "none") { preview.style.display = "none"; message("Preview unavailable. Check the image path."); } };
                        const pickerActions = document.createElement("div"); pickerActions.className = "sq-actions";
                        const closePicker = () => { pickerPanel.style.display = "none"; existing.value = ""; preview.removeAttribute("src"); preview.style.display = "none"; useButton.disabled = true; if (closeActivePicker === closePicker) closeActivePicker = null; };
                        const useButton = button(pickerActions, "Use Image", () => {
                            if (!editable() || !existing.value) return;
                            shot[key] = existing.value; input.value = shot[key]; changed(); closePicker(); message("Image path saved.");
                        });
                        useButton.disabled = true;
                        const cancelButton = button(pickerActions, "Cancel", closePicker);
                        const closePreviewButton = button(pickerActions, "Close Preview", closePicker); closePreviewButton.style.display = "none";
                        existing.onchange = () => {
                            if (!editable() || !existing.value) { preview.removeAttribute("src"); preview.style.display = "none"; useButton.disabled = true; return; }
                            preview.src = previewURL(existing.value);
                            preview.style.display = "block"; useButton.disabled = false;
                        };
                        pickerPanel.append(existing, preview, pickerActions);
                        const selectButton = button(row, "Select Image", async () => {
                            if (!editable()) return;
                            busy++; selectButton.disabled = true; message("Loading uploaded images…");
                            try {
                                const response = await api.fetchApi("/shot_queue/images");
                                if (!response.ok) throw new Error(`Image list failed (${response.status})`);
                                const { files } = await response.json();
                                if (removed) return;
                                if (!Array.isArray(files)) throw new Error("Invalid image list response");
                                const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = files.length ? "Choose uploaded image…" : "No uploaded images found";
                                existing.replaceChildren(placeholder);
                                for (const path of files) {
                                    const option = document.createElement("option"); option.value = path; option.textContent = path; existing.append(option);
                                }
                                closeActivePicker?.();
                                existing.style.display = ""; useButton.style.display = ""; cancelButton.style.display = ""; closePreviewButton.style.display = "none";
                                existing.value = ""; preview.removeAttribute("src"); preview.style.display = "none"; useButton.disabled = true; pickerPanel.style.display = ""; closeActivePicker = closePicker;
                                if (files.length) existing.focus();
                                message(files.length ? "Choose an image to preview." : "No images found in ComfyUI input.");
                            } catch (e) { if (!removed) message(e.message); }
                            finally { busy--; selectButton.disabled = false; }
                        });
                        label.append(row, pickerPanel); card.append(label);
                    }
                    const promptLabel = document.createElement("label"); promptLabel.textContent = "Prompt";
                    const prompt = document.createElement("textarea"); prompt.value = shot.prompt;
                    prompt.oninput = () => { if (editable()) { shot.prompt = prompt.value; changed(); } else prompt.value = shot.prompt; }; promptLabel.append(prompt); card.append(promptLabel);
                    const durationLabel = document.createElement("label"); durationLabel.textContent = "Duration (seconds)";
                    const duration = document.createElement("input"); duration.type = "number"; duration.min = "0.01"; duration.step = "0.1"; duration.value = shot.duration;
                    duration.onchange = () => { const v = duration.valueAsNumber; if (editable() && Number.isFinite(v) && v > 0) { shot.duration = v; changed(); } else duration.value = shot.duration; };
                    durationLabel.append(duration); card.append(durationLabel); list.append(card);
                });
            }
            button(toolbar, "Add Shot", () => { if (!editable()) return; shots.push(blank()); changed(); render(); });
            const cleanupLabel = document.createElement("label"); cleanupLabel.className = "sq-cleanup";
            const cleanup = document.createElement("input"); cleanup.type = "checkbox"; cleanup.checked = !!node.properties.clearBetweenShots;
            cleanup.onchange = () => {
                if (!editable()) { cleanup.checked = !!node.properties.clearBetweenShots; return; }
                node.properties.clearBetweenShots = cleanup.checked;
                node.graph?.setDirtyCanvas(true, true);
            };
            const cleanupText = document.createElement("span"); cleanupText.textContent = "Clear cache + unload models between shots";
            cleanupLabel.append(cleanup, cleanupText); toolbar.append(cleanupLabel);
            const runButton = button(toolbar, "Run Queue", async () => {
                if (!editable() || removed) return;
                if (!shots.length) { message("Add at least one shot."); return; }
                const state = { stop: false, node }; runner = state; runButton.disabled = true; cleanup.disabled = true; closeActivePicker?.();
                try {
                    changed();
                    const snapshot = await app.graphToPrompt();
                    if (state.stop || removed) return;
                    const own = snapshot.output[String(node.id)];
                    if (!own || own.class_type !== "ShotQueue") throw new Error("Connect this Shot Queue to the executable workflow first.");
                    if (Object.values(snapshot.output).filter(n => n.class_type === "ShotQueue").length !== 1) throw new Error("Run Queue supports one connected Shot Queue per workflow.");
                    const frozenShots = JSON.stringify(shots);
                    const clearBetweenShots = cleanup.checked;
                    let submitted = 0;
                    for (let i = 0; i < shots.length && !state.stop; i++) {
                        const job = structuredClone(snapshot);
                        job.output[String(node.id)].inputs.shots_json = frozenShots;
                        job.output[String(node.id)].inputs.shot_index = i;
                        // Ensure saved output metadata also identifies the submitted shot.
                        const savedNode = job.workflow.nodes?.find(n => String(n.id) === String(node.id));
                        if (savedNode?.widgets_values) { savedNode.widgets_values[0] = frozenShots; savedNode.widgets_values[1] = i; }
                        if (savedNode?.widgets_values_named) { savedNode.widgets_values_named.shots_json = frozenShots; savedNode.widgets_values_named.shot_index = i; }
                        message(`${clearBetweenShots ? "Running" : "Queueing"} Shot ${i + 1}/${shots.length}…`);
                        const result = await api.queuePrompt(0, job);
                        if (!result?.prompt_id || Object.keys(result.node_errors || {}).length) throw new Error("Workflow submission failed. Check node validation errors.");
                        submitted++;
                        if (!clearBetweenShots) continue;
                        if (i < shots.length - 1) {
                            const response = await api.fetchApi("/free", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ free_memory: true }) });
                            if (!response.ok) throw new Error(`Memory cleanup request failed (${response.status})`);
                        }
                        if (!await waitForCompletion(result.prompt_id, state)) break;
                    }
                    message(state.stop ? `Stopped submitting. ${submitted} shot(s) already in ComfyUI may still run.` : clearBetweenShots ? "All shots completed." : `Queued ${submitted} shots in ComfyUI. You can close this browser tab.`);
                } catch (e) { message(`Queue stopped: ${e.message} Already submitted shots may still run.`); }
                finally { if (runner === state) runner = null; runButton.disabled = false; cleanup.disabled = false; }
            });
            button(toolbar, "Stop Queue", () => {
                if (runner?.node === node) { runner.stop = true; message("Stopping submissions; submitted shots may still run."); }
                else message("Use ComfyUI's queue controls to cancel already submitted shots.");
            });
            const dom = node.addDOMWidget("shot_queue_ui", "shot_queue_ui", root, {
                serialize: false,
                getMinHeight: () => 180,
            });
            delete dom.computeSize;
            dom.computeLayoutSize = () => ({ minWidth: 300, minHeight: 180 });
            const oldConfigure = node.onConfigure;
            node.onConfigure = function () { oldConfigure?.apply(this, arguments); restore(jsonWidget.value); cleanup.checked = !!node.properties?.clearBetweenShots; };
            const oldRemoved = node.onRemoved;
            node.onRemoved = function () { removed = true; closeActivePicker?.(); if (runner?.node === node) runner.stop = true; oldRemoved?.apply(this, arguments); };
            const oldIndexCallback = indexWidget.callback;
            indexWidget.callback = function () { oldIndexCallback?.apply(this, arguments); render(); };
            restore(jsonWidget.value); node.setSize([480, 670]);
        };
    },
});
