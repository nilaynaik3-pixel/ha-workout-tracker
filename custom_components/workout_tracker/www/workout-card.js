/*
 * Workout Tracker cards and sidebar page.
 *
 * Works with either back end:
 *  - the Workout Tracker integration: data is streamed over the websocket
 *    (workout_tracker/subscribe) and actions call workout_tracker.* services;
 *  - the earlier Pyscript setup: data comes from pyscript.workout_* entities
 *    and actions call pyscript.workout_* services.
 * No dependencies.
 *
 *   type: custom:workout-card
 */

const FONT_URL =
  "https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600&display=swap";

const ACTIONS = {
  increase: { cls: "up", verb: (w) => `Go up to ${w} kg` },
  hold: { cls: "stay", verb: (w) => `Stay at ${w} kg` },
  reduce: { cls: "down", verb: (w) => `Drop to ${w} kg` },
  start: { cls: "new", verb: (w) => (w ? `Start around ${w} kg` : "Find your weight") },
};
const RPES = [6, 7, 7.5, 8, 8.5, 9, 9.5, 10];
const WT_VERSION = "1.3.0";
const REST_CHOICES = [0, 60, 90, 120, 180];

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (x) => (x === null || x === undefined || x === "" ? "" : String(Math.round(Number(x) * 100) / 100));
const title = (s) => String(s || "").replace(/^./, (c) => c.toUpperCase());

const mmss = (secs) => {
  const s = Math.max(0, Math.ceil(secs));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

// ---------------------------------------------------------------- exercise library
// free-exercise-db (public domain), shipped with the integration.
const demoSrc = (id, frame) => `/workout_tracker/demo/${encodeURIComponent(id)}/${frame}.webp`;
const SYNONYMS = {
  machine: /machine|leverage|lever|smith/, db: /dumbbell/, bb: /barbell/, ez: /e-z|ez/,
  pulldown: /pulldown|pull-down/, pushdown: /pushdown|push-down/, curl: /curl/, raise: /raise/,
};
const LIB = {
  list: null,
  byId: null,
  promise: null,
  load() {
    if (!this.promise) {
      this.promise = fetch(`/workout_tracker/static/library.json?v=${WT_VERSION}`)
        .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
        .then((list) => { this.list = list; this.byId = new Map(list.map((x) => [x.id, x])); return list; })
        .catch((err) => { this.promise = null; throw err; });
    }
    return this.promise;
  },
  /** Best matches for a search: all words first, then most words; strength work before stretches. */
  search(q, group, limit = 40) {
    if (!this.list) return [];
    const words = String(q || "").toLowerCase().split(/[^a-z0-9-]+/).filter(Boolean);
    const out = [];
    for (const x of this.list) {
      if (group && x.g !== group) continue;
      const name = x.n.toLowerCase();
      const hay = `${name} ${x.e}`;
      let hit = 0;
      for (const w of words) if (hay.includes(w) || (SYNONYMS[w] && SYNONYMS[w].test(hay))) hit++;
      if (words.length && !hit) continue;
      if (words.length > 2 && hit < words.length - 1) continue;
      const kind = x.c === "strength" || x.c === "powerlifting" ? 0 : x.c === "stretching" ? 2 : 1;
      const starts = words.length && name.startsWith(words[0]) ? 0 : 1;
      out.push([words.length - hit, kind, starts, name, x]);
    }
    out.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2] || (a[3] < b[3] ? -1 : 1));
    return out.slice(0, limit).map((r) => r[4]);
  },
};

/** Two pictures shown in turn: start and end of the movement. */
function demoAnim(id, cls = "") {
  const phase = Date.now() % 2000;   // keep the rhythm steady across re-renders
  return `<div class="anim ${cls}" style="--ph:-${phase}ms">
    <img src="${demoSrc(id, 0)}" alt="" loading="lazy" decoding="async">
    <img src="${demoSrc(id, 1)}" alt="" loading="lazy" decoding="async" onerror="this.remove()">
  </div>`;
}

// ---------------------------------------------------------------- data layer
const WT = {
  bundle: null,
  listeners: new Set(),
  subscribed: false,
  unsub: null,
  hass: null,
  profile: null,      // chosen profile (HA user id); null = the logged-in user
  profiles: null,     // { me, owner, profiles: [{id, name, has_data}] }
  integration(hass) {
    return !!hass?.config?.components?.includes("workout_tracker");
  },
  notify() {
    this.listeners.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } });
  },
  ensure(hass) {
    if (!this.integration(hass) || !hass.connection) return;
    this.hass = hass;
    if (this.profiles === null) {
      this.profiles = { profiles: [] };
      try { this.profile = localStorage.getItem("workout-tracker-profile") || null; } catch (e) { /* storage blocked */ }
      hass.callWS({ type: "workout_tracker/profiles" }).then((r) => {
        this.profiles = r;
        if (this.profile && !r.profiles.some((p) => p.id === this.profile)) this.setProfile(null);
        this.notify();
      }).catch((err) => console.error("Workout Tracker: profiles failed", err));
    }
    if (this.subscribed) return;
    this.subscribed = true;
    const msg = { type: "workout_tracker/subscribe" };
    if (this.profile) msg.profile = this.profile;
    // A switch of profile can happen before the old subscription is confirmed;
    // the generation number keeps a late update from the old one out.
    const gen = this.gen = (this.gen || 0) + 1;
    hass.connection.subscribeMessage((bundle) => {
      if (gen !== this.gen) return;
      this.bundle = bundle;
      this.notify();
    }, msg).then((unsub) => {
      if (gen === this.gen) this.unsub = unsub;
      else { try { unsub(); } catch (e) { /* already closed */ } }
    }).catch((err) => {
      if (gen !== this.gen) return;
      this.subscribed = false;
      console.error("Workout Tracker: subscribe failed", err);
    });
  },
  /** Whose data is shown. */
  current() {
    return this.profile || this.profiles?.me || null;
  },
  setProfile(id) {
    const me = this.profiles?.me;
    this.profile = id && id !== me ? id : null;
    try {
      if (this.profile) localStorage.setItem("workout-tracker-profile", this.profile);
      else localStorage.removeItem("workout-tracker-profile");
    } catch (e) { /* storage blocked */ }
    if (this.unsub) { try { this.unsub(); } catch (e) { /* already closed */ } }
    this.unsub = null;
    this.subscribed = false;
    this.bundle = null;
    this.notify();
    if (this.hass) this.ensure(this.hass);
  },
};

/** One part of the tracker's data: plan, view, selected, day, stats or export. */
function wtGet(hass, part) {
  if (WT.integration(hass)) return WT.bundle?.[part] || null;
  return hass?.states[`pyscript.workout_${part}`];
}

const INT_SERVICE = { workout_select: "select_exercise" };
function wtCall(hass, service, data = {}) {
  if (WT.integration(hass)) {
    const d = WT.profile ? { ...data, profile: WT.profile } : data;
    return hass.callService("workout_tracker", INT_SERVICE[service] || service.replace(/^workout_/, ""), d);
  }
  return hass.callService("pyscript", service, data);
}

/** True when the back end keeps today's selection itself (all current versions). */
function wtOwnSelection(hass) {
  const a = wtGet(hass, "plan")?.attributes;
  return !!a && "current_main" in a;
}

function wtMain(hass) {
  if (wtOwnSelection(hass)) return wtGet(hass, "plan").attributes.current_main || "";
  return (hass?.states["input_select.workout_main"]?.state || "").toLowerCase();
}

function wtAddonOn(hass, group) {
  if (wtOwnSelection(hass)) return (wtGet(hass, "plan").attributes.current_addons || []).includes(group);
  return hass?.states[`input_boolean.workout_add_${group}`]?.state === "on";
}

/** Dark theme? Home Assistant's flag, or light text (custom dark themes). */
function wtIsDark(el, hass) {
  return true;   // one look everywhere: the dark design shared with the home dashboard
}
function wtIsDarkTheme(el, hass) {
  if (hass?.themes?.darkMode) return true;
  const m = /rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)/.exec(getComputedStyle(el).color || "");
  if (!m) return false;
  const [r, g, b] = m.slice(1, 4).map(Number);
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 > 0.6;
}

function localISO(offsetDays) {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function loadFont() {
  if (document.querySelector(`link[href="${FONT_URL}"]`)) return;
  const l = document.createElement("link");
  l.rel = "stylesheet";
  l.href = FONT_URL;
  document.head.appendChild(l);
}

class WorkoutCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._sel = null;      // exercise the local inputs belong to
    this._todayCount = -1;
    this._w = 0;
    this._r = 15;
    this._rpe = null;
    this._busy = false;
    this._flash = "";
    this._newName = "";
    this._newGroup = "";
    this._newStep = "2.5";
    this._manageOpen = false;
    this._newDemo = null;     // library exercise picked in the add form
    this._demoOpen = false;   // big demo + instructions shown
    this._picker = null;      // {mode: "add" | "demo", q, group} while searching the library
    this._restMenu = false;
    loadFont();
    this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
    this.shadowRoot.addEventListener("input", (e) => this._onInput(e));
    this.shadowRoot.addEventListener("change", (e) => this._onInput(e));
    this.shadowRoot.addEventListener("toggle", (e) => {
      if (e.target.classList?.contains("manage")) this._manageOpen = e.target.open;
    }, true);
  }

  setConfig(config) {
    this._config = config || {};
  }

  getCardSize() {
    return 12;
  }

  getGridOptions() {
    return { columns: "full" };
  }

  connectedCallback() {
    this._onStore = () => this._update();
    WT.listeners.add(this._onStore);
  }

  disconnectedCallback() {
    WT.listeners.delete(this._onStore);
    this._stopTick();
  }

  set hass(hass) {
    this._hass = hass;
    this.toggleAttribute("data-dark", wtIsDark(this, hass));
    WT.ensure(hass);
    this._update();
  }

  _update() {
    const hass = this._hass;
    if (!hass) return;
    let key = ["plan", "view", "selected", "rest"].map((p) => wtGet(hass, p)?.last_updated || "-").join("|");
    if (!WT.integration(hass)) {
      key += ["input_select.workout_main", "input_boolean.workout_add_biceps",
        "input_boolean.workout_add_triceps", "input_boolean.workout_add_abs"]
        .map((id) => hass.states[id]?.last_updated || "-").join("|");
    }
    if (key === this._key) return;
    this._key = key;
    this._syncLocal();
    this._render();
  }

  // ---------------------------------------------------------------- state
  _st(id) {
    const m = /^pyscript\.workout_(\w+)$/.exec(id);
    return m ? wtGet(this._hass, m[1]) : this._hass?.states[id];
  }

  _syncLocal() {
    const sel = this._st("pyscript.workout_selected");
    if (!sel || sel.state === "none") return;
    const a = sel.attributes;
    const today = a.today || [];
    const work = today.filter((t) => t.kind !== "warmup");
    if (sel.state !== this._sel || (a.log_day || null) !== this._logDay) {
      this._sel = sel.state;
      this._logDay = a.log_day || null;
      this._todayCount = today.length;
      this._noteEdit = false;
      this._warm = !!a.warmup_default;
      const last = work[work.length - 1];
      if (this._warm && a.warmup_weight) { this._w = a.warmup_weight; this._r = a.default_reps ?? 15; }
      else {
        this._w = last ? last.weight : a.default_weight ?? 0;
        this._r = last ? last.reps : a.default_reps ?? 15;
      }
      this._rpe = null;
      this._pendingEx = null;
    } else if (today.length !== this._todayCount) {
      const grew = today.length > this._todayCount;
      this._todayCount = today.length;
      this._rpe = null;
      const just = today[today.length - 1];
      if (grew && just && just.kind === "warmup") {
        // Warm-up done: switch to the working weight for the next set.
        this._warm = false;
        this._w = a.default_weight ?? this._w;
        this._r = a.default_reps ?? this._r;
      } else if (!grew && !today.length && a.warmup_default) {
        this._warm = true;
      }
    }
  }

  _call(service, data = {}) {
    return wtCall(this._hass, service, data);
  }

  _haptic(kind) {
    this.dispatchEvent(new CustomEvent("haptic", { detail: kind, bubbles: true, composed: true }));
  }

  // --------------------------------------------------------------- events
  async _onClick(e) {
    const btn = e.target.closest("[data-action]");
    if (!btn || btn.disabled) return;
    const act = btn.dataset.action;
    const sel = this._st("pyscript.workout_selected");
    const step = Number(sel?.attributes.step) || 2.5;

    switch (act) {
      case "main":
        if (wtOwnSelection(this._hass)) this._call("workout_select_main", { group: btn.dataset.g });
        else this._hass.callService("input_select", "select_option", {
          entity_id: "input_select.workout_main", option: title(btn.dataset.g),
        });
        break;
      case "addon":
        if (wtOwnSelection(this._hass)) this._call("workout_toggle_addon", { group: btn.dataset.g });
        else this._hass.callService("input_boolean", "toggle", {
          entity_id: `input_boolean.workout_add_${btn.dataset.g}`,
        });
        break;
      case "suggest":
        this._call("workout_suggest");
        break;
      case "day":
        this._call("workout_set_day", { day: btn.dataset.d });
        break;
      case "ex":
        this._pendingEx = btn.dataset.name;
        this._render();
        this._call("workout_select", { exercise: btn.dataset.name });
        break;
      case "w-":
      case "w+":
        this._readInputs();
        this._w = Math.max(0, Math.round((this._w + (act === "w+" ? step : -step)) * 100) / 100);
        this._render();
        break;
      case "r-":
      case "r+":
        this._readInputs();
        this._r = Math.max(1, this._r + (act === "r+" ? 1 : -1));
        this._render();
        break;
      case "rpe": {
        const v = Number(btn.dataset.v);
        this._readInputs();
        this._rpe = this._rpe === v ? null : v;
        this._render();
        break;
      }
      case "log": {
        this._readInputs();
        if (!sel || sel.state === "none") return;
        this._busy = true;
        this._render();
        try {
          await this._call("workout_log_set", {
            exercise: sel.state, weight: this._w, reps: this._r, rpe: this._rpe ?? 0, warmup: !!this._warm,
          });
          this._haptic("success");
          this._flash = `Logged ${this._warm ? "warm-up " : ""}${this._r} \u00d7 ${num(this._w)} kg`;
          setTimeout(() => { this._flash = ""; this._render(); }, 1800);
        } catch (err) {
          this._flash = `Couldn't log the set: ${err.message || err}`;
          this._haptic("failure");
        }
        this._busy = false;
        this._render();
        break;
      }
      case "warm":
        this._readInputs();
        this._warm = !this._warm;
        if (this._warm && sel.attributes.warmup_weight) this._w = sel.attributes.warmup_weight;
        else if (!this._warm) this._w = sel.attributes.default_weight ?? this._w;
        this._render();
        break;
      case "note-edit":
        this._noteEdit = true;
        this._render();
        this.shadowRoot.getElementById("note-in")?.focus();
        break;
      case "note-cancel":
        this._noteEdit = false;
        this._render();
        break;
      case "note-save": {
        const v = this.shadowRoot.getElementById("note-in")?.value ?? "";
        this._noteEdit = false;
        this._render();
        this._call("workout_set_note", { exercise: sel.state, note: v });
        break;
      }
      case "pr-ok":
        this._prSeen = btn.dataset.id;
        this._render();
        break;
      case "gym-start":
        this._call("workout_gym_start");
        break;
      case "gym-end":
        if (confirm("End this gym visit now?")) this._call("workout_gym_end");
        break;
      case "rpeall":
        if (this._rpe) this._call("workout_rpe_all", { exercise: sel.state, rpe: this._rpe });
        break;
      case "undo":
        this._call("workout_undo", { exercise: sel.state });
        this._haptic("light");
        break;
      case "add": {
        this._readInputs();
        const name = this._newName.trim();
        if (!name) {
          this._flashAdd = "Type a name first.";
          this._render();
          return;
        }
        const data = { name, group: this._newGroup, step: Number(this._newStep) };
        if (this._newDemo) data.demo = this._newDemo;
        await this._call("workout_add_exercise", data);
        this._flashAdd = `Added ${name}.`;
        this._newName = "";
        this._newDemo = null;
        this._render();
        break;
      }
      case "demo-toggle":
        this._demoOpen = !this._demoOpen;
        this._picker = null;
        if (this._demoOpen) LIB.load().then(() => this._render()).catch(() => {});
        this._render();
        break;
      case "demo-change":
        this._openPicker("demo", sel?.attributes.group || "");
        break;
      case "demo-remove":
        if (sel && confirm(`Remove the demo from ${sel.state}?`)) {
          this._demoOpen = false;
          this._call("workout_set_demo", { exercise: sel.state, demo: "" });
        }
        break;
      case "lib-open":
        this._readInputs();
        this._openPicker("add", "");
        break;
      case "lib-g":
        this._picker.group = btn.dataset.g;
        this._render();
        break;
      case "lib-close":
        this._picker = null;
        this._render();
        break;
      case "lib-pick": {
        const x = LIB.byId?.get(btn.dataset.id);
        if (!x) return;
        if (this._picker?.mode === "demo") {
          this._picker = null;
          this._demoOpen = true;
          this._render();
          this._call("workout_set_demo", { exercise: sel.state, demo: x.id });
        } else {
          const groups = this._groupList();
          this._newName = x.n;
          this._newGroup = groups.includes(x.g) ? x.g : this._newGroup;
          this._newStep = String(x.st);
          this._newDemo = x.id;
          this._picker = null;
          this._flashAdd = "";
          this._render();
        }
        break;
      }
      case "new-demo-clear":
        this._newDemo = null;
        this._render();
        break;
      case "rest-menu":
        this._restMenu = !this._restMenu;
        this._render();
        break;
      case "rest-set":
        this._restMenu = false;
        this._call("workout_rest_settings", { seconds: Number(btn.dataset.s) });
        break;
      case "rest-notify":
        this._call("workout_rest_settings", { notify: btn.getAttribute("aria-pressed") !== "true" });
        break;
      case "rest-start":
        this._restMenu = false;
        this._call("workout_rest_start", { exercise: sel?.state });
        break;
      case "rest-add":
        this._call("workout_rest_add", { seconds: Number(btn.dataset.s) });
        break;
      case "rest-stop":
        this._call("workout_rest_stop");
        break;
      case "rest-dismiss":
        this._restOver = null;
        this._render();
        break;
      case "hide":
        if (sel && confirm(`Hide ${sel.state}? Its history is kept, and adding it again with the same name brings it back.`)) {
          this._call("workout_hide_exercise", { exercise: sel.state });
        }
        break;
    }
  }

  _onInput(e) {
    const t = e.target;
    if (t.id === "log-date" && e.type === "change" && t.value) {
      this._call("workout_set_day", { day: t.value === localISO(0) ? "today" : t.value });
      return;
    }
    if (t.id === "lib-q") {
      this._picker.q = t.value;
      const list = this.shadowRoot.querySelector(".lib-list");
      if (list) list.innerHTML = this._libItems();
      return;
    }
    if (t.id === "new-name") this._newName = t.value;
    if (t.id === "new-group") this._newGroup = t.value;
    if (t.id === "new-step") this._newStep = t.value;
    if (t.id === "w" || t.id === "r") this._readInputs();
  }

  _openPicker(mode, group) {
    this._picker = { mode, q: "", group };
    this._render();
    LIB.load().then(() => {
      this._render();
      this.shadowRoot.getElementById("lib-q")?.focus();
    }).catch((err) => {
      this._picker = { ...this._picker, error: String(err.message || err) };
      this._render();
    });
  }

  _groupList() {
    const p = this._st("pyscript.workout_plan")?.attributes || {};
    return [...(p.main || []), ...(p.addon || [])].map((r) => r.group);
  }

  _readInputs() {
    const w = this.shadowRoot.getElementById("w");
    const r = this.shadowRoot.getElementById("r");
    if (w && w.value !== "" && !isNaN(Number(w.value.replace(",", ".")))) this._w = Number(w.value.replace(",", "."));
    if (r && r.value !== "" && !isNaN(Number(r.value))) this._r = Math.max(1, Math.round(Number(r.value)));
  }

  // --------------------------------------------------------------- render
  _render() {
    if (!this._hass) return;
    const plan = this._st("pyscript.workout_plan");
    const view = this._st("pyscript.workout_view");
    const sel = this._st("pyscript.workout_selected");

    if (!plan || !view) {
      this.shadowRoot.innerHTML = `<style>${CSS}</style><ha-card><div class="wc empty card">
        <p class="big">${WT.integration(this._hass) ? "Loading\u2026" : "Waiting for the tracker"}</p>
        <p>The workout entities don't exist yet. Check that Pyscript is installed with
        "Allow all imports" on, then look in Settings \u2192 System \u2192 Logs for <code>pyscript</code>.</p>
      </div></ha-card>`;
      return;
    }

    // keep focus/caret for the text field across re-renders
    const active = this.shadowRoot.activeElement?.id;

    this.shadowRoot.innerHTML = `<style>${CSS}</style>
      <ha-card><div class="wc"><div class="cols">
        <div class="card col-a">
          ${this._renderDay(plan, view)}
          ${this._renderExercises(view, sel)}
        </div>
        <div class="card col-b">
          ${sel && sel.state !== "none" ? this._renderLog(sel) + this._renderHistory(sel) : `<p class="hint">Pick an exercise to start logging.</p>`}
        </div>
        <div class="card col-c">${this._renderManage(plan, sel)}</div>
      </div></div></ha-card>`;

    if (active === "new-name" || active === "lib-q") {
      const el = this.shadowRoot.getElementById(active);
      el?.focus();
      el?.setSelectionRange(el.value.length, el.value.length);
    }
    this._syncTick();
  }

  // ------------------------------------------------------------ rest timer
  _rest() {
    const r = wtGet(this._hass, "rest");
    return r && r.attributes && "seconds" in r.attributes ? r : null;
  }

  _syncTick() {
    const r = this._rest();
    if (r && r.state === "running") {
      if (!this._tick) this._tick = setInterval(() => this._paintRest(), 250);
      this._paintRest();
    } else {
      this._stopTick();
    }
  }

  _stopTick() {
    if (this._tick) clearInterval(this._tick);
    this._tick = null;
  }

  /** Update the countdown in place, without re-rendering the card. */
  _paintRest() {
    const r = this._rest();
    const box = this.shadowRoot.querySelector(".rest.on");
    if (!r || r.state !== "running" || !box) return;
    const a = r.attributes;
    const left = (a.ends - Date.now()) / 1000;
    const total = Math.max(1, (a.ends - a.started) / 1000);
    box.querySelector(".rest-time").textContent = left > 0 ? mmss(left) : "Go";
    box.querySelector(".rest-bar i").style.transform = `scaleX(${Math.max(0, Math.min(1, left / total))})`;
    if (left <= 0 && !box.classList.contains("over")) {
      box.classList.add("over");
      box.querySelector(".rest-lbl").textContent = "Rest over";
      this._buzz(a.started);
    }
  }

  _buzz(token) {
    if (this._buzzed === token) return;
    this._buzzed = token;
    this._haptic("success");
    try { navigator.vibrate?.([120, 80, 120]); } catch (e) { /* not supported */ }
  }

  _renderRest(sel) {
    const r = this._rest();
    if (!r || (sel.attributes.log_day)) return "";
    const a = r.attributes;
    // The timer ran out (rather than being skipped): say so for a little while.
    const prev = this._lastRest;
    this._lastRest = r.state === "running" ? { ends: a.ends, started: a.started } : null;
    if (r.state === "running") this._restOver = null;
    else if (prev && Date.now() >= prev.ends - 1500) {
      this._restOver = { until: Date.now() + 15000, started: prev.started };
      this._buzz(prev.started);
      setTimeout(() => this._render(), 15100);
    }
    if (r.state !== "running" && this._restOver && Date.now() < this._restOver.until) {
      return `<div class="rest on over" role="status">
        <div class="rest-row">
          <span class="rest-time">Go</span>
          <span class="rest-lbl">Rest over: time for the next set</span>
          <button class="chip" data-action="rest-dismiss">OK</button>
        </div>
      </div>`;
    }
    if (r.state === "running") {
      return `<div class="rest on" role="timer" aria-live="off">
        <div class="rest-bar"><i></i></div>
        <div class="rest-row">
          <span class="rest-time">${mmss((a.ends - Date.now()) / 1000)}</span>
          <span class="rest-lbl">Rest${a.exercise && a.exercise !== sel.state ? ` after ${esc(a.exercise)}` : ""}</span>
          <button class="chip" data-action="rest-add" data-s="-15" aria-label="15 seconds less">\u221215</button>
          <button class="chip" data-action="rest-add" data-s="15" aria-label="15 seconds more">+15</button>
          <button class="chip" data-action="rest-stop">Skip</button>
        </div>
      </div>`;
    }
    const len = a.seconds ? mmss(a.seconds) : "off";
    const menu = this._restMenu ? `<div class="rest-menu">
        <div class="rest-opts" role="group" aria-label="Rest between sets">
          ${REST_CHOICES.map((v) => `<button class="chip ${v === a.seconds ? "on" : ""}" data-action="rest-set" data-s="${v}" aria-pressed="${v === a.seconds}">${v ? mmss(v) : "Off"}</button>`).join("")}
        </div>
        ${a.phone ? `<button class="chip ${a.notify ? "on" : ""}" data-action="rest-notify" aria-pressed="${!!a.notify}">Phone alert when rest is over</button>` : ""}
        <button class="link" data-action="rest-start">Start a rest now</button>
      </div>` : "";
    return `<div class="rest">
      <button class="link quiet" data-action="rest-menu" aria-expanded="${this._restMenu}">Rest timer: ${len}${a.seconds ? " after each set" : ""}</button>
      ${menu}
    </div>`;
  }

  // ------------------------------------------------------------ demos and library
  _renderDemoThumb(sel) {
    if (!WT.integration(this._hass)) return "";
    const id = sel.attributes.demo;
    if (!id) {
      return `<button class="demo-thumb none" data-action="demo-change" aria-label="Add a demo picture">+ demo</button>`;
    }
    return `<button class="demo-thumb ${this._demoOpen ? "open" : ""}" data-action="demo-toggle"
      aria-expanded="${this._demoOpen}" aria-label="${this._demoOpen ? "Hide" : "Show"} how to do it">${demoAnim(id)}</button>`;
  }

  _renderDemoPanel(sel) {
    if (this._picker?.mode === "demo") return this._renderPicker();
    const id = sel.attributes.demo;
    if (!this._demoOpen || !id) return "";
    const x = LIB.byId?.get(id);
    const muscles = x ? [x.m.map(title).join(", "), x.s.length ? `also ${x.s.join(", ")}` : ""].filter(Boolean).join(" \u00b7 ") : "";
    return `<div class="demo-panel">
      ${demoAnim(id, "big")}
      ${x ? `<p class="demo-meta"><b>${esc(x.n)}</b><span>${esc(muscles)}${x.e ? ` \u00b7 ${esc(x.e)}` : ""}</span></p>
        <ol class="howto">${x.i.map((t) => `<li>${esc(t)}</li>`).join("")}</ol>`
        : `<p class="hint">Loading the instructions\u2026</p>`}
      <div class="demo-tools">
        <button class="link" data-action="demo-change">Pick a different demo</button>
        <button class="link quiet" data-action="demo-remove">Remove</button>
        <button class="link quiet" data-action="demo-toggle">Close</button>
      </div>
    </div>`;
  }

  _renderPicker() {
    const pk = this._picker;
    const groups = this._groupList();
    const sel = this._st("pyscript.workout_selected");
    const head = pk.mode === "demo"
      ? `Demo for <b>${esc(sel?.state || "")}</b>`
      : "Find an exercise in the library";
    return `<div class="picker">
      <p class="picker-head">${head}</p>
      <input id="lib-q" type="search" value="${esc(pk.q)}" placeholder="Search ${LIB.list ? LIB.list.length : ""} exercises, e.g. cable fly" autocomplete="off" enterkeyhint="search">
      <div class="lib-groups">
        <button class="chip ${!pk.group ? "on" : ""}" data-action="lib-g" data-g="">All</button>
        ${groups.map((g) => `<button class="chip ${pk.group === g ? "on" : ""}" data-action="lib-g" data-g="${esc(g)}">${esc(title(g))}</button>`).join("")}
      </div>
      <ul class="lib-list">${pk.error ? `<li class="hint">Couldn't load the library (${esc(pk.error)}).</li>` : this._libItems()}</ul>
      <button class="link quiet" data-action="lib-close">Cancel</button>
    </div>`;
  }

  _libItems() {
    const pk = this._picker;
    if (!LIB.list) return `<li class="hint">Loading\u2026</li>`;
    const found = LIB.search(pk.q, pk.group);
    if (!found.length) return `<li class="hint">Nothing found. Try fewer words${pk.group ? ", or All groups" : ""}.</li>`;
    return found.map((x) => `<li><button data-action="lib-pick" data-id="${esc(x.id)}">
        ${x.f ? `<img src="${demoSrc(x.id, 0)}" alt="" loading="lazy" decoding="async">` : `<span class="noimg"></span>`}
        <span><b>${esc(x.n)}</b><small>${esc([title(x.m[0] || ""), x.e].filter(Boolean).join(" \u00b7 "))}</small></span>
      </button></li>`).join("");
  }

  _renderDay(plan, view) {
    const p = plan.attributes;
    const mainNow = wtMain(this._hass);
    const addonsOn = (p.addon || []).filter((r) => wtAddonOn(this._hass, r.group)).map((r) => r.group);
    const offPlan = mainNow !== p.next_main || addonsOn.length !== 1 || addonsOn[0] !== p.next_addon;

    const chip = (r, kind, on) => {
      const when = r.today ? "today" : r.days == null ? "new" : r.days === 0 ? "today" : `${r.days}d`;
      const due = r.group === (kind === "main" ? p.next_main : p.next_addon);
      return `<button class="g ${on ? "on" : ""} ${due ? "due" : ""}" data-action="${kind}" data-g="${esc(r.group)}"
        aria-pressed="${on}" title="${due ? "Due today" : ""}">
        <span>${esc(title(r.group))}</span><small>${when}</small></button>`;
    };

    return `<header class="day">
      <h1><span>${esc(title(mainNow) || "\u2013")}</span>${addonsOn.map((g) => `<em>+ ${esc(g)}</em>`).join("")}</h1>
      <div class="groups mains">${(p.main || []).map((r) => chip(r, "main", r.group === mainNow)).join("")}</div>
      <div class="groups addons">${(p.addon || []).map((r) => chip(r, "addon", addonsOn.includes(r.group))).join("")}</div>
      ${this._gymLine(p)}
      ${offPlan ? `<button class="link" data-action="suggest">Back to today's plan: ${esc(title(p.next_main))}${p.next_addon ? ` + ${esc(p.next_addon)}` : ""}</button>` : ""}
    </header>`;
  }

  _gymLine(p) {
    const v = p.gym_visit;
    if (v && v.start) {
      const start = new Date(v.start);
      const mins = Math.max(0, Math.round((Date.now() - start.getTime()) / 60000));
      const hhmm = v.start.slice(11, 16);
      return `<p class="gym"><i></i>At the gym since ${hhmm} \u00b7 ${mins} min
        <button class="link quiet" data-action="gym-end">End visit</button></p>`;
    }
    return `<p class="gym off">${p.gym_tracking ? "Not at the gym" : "Gym time not tracked by location"}
      <button class="link quiet" data-action="gym-start">Start a visit</button></p>`;
  }

  _renderExercises(view, sel) {
    const cards = view.attributes.cards || [];
    const current = this._pendingEx || sel?.state;
    const groups = view.attributes.groups || [];
    const rows = groups.map((g) => {
      const list = cards.filter((c) => c.group === g);
      if (!list.length) return "";
      return `<div class="ex-group">
        <h3>${esc(title(g))}</h3>
        <div class="ex-row">${list.map((c) => {
          const a = ACTIONS[c.suggestion.action] || ACTIONS.hold;
          const done = c.today_done;
          return `<button class="exc ${c.name === current ? "on" : ""} ${done ? "done" : ""}" data-action="ex" data-name="${esc(c.name)}">
            <i class="pip ${a.cls}"></i>${esc(c.name)}${done ? `<b>${done}</b>` : ""}</button>`;
        }).join("")}</div></div>`;
    }).join("");
    return `<nav class="exercises" aria-label="Exercises">${rows || `<p class="hint">No exercises in these groups yet. Add one under Manage exercises.</p>`}</nav>`;
  }

  _renderLog(sel) {
    const a = sel.attributes;
    const s = a.suggestion || {};
    const act = ACTIONS[s.action] || ACTIONS.hold;
    const today = a.today || [];
    const target = a.target_sets || 3;
    const next = today.length + 1;
    const bw = Number(this._w) === 0;

    const warmups = today.filter((t) => t.kind === "warmup");
    const work = today.filter((t) => t.kind !== "warmup");
    const plates = warmups.map((t) => `<li class="plate warm" title="Warm-up: ${t.reps} reps \u00d7 ${num(t.weight)} kg">
        <b>W</b><small>${t.reps}\u00d7${t.weight ? num(t.weight) : "bw"}</small></li>`);
    for (let i = 0; i < Math.max(target, work.length); i++) {
      const t = work[i];
      plates.push(t
        ? `<li class="plate filled" title="${t.reps} reps \u00d7 ${num(t.weight)} kg${t.rpe ? `, RPE ${num(t.rpe)}` : ""}">
             <b>${t.reps}</b><small>${t.weight ? num(t.weight) : "bw"}${t.rpe ? ` @${num(t.rpe)}` : ""}</small></li>`
        : `<li class="plate" aria-label="Set ${i + 1} to do"><small>${i + 1}</small></li>`);
    }
    const nextWork = work.length + 1;

    const missingRpe = work.some((t) => t.rpe == null);

    const pr = a.pr;
    const prBanner = pr && String(pr.set_id) !== String(this._prSeen) && pr.day === (a.log_day || localISO(0))
      ? `<div class="pr" role="status">
          <div><strong>New record</strong>
          ${pr.records.map((r) => `<span>${esc(r.label)}: <b>${num(r.value)}${r.type === "reps" ? " reps" : " kg"}</b> <small>(was ${num(r.previous)})</small></span>`).join("")}</div>
          <button class="link" data-action="pr-ok" data-id="${pr.set_id}">Nice</button>
        </div>`
      : "";
    if (prBanner && this._prHaptic !== pr.set_id) { this._prHaptic = pr.set_id; this._haptic("heavy"); }

    const note = a.note || "";
    const noteBlock = this._noteEdit
      ? `<div class="note edit">
          <textarea id="note-in" rows="2" maxlength="500" placeholder="Seat height, grip, which machine\u2026">${esc(note)}</textarea>
          <div><button class="link" data-action="note-save">Save note</button><button class="link quiet" data-action="note-cancel">Cancel</button></div>
        </div>`
      : note
        ? `<button class="note" data-action="note-edit" aria-label="Edit note"><span>${esc(note)}</span></button>`
        : `<button class="note empty" data-action="note-edit">Add a note (seat height, grip\u2026)</button>`;

    const logDay = a.log_day || null;
    const yest = localISO(-1);
    const dayPicker = logDay
      ? `<div class="logday past"><p><strong>Logging for ${esc(fmtDayLong(logDay))}</strong><span>Sets you log now are saved to that day.</span></p>
           <button class="link" data-action="day" data-d="today">Back to today</button></div>`
      : "";
    return `<section class="log">
      ${dayPicker}
      ${prBanner}
      <div class="exhead"><h2>${esc(sel.state)}</h2>${this._renderDemoThumb(sel)}</div>
      ${this._renderDemoPanel(sel)}
      ${noteBlock}
      <p class="advice ${act.cls}">
        <strong>${esc(s.action === "start" && !s.weight ? act.verb(0) : act.verb(num(s.weight)))}</strong>
        <span class="for">for ${esc(s.reps ?? 15)} reps</span>
        <span class="why">${esc(s.reason || "")}</span>
      </p>

      <div class="steppers">
        <div class="stepper">
          <button data-action="w-" aria-label="Less weight">\u2212</button>
          <label><input id="w" inputmode="decimal" value="${esc(num(this._w))}" aria-label="Weight in kg"><span>${bw ? "kg \u00b7 bodyweight" : "kg"}</span></label>
          <button data-action="w+" aria-label="More weight">+</button>
        </div>
        <div class="stepper reps">
          <button data-action="r-" aria-label="Fewer reps">\u2212</button>
          <label><input id="r" inputmode="numeric" value="${esc(this._r)}" aria-label="Reps"><span>reps</span></label>
          <button data-action="r+" aria-label="More reps">+</button>
        </div>
      </div>

      <div class="setkind">
        <button class="chip ${this._warm ? "on" : ""}" data-action="warm" aria-pressed="${!!this._warm}">Warm-up set</button>
        <span>${this._warm ? "Not counted in advice, records or set totals" : ""}</span>
      </div>

      <div class="rpe" role="group" aria-label="How hard was it (RPE)">
        <span>How hard?</span>
        ${RPES.map((v) => `<button class="${this._rpe === v ? "on" : ""}" data-action="rpe" data-v="${v}" aria-pressed="${this._rpe === v}">${v}</button>`).join("")}
      </div>

      <button class="log-btn ${this._flash && !this._flash.startsWith("Couldn") ? "ok" : ""}" data-action="log" ${this._busy ? "disabled" : ""}>
        ${this._flash ? esc(this._flash) : this._warm ? "Log warm-up" : `Log set ${nextWork}`}
      </button>
      ${this._renderRest(sel)}

      <div class="sets">
        <ol class="plates">${plates.join("")}</ol>
        <div class="set-tools">
          ${this._rpe && work.length ? `<button class="link" data-action="rpeall">Use RPE ${this._rpe} for ${missingRpe ? "today's sets without one" : "all of today's sets"}</button>` : ""}
          ${today.length ? `<button class="link quiet" data-action="undo">Undo last set</button>` : ""}
        </div>
      </div>
    </section>`;
  }

  _renderHistory(sel) {
    const a = sel.attributes;
    const series = a.series || [];
    const hist = a.history || [];
    if (!hist.length) {
      return `<section class="hist"><p class="hint">No history yet for ${esc(sel.state)}. Your first logged session sets the baseline.</p></section>`;
    }
    let spark = "";
    if (series.length >= 2) {
      const vals = series.map((p) => p.strength);
      const lo = Math.min(...vals), hi = Math.max(...vals), W = 140, H = 40, pad = 4;
      const x = (i) => pad + (i * (W - 2 * pad)) / (vals.length - 1);
      const y = (v) => (hi === lo ? H / 2 : H - pad - ((v - lo) * (H - 2 * pad)) / (hi - lo));
      const pts = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
      spark = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Estimated max over the last ${vals.length} sessions">
        <polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
        <circle cx="${x(vals.length - 1)}" cy="${y(vals[vals.length - 1])}" r="3.5" class="last"/></svg>`;
    }
    const tr = a.trend;
    const trTxt = tr == null ? "" : `<span class="${tr > 0 ? "pos" : tr < 0 ? "neg" : ""}">${tr > 0 ? "+" : ""}${tr}% vs last session</span>`;

    return `<section class="hist">
      <div class="trend">
        <div><b>${num(a.strength)} kg</b><span>estimated max</span>${trTxt}</div>
        ${spark}
      </div>
      <ol class="sessions">${hist.map((h) => {
        const i = h.indexOf(" \u00b7 ");
        return `<li><time>${esc(i > 0 ? h.slice(0, i) : "")}</time><span>${esc(i > 0 ? h.slice(i + 3) : h)}</span></li>`;
      }).join("")}</ol>
    </section>`;
  }

  _renderManage(plan, sel) {
    const p = plan.attributes;
    const groups = [...(p.main || []), ...(p.addon || [])].map((r) => r.group);
    if (!this._newGroup || !groups.includes(this._newGroup)) this._newGroup = groups[0] || "";
    const steps = ["1", "2", "2.5", "4", "5", "10"];
    if (!steps.includes(this._newStep)) steps.push(this._newStep);
    const lib = WT.integration(this._hass);
    const picked = this._newDemo
      ? `<div class="picked">${demoAnim(this._newDemo, "mini")}<span>Demo from the library</span>
           <button class="link quiet" data-action="new-demo-clear">Remove</button></div>` : "";
    return `<details class="manage" ${this._manageOpen ? "open" : ""}>
      <summary>Manage exercises</summary>
      ${lib && this._picker?.mode === "add" ? this._renderPicker()
        : lib ? `<button class="lib-btn" data-action="lib-open">Search the exercise library</button>` : ""}
      ${picked}
      <div class="form">
        <label class="f-name">Name<input id="new-name" value="${esc(this._newName)}" placeholder="e.g. Cable fly" autocomplete="off"></label>
        <label>Muscle group<select id="new-group">${groups.map((g) => `<option value="${esc(g)}" ${g === this._newGroup ? "selected" : ""}>${esc(title(g))}</option>`).join("")}</select></label>
        <label>Weight step<select id="new-step">${steps.map((s) => `<option value="${s}" ${s === this._newStep ? "selected" : ""}>${s} kg</option>`).join("")}</select></label>
        <button class="add-btn" data-action="add">Add exercise</button>
      </div>
      ${this._flashAdd ? `<p class="hint">${esc(this._flashAdd)}</p>` : ""}
      ${sel && sel.state !== "none" ? `<button class="link quiet" data-action="hide">Hide ${esc(sel.state)} from the lists</button>` : ""}
    </details>`;
  }
}

const CSS = `
:host {
  /* design tokens shared with the home dashboard */
  --bg: #141a22; --surface: #1c2430; --surface-2: #232d3b; --line: #2a3442;
  --text: #e6eaf0; --muted: #8a96a8; --faint: #5d6878;
  --lamp: #f2b544; --lamp-soft: rgba(242,181,68,.16);
  --presence: #5cc8b4; --alert: #f0605a; --night: #7c83f2; --guest: #64a7f0;
  --display: "Space Grotesk", var(--ha-font-family-body, system-ui), sans-serif;
  --body: var(--ha-font-family-body, var(--paper-font-body1_-_font-family, system-ui)), sans-serif;
  --r-lg: 20px; --r-md: 14px; --r-sm: 10px;
  /* the tracker's roles, mapped onto them */
  --wc-ink: var(--text);
  --wc-mute: var(--muted);
  --wc-faint: var(--faint);
  --wc-line: var(--line);
  --wc-bg: var(--surface);
  --wc-sunk: var(--surface-2);
  --plate-green: var(--presence);
  --plate-yellow: var(--lamp);
  --plate-blue: var(--guest);
  --plate-steel: var(--faint);
  --wc-num: var(--display);
  --wc-fill: var(--lamp);
  --wc-on-fill: #1b1406;
  --wc-sel-bg: var(--lamp-soft);
  --wc-sel-fg: var(--text);
  --wc-sel-line: var(--lamp);
  --wc-link: var(--lamp);
  --wc-pr-bg: rgba(92,200,180,.14);
  --wc-pr-head: var(--presence);
  display: block; color: var(--text); font-family: var(--body);
}
* { box-sizing: border-box; }
ha-card { overflow: hidden; }
.wc { padding: 20px 18px 8px; color: var(--wc-ink); font-size: 15px; line-height: 1.4; }
button { font: inherit; color: inherit; cursor: pointer; -webkit-tap-highlight-color: transparent; }
button:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible {
  outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 2px;
}
.hint { color: var(--wc-mute); margin: 8px 0 16px; }
.empty .big { font: 600 24px/1 var(--wc-num); margin: 0 0 8px; }

/* ---- day header ---- */
.day h1 { margin: 0 0 14px; font: 600 37px/0.95 var(--wc-num); letter-spacing: .005em; }
.day h1 em { font-style: normal; font-weight: 600; color: var(--wc-mute); font-size: .62em; margin-left: .35em; }
.groups { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
.g {
  display: inline-flex; align-items: baseline; gap: 6px;
  padding: 7px 12px; border-radius: 999px; border: 1px solid var(--wc-line); background: none;
}
.g small { color: var(--wc-mute); font-size: 12px; font-variant-numeric: tabular-nums; }
.g.due { border-color: color-mix(in srgb, var(--wc-ink) 55%, transparent); }
.g.on { background: var(--wc-sel-bg); color: var(--wc-sel-fg); border-color: var(--wc-sel-line); }
.g.on small { color: color-mix(in srgb, var(--wc-sel-fg) 70%, transparent); }
.mains, .addons { display: grid; gap: 6px; }
.mains { grid-template-columns: repeat(4, 1fr); }
.addons { grid-template-columns: repeat(3, 1fr); }
.mains .g, .addons .g { flex-direction: column; align-items: center; gap: 0; padding: 8px 2px; font-size: 14px; border-radius: 12px; min-width: 0; }
.mains .g span, .addons .g span { max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
.addons .g { padding: 6px 4px; font-size: 14px; }
.link { background: none; border: 0; padding: 6px 0; color: var(--wc-link); text-align: left; }
.link.quiet { color: var(--wc-mute); }

/* ---- exercise picker ---- */
.exercises { margin: 18px -18px 0; padding: 14px 18px 4px; border-top: 1px solid var(--wc-line); }
.ex-group h3 { margin: 0 0 6px; font-size: 13px; font-weight: 500; color: var(--wc-mute); }
.ex-row { display: flex; gap: 6px; overflow-x: auto; padding-bottom: 12px; scrollbar-width: none; }
.ex-row::-webkit-scrollbar { display: none; }
.exc {
  flex: none; display: inline-flex; align-items: center; gap: 7px;
  padding: 9px 12px; border-radius: 10px; border: 1px solid var(--wc-line); background: none; white-space: nowrap;
}
.exc.on { border-color: var(--wc-sel-line); box-shadow: inset 0 0 0 1px var(--wc-sel-line); background: var(--wc-sunk); font-weight: 600; }
.exc b { font: 600 12px/1 var(--wc-num); background: var(--wc-fill); color: var(--wc-on-fill); border-radius: 999px; padding: 3px 6px 2px; }
.pip { width: 8px; height: 8px; border-radius: 50%; background: var(--plate-steel); flex: none; }
.pip.up { background: var(--plate-green); } .pip.stay { background: var(--plate-yellow); } .pip.down { background: var(--plate-blue); }

/* ---- notes, warm-up, PR, gym ---- */
.note { display: block; width: 100%; text-align: left; margin: -2px 0 12px; padding: 8px 10px; border-radius: 10px;
  border: 0; background: var(--wc-sunk); font-size: 14px; color: var(--wc-ink); white-space: pre-wrap; }
.note.empty { background: none; padding: 0; color: var(--wc-mute); font-size: 13px; }
.note.edit { padding: 0; background: none; }
.note textarea { width: 100%; font: inherit; font-size: 16px; color: var(--wc-ink); background: var(--wc-sunk);
  border: 1px solid var(--wc-line); border-radius: 10px; padding: 8px 10px; resize: vertical; }
.note.edit div { display: flex; gap: 16px; }
.setkind { display: flex; align-items: center; gap: 10px; margin: 12px 0 0; font-size: 12px; color: var(--wc-mute); }
.setkind .chip.on { background: var(--plate-yellow); border-color: var(--plate-yellow); color: #1d1f21; font-weight: 600; }
.plate.warm { border: 2px solid var(--plate-yellow); width: 42px; height: 42px; }
.plate.warm b { font-size: 15px; color: var(--wc-ink); }
.pr { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: -6px -18px 16px;
  padding: 12px 18px; background: var(--wc-pr-bg); border-left: 4px solid var(--plate-green);
  animation: prIn .5s ease-out; }
.pr strong { display: block; font: 600 20px/1 var(--wc-num); margin-bottom: 4px; color: var(--wc-pr-head); }
.pr span { display: block; font-size: 14px; }
.pr small { color: var(--wc-mute); }
@keyframes prIn { from { transform: translateY(-6px); opacity: 0; } to { transform: none; opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .pr { animation: none; } }
.gym { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 10px 0 0; font-size: 13px; }
.gym i { width: 8px; height: 8px; border-radius: 50%; background: var(--plate-green); box-shadow: 0 0 0 3px color-mix(in srgb, var(--plate-green) 25%, transparent); }
.gym.off { color: var(--wc-mute); }
.gym .link { padding: 0; margin-left: auto; font-size: 13px; }

/* ---- log day ---- */
.logday { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px 10px;
  margin: -6px 0 14px; font-size: 13px; color: var(--wc-mute); }
.logday.past { margin: -6px -18px 16px; padding: 12px 18px; background: color-mix(in srgb, var(--plate-yellow) 16%, transparent);
  border-left: 4px solid var(--plate-yellow); color: var(--wc-ink); }
.logday p { margin: 0; display: flex; flex-direction: column; }
.logday p strong { font-size: 15px; }
.logday p span { color: var(--wc-mute); }
.logday-opts { display: flex; gap: 6px; margin-left: auto; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 6px 11px; border-radius: 999px;
  border: 1px solid var(--wc-line); background: none; font-size: 13px; color: var(--wc-ink); position: relative; cursor: pointer; }
.chip.date input { position: absolute; inset: 0; opacity: 0; width: 100%; cursor: pointer; }
.chip.date input::-webkit-calendar-picker-indicator { position: absolute; inset: 0; width: 100%; height: 100%; cursor: pointer; }

/* ---- logging ---- */
.log { margin: 4px -18px 0; padding: 18px 18px 6px; border-top: 1px solid var(--wc-line); }
.log h2 { margin: 0 0 8px; font-size: 20px; font-weight: 600; }
.advice { margin: 0 0 18px; padding-left: 12px; border-left: 4px solid var(--plate-steel); }
.advice.up { border-color: var(--plate-green); } .advice.stay { border-color: var(--plate-yellow); } .advice.down { border-color: var(--plate-blue); }
.advice strong { font-size: 17px; }
.advice .for { color: var(--wc-mute); margin-left: 4px; }
.advice .why { display: block; color: var(--wc-mute); font-size: 14px; margin-top: 2px; }

.steppers { display: grid; grid-template-columns: 1.25fr 1fr; gap: 10px; }
.stepper { display: flex; align-items: stretch; border-radius: 14px; background: var(--wc-sunk); }
.stepper > button { flex: none; width: 44px; border: 0; background: none; font-size: 26px; font-weight: 300; }
.stepper > button:active { background: color-mix(in srgb, var(--wc-ink) 10%, transparent); }
.stepper label { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center; padding: 8px 0 8px; }
.stepper input {
  width: 100%; border: 0; background: none; text-align: center; color: var(--wc-ink);
  font: 600 44px/1 var(--wc-num); font-variant-numeric: tabular-nums; padding: 0;
}
.stepper input:focus { outline: none; }
.stepper label span { font-size: 12px; color: var(--wc-mute); margin-top: 2px; }

.rpe { display: flex; align-items: center; gap: 4px; margin: 14px 0; overflow-x: auto; scrollbar-width: none; }
.rpe::-webkit-scrollbar { display: none; }
.rpe span { color: var(--wc-mute); font-size: 13px; margin-right: 4px; flex: none; }
.rpe button {
  flex: none; min-width: 38px; height: 36px; padding: 0 6px; border-radius: 8px;
  border: 1px solid var(--wc-line); background: none; font: 600 18px/1 var(--wc-num);
}
.rpe button.on { background: var(--wc-fill); color: var(--wc-on-fill); border-color: var(--wc-fill); }

.log-btn {
  display: block; width: 100%; height: 58px; border: 0; border-radius: 14px;
  background: var(--wc-fill); color: var(--wc-on-fill); font: 600 22px/1 var(--wc-num); letter-spacing: .01em;
  transition: background-color .25s;
}
.log-btn:active { transform: translateY(1px); }
.log-btn:disabled { opacity: .6; }
.log-btn.ok { background: var(--plate-green); color: #fff; }

.sets { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; margin: 14px 0 6px; }
.plates { list-style: none; display: flex; gap: 8px; margin: 0; padding: 0; }
.plate {
  width: 50px; height: 50px; border-radius: 50%; border: 2px dashed var(--wc-line);
  display: flex; flex-direction: column; align-items: center; justify-content: center; color: var(--wc-mute);
}
.plate.filled { border: 0; background: var(--wc-fill); color: var(--wc-on-fill);
  box-shadow: inset 0 0 0 4px var(--wc-bg), inset 0 0 0 5px color-mix(in srgb, var(--wc-bg) 35%, transparent); }
.plate b { font: 600 17px/1 var(--wc-num); }
.plate small { font-size: 10px; line-height: 1.1; }
.set-tools { display: flex; flex-direction: column; align-items: flex-end; }

/* ---- history ---- */
.hist { margin: 8px -18px 0; padding: 14px 18px 8px; border-top: 1px solid var(--wc-line); }
.trend { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.trend div { display: flex; flex-direction: column; }
.trend b { font: 600 25px/1 var(--wc-num); }
.trend span { font-size: 13px; color: var(--wc-mute); }
.trend .pos { color: var(--plate-green); } .trend .neg { color: var(--plate-blue); }
.trend svg { color: var(--wc-mute); flex: none; }
.trend svg .last { fill: var(--wc-ink); }
.sessions { list-style: none; margin: 12px 0 4px; padding: 0; }
.sessions li { display: grid; grid-template-columns: 92px 1fr; gap: 8px; padding: 6px 0; font-size: 14px; }
.sessions li + li { border-top: 1px solid var(--wc-line); }
.sessions time { color: var(--wc-mute); font-variant-numeric: tabular-nums; }

/* ---- manage ---- */
.manage { margin: 6px -18px 0; padding: 12px 18px; border-top: 1px solid var(--wc-line); }
.manage summary { color: var(--wc-mute); cursor: pointer; padding: 4px 0; }
.form { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 12px 0 4px; }
.form label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--wc-mute); }
.form .f-name { grid-column: 1 / -1; }
.form input, .form select {
  font: inherit; font-size: 16px; color: var(--wc-ink); background: var(--wc-sunk);
  border: 1px solid transparent; border-radius: 10px; padding: 10px 12px;
}
.add-btn { grid-column: 1 / -1; height: 44px; border-radius: 12px; border: 1px solid var(--wc-ink); background: none; font-weight: 600; }

/* ---- demos ---- */
.exhead { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.exhead h2 { flex: 1; min-width: 0; }
.anim { position: relative; aspect-ratio: 3 / 2; overflow: hidden; border-radius: 10px; background: #fff; }
.anim img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.anim img + img { animation: wcFlip 2s ease-in-out infinite; animation-delay: var(--ph, 0ms); }
@keyframes wcFlip { 0%, 42% { opacity: 0; } 50%, 92% { opacity: 1; } 100% { opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .anim img + img { animation: none; opacity: 0; } }
.demo-thumb { flex: none; width: 84px; padding: 0; border: 1px solid var(--wc-line); border-radius: 11px; background: none; margin-top: -2px; }
.demo-thumb .anim { border-radius: 10px; }
.demo-thumb.open { border-color: var(--wc-sel-line); box-shadow: inset 0 0 0 1px var(--wc-sel-line); }
.demo-thumb.none { height: 56px; border-style: dashed; color: var(--wc-mute); font-size: 12px; }
.demo-panel { margin: 4px 0 14px; }
.demo-panel .anim.big { max-width: 420px; }
.demo-meta { margin: 10px 0 4px; display: flex; flex-direction: column; }
.demo-meta span { color: var(--wc-mute); font-size: 13px; }
.howto { margin: 6px 0 4px; padding-left: 20px; font-size: 14px; }
.howto li + li { margin-top: 4px; }
.demo-tools { display: flex; gap: 16px; flex-wrap: wrap; }
.picker { margin: 4px 0 14px; padding: 12px; border-radius: 14px; background: var(--wc-sunk); }
.picker-head { margin: 0 0 8px; font-size: 14px; }
.picker input { width: 100%; font: inherit; font-size: 16px; color: var(--wc-ink); background: var(--wc-bg);
  border: 1px solid var(--wc-line); border-radius: 10px; padding: 10px 12px; }
.lib-groups { display: flex; gap: 6px; overflow-x: auto; scrollbar-width: none; margin: 8px 0; }
.lib-groups::-webkit-scrollbar { display: none; }
.lib-groups .chip { flex: none; }
.chip.on { background: var(--wc-sel-bg); color: var(--wc-sel-fg); border-color: var(--wc-sel-line); }
.lib-list { list-style: none; margin: 0 0 4px; padding: 0; max-height: 340px; overflow-y: auto; }
.lib-list li + li { border-top: 1px solid var(--wc-line); }
.lib-list button { display: flex; align-items: center; gap: 10px; width: 100%; padding: 7px 2px; border: 0; background: none; text-align: left; }
.lib-list img, .lib-list .noimg { flex: none; width: 60px; height: 40px; object-fit: cover; border-radius: 6px; background: #fff; }
.lib-list b { display: block; font-weight: 500; }
.lib-list small { color: var(--wc-mute); font-size: 12px; }
.lib-list .hint { margin: 8px 0; }
.lib-btn { width: 100%; height: 44px; margin: 10px 0 2px; border-radius: 12px; border: 1px solid var(--wc-line); background: var(--wc-sunk); font-weight: 600; }
.picked { display: flex; align-items: center; gap: 10px; margin: 10px 0 0; font-size: 13px; color: var(--wc-mute); }
.picked .anim.mini { width: 60px; flex: none; border-radius: 6px; }
.picked .link { margin-left: auto; }

/* ---- rest timer ---- */
.rest { margin: 8px 0 0; }
.rest > .link { padding: 4px 0; font-size: 13px; }
.rest-menu { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; margin: 4px 0 6px; }
.rest-opts { display: flex; gap: 6px; flex-wrap: wrap; }
.rest.on { margin-top: 10px; padding: 10px 12px 8px; border-radius: 14px; background: var(--wc-sunk); }
.rest-bar { height: 4px; border-radius: 2px; background: var(--wc-line); overflow: hidden; }
.rest-bar i { display: block; height: 100%; background: var(--wc-ink); transform-origin: left; transition: transform .25s linear; }
.rest-row { display: flex; align-items: center; gap: 6px; margin-top: 8px; }
.rest-time { font: 600 29px/1 var(--wc-num); font-variant-numeric: tabular-nums; min-width: 2.6em; }
.rest-lbl { flex: 1; min-width: 0; color: var(--wc-mute); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rest.on.over { background: var(--wc-pr-bg); }
.rest.on.over .rest-time { color: var(--wc-pr-head); }
.rest.on.over .rest-lbl { color: var(--wc-ink); font-weight: 600; }

@media (max-width: 380px) {
  .steppers { grid-template-columns: 1fr; }
  .day h1 { font-size: 38px; }
}
@media (prefers-reduced-motion: reduce) { .log-btn { transition: none; } }

/* ======== home-dashboard look ======== */
ha-card { background: none; border: 0; box-shadow: none; border-radius: 0; overflow: visible; color: var(--text); }
.wc { padding: 0; container-type: inline-size; font-family: var(--body); }
.cols { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-lg); padding: 20px 18px 10px; min-width: 0; }
@container (min-width: 880px) {
  .cols { grid-template-columns: minmax(320px, .9fr) minmax(380px, 1.1fr); grid-template-areas: "a b" "c b"; grid-template-rows: auto 1fr; align-items: start; }
  .col-a { grid-area: a; } .col-b { grid-area: b; } .col-c { grid-area: c; }
}
.col-b > .log:first-child { margin-top: -20px; border-top: 0; }
.col-b > .hint:first-child { margin: 0 0 10px; }
.col-c { padding-top: 8px; padding-bottom: 8px; }
.col-c > .manage { margin: 0; padding: 0; border-top: 0; }
button:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible { outline: 2px solid var(--lamp); outline-offset: 2px; }

.day h1 { font: 600 38px/1 var(--display); letter-spacing: -.02em; }
.day h1 em { font-family: var(--display); font-weight: 500; }
.g { background: var(--surface-2); border: 1px solid transparent; color: var(--text); }
.mains .g, .addons .g { border-radius: var(--r-md); }
.g small { color: var(--muted); }
.g.due { border-color: #3b4657; }
.g.on { background: var(--lamp-soft); border-color: var(--lamp); color: var(--text); }
.g.on small { color: var(--lamp); }
.g:active, .exc:active, .chip:active, .rpe button:active { transform: scale(.98); }
.link { color: var(--lamp); }
.link.quiet { color: var(--muted); }

.exercises { border-top-color: var(--line); }
.ex-group h3 { color: var(--muted); }
.exc { background: var(--surface-2); border: 1px solid transparent; border-radius: var(--r-sm); min-height: 44px; }
.exc.on { background: var(--lamp-soft); border-color: var(--lamp); box-shadow: none; }
.exc b { background: var(--lamp); color: #1b1406; }

.log { border-top-color: var(--line); }
.log h2 { font-family: var(--display); font-size: 22px; letter-spacing: -.01em; }
.note { background: var(--surface-2); }
.note textarea, .form input, .form select, .picker input {
  background: var(--surface-2); border: 1px solid var(--line); color: var(--text); }
.picker input { background: var(--surface); }
.stepper { background: var(--surface-2); border-radius: var(--r-md); }
.stepper > button { color: var(--muted); }
.stepper input { font: 500 46px/1 var(--display); letter-spacing: -.03em; }
.rpe button { border-color: var(--line); background: var(--surface-2); font: 500 16px/1 var(--display); color: var(--text); }
.rpe button.on { background: var(--lamp); border-color: var(--lamp); color: #1b1406; font-weight: 600; }
.log-btn { background: var(--lamp); color: #1b1406; font: 600 22px/1 var(--display); letter-spacing: -.01em; border-radius: var(--r-md); }
.log-btn.ok { background: var(--presence); color: #0c1d1a; }
.plate { border-color: #3b4657; }
.plate.filled { background: var(--lamp); color: #1b1406; box-shadow: inset 0 0 0 3px var(--surface), inset 0 0 0 4px rgba(242,181,68,.4); }
.plate b { font: 600 17px/1 var(--display); }
.plate.warm { border-color: var(--lamp); }
.setkind .chip.on { background: var(--lamp); border-color: var(--lamp); color: #1b1406; }
.chip { background: var(--surface-2); border-color: transparent; color: var(--text); }
.chip.on { background: var(--lamp-soft); border-color: var(--lamp); color: var(--text); }
.pr { background: rgba(92,200,180,.14); border-left-color: var(--presence); }
.pr strong { font: 600 20px/1 var(--display); color: var(--presence); }
.logday.past { background: var(--lamp-soft); border-left-color: var(--lamp); }

.hist { border-top-color: var(--line); }
.trend b { font: 600 28px/1 var(--display); letter-spacing: -.02em; }
.trend .pos { color: var(--presence); } .trend .neg { color: var(--guest); }
.trend svg { color: var(--lamp); }
.trend svg .last { fill: var(--lamp); }
.sessions li + li { border-top-color: var(--line); }

.manage summary { color: var(--muted); }
.add-btn, .lib-btn { background: var(--surface-2); border: 1px solid var(--line); color: var(--text); border-radius: var(--r-md); }
.demo-thumb { border-color: var(--line); background: var(--surface-2); }
.demo-thumb.open { border-color: var(--lamp); box-shadow: none; }
.picker { background: var(--surface-2); border-radius: var(--r-md); }
.lib-list li + li { border-top-color: var(--line); }
.rest.on { background: var(--surface-2); border-radius: var(--r-md); }
.rest-bar { background: var(--line); }
.rest-bar i { background: var(--lamp); }
.rest-time { font: 600 32px/1 var(--display); letter-spacing: -.02em; }
.rest.on.over { background: rgba(92,200,180,.14); }
.rest.on .chip { background: var(--surface); }
.picker .chip { background: var(--surface); }
.rest.on.over .rest-time { color: var(--presence); }
`;

if (!customElements.get("workout-card")) customElements.define("workout-card", WorkoutCard);
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "workout-card")) window.customCards.push({
  type: "workout-card",
  name: "Workout card",
  description: "Log sets and see progression advice from the pyscript workout tracker.",
});

/* ======================================================================
 * workout-overview-card: weekly overview and progression charts.
 *   type: custom:workout-overview-card
 * Reads pyscript.workout_stats.
 * ==================================================================== */

const SEQ = {
  // one-hue blue ramp, 4 bins; dark mode steps chosen for the dark surface
  light: ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab"],
  dark: ["#104281", "#256abf", "#5598e7", "#9ec5f4"],
};
const SERIES = {
  light: { weight: "#2a78d6", max: "#eb6834" },
  dark: { weight: "#3987e5", max: "#d95926" },
};
// categorical slots in validated order (light / dark steps of the same hues)
const CATS = {
  light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
  dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
};
const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const pd = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const fmtDay = (s) => { const d = pd(s); return `${d.getDate()} ${MONTHS[d.getMonth()]}`; };
const fmtDayLong = (s) => { const d = pd(s); return `${DOW[(d.getDay() + 6) % 7]} ${d.getDate()} ${MONTHS[d.getMonth()]}`; };
const bin = (n) => (n <= 0 ? -1 : n <= 8 ? 0 : n <= 15 ? 1 : n <= 22 ? 2 : 3);

class WorkoutOverviewCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._ex = null;
    loadFont();
    this._ro = new ResizeObserver(() => {
      const w = Math.round(this.getBoundingClientRect().width);
      if (w && w !== this._w) { this._w = w; this._render(); }
    });
    this.shadowRoot.addEventListener("change", (e) => {
      const t = e.target;
      if (t.id === "ex-pick") { this._ex = t.value; this._render(); return; }
      if (t.id === "grp-pick") { this._grp = t.value; this._ex = null; this._render(); return; }
      if (t.dataset.f) this._saveRow(t.closest("tr"));
      if (t.id === "add-ex" && t.value) this._addSet(t.value);
      if (t.id === "b-metric") { this._bodyMetric = t.value; this._bodyMsg = ""; this._render(); }
    });
    this.shadowRoot.addEventListener("click", (e) => this._onAct(e));
    this.shadowRoot.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.dataset?.f) e.target.blur();
    });
    this.shadowRoot.addEventListener("pointerover", (e) => this._tipFor(e));
    this.shadowRoot.addEventListener("pointerdown", (e) => this._tipFor(e));
    this.shadowRoot.addEventListener("pointerout", (e) => {
      const t = e.target.closest?.("[data-tip]");
      if (t && !t.contains(e.relatedTarget)) this._hideTip();
    });
  }

  connectedCallback() {
    this._ro.observe(this);
    this._onStore = () => this._update();
    WT.listeners.add(this._onStore);
  }
  disconnectedCallback() {
    this._ro.disconnect();
    WT.listeners.delete(this._onStore);
  }
  setConfig(c) { this._config = c || {}; }
  getCardSize() { return 16; }
  getGridOptions() { return { columns: "full" }; }

  set hass(hass) {
    this._hass = hass;
    this.toggleAttribute("data-dark", wtIsDark(this, hass));
    WT.ensure(hass);
    this._update();
  }

  _update() {
    const hass = this._hass;
    if (!hass) return;
    const st = wtGet(hass, "stats");
    const dd = wtGet(hass, "day");
    const ex = wtGet(hass, "export");
    const key = (st?.last_updated || "-") + (ex?.last_updated || "-") + (this.hasAttribute("data-dark") ? "d" : "l");
    const dkey = dd?.last_updated || "-";
    if (key !== this._key) {
      this._key = key;
      this._dkey = dkey;
      this._render();
    } else if (dkey !== this._dkey) {
      this._dkey = dkey;
      this._renderCal();
    }
  }

  // ------------------------------------------------------------ tooltip
  _tipFor(e) {
    const t = e.target.closest?.("[data-tip]");
    if (!t) { if (e.type === "pointerdown") this._hideTip(); return; }
    this._showTip(t.dataset.tip, t.getBoundingClientRect());
  }

  _showTip(text, rect) {
    const tip = this.shadowRoot.getElementById("tip");
    const host = this.shadowRoot.querySelector(".ov")?.getBoundingClientRect();
    if (!tip || !host) return;
    tip.innerHTML = text;
    tip.hidden = false;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let x = rect.left + rect.width / 2 - host.left - tw / 2;
    x = Math.max(8, Math.min(x, host.width - tw - 8));
    let y = rect.top - host.top - th - 8;
    if (y < 4) y = rect.bottom - host.top + 8;
    tip.style.transform = `translate(${x}px, ${y}px)`;
  }

  _hideTip() {
    const tip = this.shadowRoot.getElementById("tip");
    if (tip) tip.hidden = true;
  }

  // ------------------------------------------------------------- render
  _render() {
    if (!this._hass) return;
    // Hold the current height during the rebuild: a momentarily empty card
    // would shrink the page and the browser would snap back to the top.
    const h = this.offsetHeight;
    if (h) {
      this.style.minHeight = `${h}px`;
      requestAnimationFrame(() => requestAnimationFrame(() => { this.style.minHeight = ""; }));
    }
    const st = wtGet(this._hass, "stats");
    if (!st) {
      this.shadowRoot.innerHTML = `<style>${CSS}${OV_CSS}</style><ha-card><div class="wc empty">
        <p class="big">No stats yet</p><p>Update <code>workout.py</code> and reload it, then log a set.</p></div></ha-card>`;
      return;
    }
    this._dark = this.hasAttribute("data-dark");
    const a = st.attributes;
    // every section is its own card: 18px padding each side, 16px between cards
    const W = Math.max(260, (this._w || 400) - 38);
    const wide = W > 720;
    const colW = wide ? (W - 54) / 2 : W;

    this.shadowRoot.innerHTML = `<style>${CSS}${OV_CSS}</style>
      <ha-card><div class="wc ov ${wide ? "wide" : ""}">
        ${this._week(a)}
        <div class="grid">
          ${this._progress(a, W)}
          ${this._weekly(a, colW)}
          ${this._calendar(a, colW)}
          ${this._gymTime(a, colW)}
          ${this._balance(a)}
          ${this._body(a, colW)}
          ${this._bests(a)}
          ${this._calendarMonth(a)}
          ${this._export()}
        </div>
        <div id="tip" class="tip" role="tooltip" hidden></div>
      </div></ha-card>`;
    this._wireLine();
    if (this._scrollToDay && this._dayData()) {
      this._scrollToDay = false;
      this.shadowRoot.getElementById("dayd")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  // Calendar + day editing --------------------------------------------
  _renderCal() {
    const old = this.shadowRoot.querySelector(".calm");
    const st = wtGet(this._hass, "stats");
    if (!old || !st) { this._render(); return; }
    // keep whatever field has focus (e.g. the reps box you just tabbed into)
    const act = this.shadowRoot.activeElement;
    const focusSel = act?.dataset?.f && act.closest("tr") ? `tr[data-id="${act.closest("tr").dataset.id}"] [data-f="${act.dataset.f}"]` : null;
    const tpl = document.createElement("template");
    tpl.innerHTML = this._calendarMonth(st.attributes);
    old.replaceWith(tpl.content.firstElementChild);
    if (focusSel) this.shadowRoot.querySelector(focusSel)?.focus({ preventScroll: true });
    if (this._scrollToDay && this._dayData()) {
      this._scrollToDay = false;
      this.shadowRoot.getElementById("dayd")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  _openDay(day) {
    this._day = day;
    this._dayErr = "";
    this._scrollToDay = true;
    this._renderCal();
    wtCall(this._hass, "workout_open_day", { day }).catch((err) => {
      this._dayErr = `Couldn't open this day: ${err.message || err}`;
      this._renderCal();
    });
  }

  _closeDay() {
    this._day = null;
    this._renderCal();
    wtCall(this._hass, "workout_open_day", { day: "" }).catch(() => {});
  }

  _dayData() {
    const d = wtGet(this._hass, "day");
    return d && d.state === this._day ? d.attributes : null;
  }

  _flashDay(msg) {
    this._dayMsg = msg;
    clearTimeout(this._dayT);
    this._dayT = setTimeout(() => { this._dayMsg = ""; this._renderCal(); }, 1600);
  }

  async _mutate(service, data, msg) {
    try {
      this._dayErr = "";
      await wtCall(this._hass, service, data);
      this._flashDay(msg);
      this._renderCal();
    } catch (err) {
      this._dayErr = `Couldn't save: ${err.message || err}`;
      this._renderCal();
    }
  }

  _saveRow(tr) {
    if (!tr) return;
    const val = (f) => tr.querySelector(`[data-f="${f}"]`).value.replace(",", ".");
    const weight = Number(val("weight")), reps = Math.round(Number(val("reps"))), rpe = Number(val("rpe") || 0);
    if (isNaN(weight) || weight < 0 || !reps || reps < 1) { this._dayErr = "Weight must be 0 or more and reps at least 1."; this._renderCal(); return; }
    this._dayErr = "";
    this._mutate("workout_update_set", { set_id: Number(tr.dataset.id), weight, reps, rpe }, "Saved");
  }

  _addSet(exercise) {
    this._mutate("workout_add_set", { day: this._day, exercise }, `Added a set of ${exercise}`);
  }

  _onAct(e) {
    const b = e.target.closest("[data-act]");
    if (!b || b.disabled) return;
    const act = b.dataset.act;
    if (act === "prev" || act === "next") {
      const [y, m] = this._monthKey().split("-").map(Number);
      const d = new Date(y, m - 1 + (act === "next" ? 1 : -1), 1);
      this._month = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
      this._renderCal();
    } else if (act === "day") {
      if (this._day === b.dataset.d) this._closeDay(); else this._openDay(b.dataset.d);
    } else if (act === "close") {
      this._closeDay();
    } else if (act === "del") {
      if (b.dataset.armed) this._mutate("workout_delete_set", { set_id: Number(b.dataset.id) }, "Set removed");
      else {
        b.dataset.armed = "1"; b.textContent = "Remove?"; b.classList.add("armed");
        setTimeout(() => { if (b.isConnected) { delete b.dataset.armed; b.textContent = "\u2715"; b.classList.remove("armed"); } }, 3000);
      }
    } else if (act === "addset") {
      this._addSet(b.dataset.ex);
    } else if (act === "pick-ex") {
      this._ex = b.dataset.name;
      this._render();
    } else if (act === "wu") {
      this._mutate("workout_update_set", { set_id: Number(b.dataset.id), warmup: b.dataset.on !== "1" },
        b.dataset.on === "1" ? "Marked as working set" : "Marked as warm-up");
    } else if (act === "body-add") {
      const metric = this.shadowRoot.getElementById("b-metric").value;
      const raw = this.shadowRoot.getElementById("b-value").value.replace(",", ".");
      const value = Number(raw);
      if (!raw || isNaN(value) || value <= 0) { this._bodyMsg = "Enter a number first."; this._render(); return; }
      this._bodyMetric = metric === "height" ? null : metric;
      if (metric === "height") this._heightEdit = false;
      wtCall(this._hass, "workout_log_body", { metric, value })
        .then(() => { this._bodyMsg = "Saved"; this._render(); setTimeout(() => { this._bodyMsg = ""; this._render(); }, 2000); })
        .catch((err) => { this._bodyMsg = `Couldn't save: ${err.message || err}`; this._render(); });
    } else if (act === "height-edit") {
      this._heightEdit = true;
      this._bodyMetric = "height";
      this._render();
      this.shadowRoot.getElementById("b-value")?.focus();
    } else if (act === "body-del") {
      if (b.dataset.armed) wtCall(this._hass, "workout_delete_body", { entry_id: Number(b.dataset.id) });
      else {
        b.dataset.armed = "1"; b.textContent = "Remove?"; b.classList.add("armed");
        setTimeout(() => { if (b.isConnected) { delete b.dataset.armed; b.textContent = "\u2715"; b.classList.remove("armed"); } }, 3000);
      }
    } else if (act === "export") {
      this._exporting = true;
      this._render();
      wtCall(this._hass, "workout_export").finally(() => { this._exporting = false; this._render(); });
    }
  }

  _monthKey() {
    if (!this._month) this._month = localISO(0).slice(0, 7);
    return this._month;
  }

  _calendarMonth(a) {
    const key = this._monthKey();
    const [y, m] = key.split("-").map(Number);
    const days = new Map((a.days || []).map((d) => [d[0], d]));
    const first = new Date(y, m - 1, 1);
    const nDays = new Date(y, m, 0).getDate();
    const lead = (first.getDay() + 6) % 7;
    const today = a.today || localISO(0);
    const earliest = (a.days && a.days[0] && a.days[0][0].slice(0, 7)) || key;
    let workouts = 0, sets = 0;
    const cells = [];
    for (let i = 0; i < lead; i++) cells.push(`<span class="mday blank" aria-hidden="true"></span>`);
    for (let d = 1; d <= nDays; d++) {
      const iso = `${key}-${String(d).padStart(2, "0")}`;
      const info = days.get(iso);
      const future = iso > today;
      if (info) { workouts++; sets += info[1]; }
      const label = `${fmtDayLong(iso)}${info ? `, ${info[1]} sets: ${info[2].map(title).join(", ")}` : future ? "" : ", rest day"}`;
      cells.push(`<button class="mday ${info ? "on" : ""} ${iso === today ? "today" : ""} ${iso === this._day ? "sel" : ""}"
        data-act="day" data-d="${iso}" ${future ? "disabled" : ""} aria-label="${esc(label)}" aria-pressed="${iso === this._day}">
        <span class="dn">${d}</span>
        <span class="pl">${info ? info[1] : ""}</span>
      </button>`);
    }
    const monthName = first.toLocaleDateString(undefined, { month: "long", year: "numeric" });
    return `<section class="panel span calm">
      <div class="phead">
        <h2>Calendar</h2>
        <div class="mnav">
          <button data-act="prev" aria-label="Previous month" ${key <= earliest ? "disabled" : ""}>\u2039</button>
          <span>${esc(monthName)}</span>
          <button data-act="next" aria-label="Next month" ${key >= today.slice(0, 7) ? "disabled" : ""}>\u203a</button>
        </div>
      </div>
      <p class="sub">${workouts ? `${workouts} workout${workouts === 1 ? "" : "s"} and ${sets} sets this month. Tap a day to see or change it.` : "No workouts logged this month. Tap a day to add one."}</p>
      <div class="calbody ${this._day ? "open" : ""}">
        <div class="mgrid">${DOW.map((d) => `<span class="mh">${d.slice(0, 2)}</span>`).join("")}${cells.join("")}</div>
        ${this._day ? this._dayPanel() : ""}
      </div>
    </section>`;
  }

  _dayPanel() {
    const d = this._dayData();
    const head = `<div class="dhead"><h3>${esc(fmtDayLong(this._day))}</h3>
      <span class="dmsg" role="status">${esc(this._dayMsg || "")}</span>
      <button class="link quiet" data-act="close">Close</button></div>`;
    if (this._dayErr && !d) return `<div class="dayd" id="dayd">${head}<p class="err">${esc(this._dayErr)}</p></div>`;
    if (!d) return `<div class="dayd" id="dayd">${head}<p class="hint">Loading\u2026</p></div>`;
    const rpeOpts = (v) => `<option value="0" ${!v ? "selected" : ""}>\u2013</option>` +
      [6, 6.5, 7, 7.5, 8, 8.5, 9, 9.5, 10].map((r) => `<option value="${r}" ${Number(v) === r ? "selected" : ""}>${r}</option>`).join("");
    const blocks = d.exercises.map((ex) => `<div class="dex">
      <h4>${esc(ex.name)} <small>${esc(title(ex.group))}</small></h4>
      <table class="dsets"><thead><tr><th scope="col">Set</th><th scope="col">Weight</th><th scope="col">Reps</th><th scope="col">RPE</th><th scope="col"><span class="sr">Type</span></th><th></th></tr></thead><tbody>
      ${ex.sets.map((s, i) => `<tr data-id="${s.id}">
        <td class="sn">${i + 1}</td>
        <td><label class="unit"><input data-f="weight" inputmode="decimal" value="${num(s.weight)}" aria-label="Set ${i + 1} weight in kg"><span>kg</span></label></td>
        <td><input data-f="reps" inputmode="numeric" value="${s.reps}" aria-label="Set ${i + 1} reps"></td>
        <td><select data-f="rpe" aria-label="Set ${i + 1} RPE">${rpeOpts(s.rpe)}</select></td>
        <td><button class="wtog ${s.kind === "warmup" ? "on" : ""}" data-act="wu" data-id="${s.id}" data-on="${s.kind === "warmup" ? 1 : 0}"
          aria-pressed="${s.kind === "warmup"}" title="Warm-up set">W</button></td>
        <td><button class="del" data-act="del" data-id="${s.id}" aria-label="Remove set ${i + 1}">\u2715</button></td>
      </tr>`).join("")}
      </tbody></table>
      <button class="link" data-act="addset" data-ex="${esc(ex.name)}">+ Add a set</button>
    </div>`).join("");
    const catalog = (d.catalog || []).map((c) => `<optgroup label="${esc(title(c.group))}">${c.exercises.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("")}</optgroup>`).join("");
    const visit = (d.visit || []).map((v) => v.end ? `at the gym ${v.start}\u2013${v.end} (${v.minutes} min)` : `at the gym since ${v.start}`).join(", ");
    const sub = d.sets || d.warmups
      ? `${d.groups.map(title).join(" + ")}: ${d.sets} working set${d.sets === 1 ? "" : "s"}${d.warmups ? ` + ${d.warmups} warm-up${d.warmups === 1 ? "" : "s"}` : ""}${visit ? `, ${visit}` : ""}. Changes save when you leave a field; W marks a warm-up.`
      : "Nothing logged on this day. Add the exercises you did below.";
    return `<div class="dayd" id="dayd">
      ${head}
      <p class="sub">${esc(sub)}</p>
      ${this._dayErr ? `<p class="err">${esc(this._dayErr)}</p>` : ""}
      ${blocks}
      <label class="addex"><span>Add an exercise</span>
        <select id="add-ex"><option value="">Choose\u2026</option>${catalog}</select></label>
    </div>`;
  }

  // Time in the gym ----------------------------------------------------
  _gymTime(a, W) {
    const g = a.gym;
    if (!g) return "";
    const weeks = g.weeks || [];
    const fmt = (m) => (m >= 60 ? `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ""}` : `${m} min`);
    const any = weeks.some((w) => w.minutes);
    if (!any) {
      return `<section class="panel"><h2>Time in the gym</h2>
        <p class="hint">Nothing yet. It fills in from gym arrivals and departures, or from when you log your first and last set.</p></section>`;
    }
    const H = 150, padL = 30, padB = 22, padT = 18;
    const maxH = Math.max(1, ...weeks.map((w) => w.minutes / 60));
    const ticks = niceTicks(0, maxH, 3);
    const top = ticks[ticks.length - 1];
    const bw = (W - padL) / weeks.length;
    const barW = Math.max(6, Math.min(28, bw - 4));
    const y = (v) => padT + (H - padT - padB) * (1 - v / top);
    const col = SERIES[this._dark ? "dark" : "light"].weight;
    const bars = weeks.map((w, i) => {
      const cx = padL + bw * i + bw / 2;
      const hrs = w.minutes / 60;
      const h = Math.max(0, y(0) - y(hrs));
      const r = Math.min(4, barW / 2, h);
      const est = w.estimated_minutes ? `<br><span class="mute">${fmt(w.estimated_minutes)} estimated from logged sets</span>` : "";
      const tip = `<b>Week of ${fmtDay(w.start)}</b><br>${w.minutes ? `${fmt(w.minutes)} over ${w.visits} visit${w.visits === 1 ? "" : "s"}` : "No gym time"}${est}`;
      const bar = w.minutes ? `<path d="M${cx - barW / 2},${y(0)} V${y(hrs) + r} q0,-${r} ${r},-${r} H${cx + barW / 2 - r} q${r},0 ${r},${r} V${y(0)} Z" fill="${col}" class="${i === weeks.length - 1 ? "" : "past"}"/>` : "";
      return `<g data-tip="${esc(tip)}"><rect x="${cx - bw / 2}" y="${padT}" width="${bw}" height="${H - padT - padB}" fill="transparent"/>${bar}</g>`;
    }).join("");
    const grid = ticks.map((t) => `<line x1="${padL}" x2="${W}" y1="${y(t)}" y2="${y(t)}" class="gl"/><text x="${padL - 6}" y="${y(t) + 4}" class="ax" text-anchor="end">${t}h</text>`).join("");
    const xl = `<text x="${padL + bw / 2}" y="${H - 5}" class="ax" text-anchor="start">${fmtDay(weeks[0].start)}</text><text x="${W}" y="${H - 5}" class="ax" text-anchor="end">This week</text>`;
    const avg = g.avg_week_minutes;
    return `<section class="panel">
      <h2>Time in the gym</h2>
      <p class="stat"><b>${avg != null ? fmt(avg) : fmt(g.this_week_minutes)}</b><span>${avg != null ? `a week on average (last ${g.avg_weeks_counted} full week${g.avg_weeks_counted === 1 ? "" : "s"})` : "so far this week"}</span></p>
      <p class="sub">${g.avg_session_minutes ? `About ${fmt(g.avg_session_minutes)} per visit. ` : ""}This week: ${fmt(g.this_week_minutes)}.</p>
      <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Hours in the gym per week">${grid}${bars}${xl}</svg>
    </section>`;
  }

  // Body weight and measurements ----------------------------------------
  _body(a, W) {
    const b = a.body;
    if (!b) return "";
    // The server's core predates height/BMI: say so instead of failing silently.
    const outdated = Array.isArray(b.metrics) && !b.metrics.some((m) => m.key === "height");
    this._bodyOutdated = outdated;
    // Every measurement is always offered, even if the server data predates it.
    const KNOWN = [["weight", "Body weight", "kg"], ["bodyfat", "Body fat", "%"], ["waist", "Waist", "cm"],
      ["chest", "Chest", "cm"], ["arms", "Arms", "cm"], ["thighs", "Thighs", "cm"], ["hips", "Hips", "cm"],
      ["height", "Height", "cm"]];
    const metrics = (b.metrics || []).slice();
    KNOWN.forEach(([key, label, unit]) => {
      if (!metrics.some((m) => m.key === key)) metrics.push({ key, label, unit, latest: null });
    });
    const weight = metrics.find((m) => m.key === "weight");
    let chart = "";
    if (weight && weight.series && weight.series.length >= 2) {
      const pts = weight.series;
      const H = 120, padL = 34, padR = 44, padT = 10, padB = 20;
      const vals = pts.map((p) => p[1]);
      let lo = Math.min(...vals), hi = Math.max(...vals);
      const span = Math.max(hi - lo, 1);
      lo -= span * 0.2; hi += span * 0.2;
      const t0 = pd(pts[0][0]).getTime(), t1 = pd(pts[pts.length - 1][0]).getTime();
      const x = (d) => padL + ((pd(d).getTime() - t0) / Math.max(t1 - t0, 1)) * (W - padL - padR);
      const yv = (v) => padT + (H - padT - padB) * (1 - (v - lo) / (hi - lo));
      const col = SERIES[this._dark ? "dark" : "light"].weight;
      const path = pts.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${yv(p[1]).toFixed(1)}`).join("");
      const ticks = niceTicks(lo, hi, 3).filter((t) => t >= lo && t <= hi);
      const last = pts[pts.length - 1];
      chart = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Body weight over time">
        ${ticks.map((t) => `<line x1="${padL}" x2="${W - padR}" y1="${yv(t)}" y2="${yv(t)}" class="gl"/><text x="${padL - 6}" y="${yv(t) + 4}" class="ax" text-anchor="end">${num(t)}</text>`).join("")}
        <path d="${path}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round"/>
        ${pts.map((p) => `<circle cx="${x(p[0])}" cy="${yv(p[1])}" r="3" fill="${col}" class="pt" data-tip="${esc(`<b>${fmtDayLong(p[0])}</b><br>${num(p[1])} kg`)}"/>`).join("")}
        <text x="${x(last[0]) + 8}" y="${yv(last[1]) + 4}" class="endl">${num(last[1])} kg</text>
        <text x="${padL}" y="${H - 4}" class="ax">${fmtDay(pts[0][0])}</text>
        <text x="${W - padR}" y="${H - 4}" class="ax" text-anchor="end">${fmtDay(last[0])}</text>
      </svg>`;
    }
    const hasHeight = metrics.some((m) => m.key === "height" && m.latest != null);
    const heightM = metrics.find((m) => m.key === "height");
    const heightCm = heightM && heightM.latest != null ? heightM.latest : null;
    // BMI over time: every weigh-in divided by height squared
    const bmiSpark = () => {
      const wser = (weight && weight.series) || [];
      if (!heightCm || wser.length < 2) return "";
      const h2 = (heightCm / 100) ** 2;
      const vals = wser.map((p) => p[1] / h2);
      const lo = Math.min(...vals), hi = Math.max(...vals), SW = 72, SH = 26, pad = 3;
      const xs = (i) => pad + (i * (SW - 2 * pad)) / (vals.length - 1);
      const ys = (v) => (hi === lo ? SH / 2 : SH - pad - ((v - lo) * (SH - 2 * pad)) / (hi - lo));
      const pts = vals.map((v, i) => `${xs(i).toFixed(1)},${ys(v).toFixed(1)}`).join(" ");
      const label = `BMI from ${num(Math.round(vals[0] * 10) / 10)} on ${fmtDay(wser[0][0])} to ${num(Math.round(vals[vals.length - 1] * 10) / 10)}`;
      return `<svg class="spark" width="${SW}" height="${SH}" viewBox="0 0 ${SW} ${SH}" role="img" aria-label="${esc(label)}"
          data-tip="${esc(`<b>BMI over time</b><br>${label}`)}">
        <polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>
        <circle cx="${xs(vals.length - 1)}" cy="${ys(vals[vals.length - 1])}" r="2.6" class="last"/></svg>`;
    };
    const derived = (b.derived || []).map((d) => {
      const ch = d.change ? `, ${d.change > 0 ? "+" : ""}${num(d.change)} since ${fmtDay(d.change_since)}` : "";
      const spark = d.key === "bmi" ? bmiSpark() : "";
      return `<li${spark ? ' class="has-spark"' : ""}><span>${esc(d.label)}</span>
        <b>${num(d.value)}${d.unit ? `<i>${esc(d.unit)}</i>` : ""}${spark}</b><small>${esc(d.note)}${ch}</small></li>`;
    }).join("");
    const heightLine = heightCm != null
      ? `<p class="hline">Height ${num(heightCm)} cm <button class="link quiet" data-act="height-edit">Change</button></p>`
      : "";
    const derivedBlock = derived
      ? `<ul class="btiles derived">${derived}</ul>${heightLine}`
      : metrics.some((m) => m.key === "weight" && m.latest != null) && !hasHeight
        ? `<p class="hint">Add your height (Measurement \u2192 Height) to see your BMI.</p>`
        : heightLine;
    const tiles = metrics.filter((m) => m.latest != null && m.key !== "height").map((m) => {
      const ch = m.change;
      const chTxt = ch == null || ch === 0 ? "" : `<small>${ch > 0 ? "+" : ""}${num(ch)} since ${fmtDay(m.change_since)}</small>`;
      return `<li><span>${esc(m.label)}</span><b>${num(m.latest)}<i>${esc(m.unit)}</i></b>${chTxt}</li>`;
    }).join("");
    // Height is asked for once: hide it from the list when known (unless "Change" was tapped)
    const listed = metrics.filter((m) => m.key !== "height" || heightCm == null || this._heightEdit);
    if (this._bodyMetric && !listed.some((m) => m.key === this._bodyMetric)) this._bodyMetric = null;
    const sel = this._bodyMetric || "weight";
    const opts = listed.map((m) => `<option value="${m.key}" ${m.key === sel ? "selected" : ""}>${esc(m.key === "weight" ? "Weight" : m.label)}</option>`).join("");
    const unit = (metrics.find((m) => m.key === sel) || {}).unit || "";
    const recent = (b.recent || []).slice(0, 6).map((r) => {
      const m = metrics.find((x) => x.key === r.metric) || { label: r.metric, unit: "" };
      return `<li><time>${fmtDay(r.day)}</time><span>${esc(m.label)}</span><b>${num(r.value)} ${esc(m.unit)}</b>
        <button class="del" data-act="body-del" data-id="${r.id}" aria-label="Remove ${esc(m.label)} on ${fmtDay(r.day)}">\u2715</button></li>`;
    }).join("");
    return `<section class="panel">
      <h2>Body</h2>
      ${tiles ? `<ul class="btiles">${tiles}</ul>` : `<p class="hint">Log your body weight and measurements to see how they change alongside your lifts.</p>`}
      ${this._bodyOutdated ? `<p class="warn">Your tracker is still running an older <code>workout_core.py</code>, so height and BMI can't be saved yet. Replace <code>/config/pyscript_modules/workout_core.py</code> and restart Home Assistant.</p>` : derivedBlock}
      ${chart}
      <div class="bform">
        <label>Measurement<select id="b-metric">${opts}</select></label>
        <label>Today's value<span class="unit"><input id="b-value" inputmode="decimal" placeholder="0"><span>${esc(unit)}</span></span></label>
        <button class="add-btn" data-act="body-add">Save</button>
      </div>
      ${this._bodyMsg ? `<p class="hint">${esc(this._bodyMsg)}</p>` : ""}
      ${recent ? `<details class="numbers"><summary>Recent entries</summary><ul class="brecent">${recent}</ul></details>` : ""}
    </section>`;
  }

  // Export -----------------------------------------------------------
  _export() {
    const ex = wtGet(this._hass, "export");
    const files = ex?.attributes?.files || [];
    const when = ex && ex.state && ex.state !== "unknown" ? new Date(ex.state) : null;
    const labels = { sets: "All sets", body: "Body measurements", "gym-visits": "Gym visits" };
    return `<section class="panel span exp">
      <div class="phead"><h2>Export</h2>
        <button class="add-btn small" data-act="export" ${this._exporting ? "disabled" : ""}>${this._exporting ? "Exporting\u2026" : files.length ? "Export again" : "Export to CSV"}</button></div>
      <p class="sub">CSV files you can open in Excel, Numbers or Google Sheets.${when ? ` Last export ${when.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}.` : ""}</p>
      ${files.length ? `<ul class="files">${files.map((f) => `<li><a href="${esc(f.url)}" download target="_blank" rel="noopener">${esc(labels[f.name] || f.name)}</a><small>${f.rows} rows</small></li>`).join("")}</ul>` : ""}
    </section>`;
  }

  // This week ---------------------------------------------------------
  _week(a) {
    const done = a.workouts_this_week || 0;
    const target = a.target_days || 5;
    const tiles = (a.this_week || []).map((d, i) => {
      const isToday = d.day === a.today;
      const cls = d.sets ? "on" : d.future ? "future" : "";
      const tip = d.sets ? `<b>${fmtDayLong(d.day)}</b><br>${d.sets} sets: ${d.groups.map(title).join(", ")}` : `<b>${fmtDayLong(d.day)}</b><br>${d.future ? "Coming up" : "Rest day"}`;
      return `<li class="${cls} ${isToday ? "today" : ""}" data-tip="${esc(tip)}">
        <span class="dow">${DOW[i]}</span>
        <span class="dot">${d.sets ? d.sets : ""}</span>
        <span class="gs">${d.groups.slice(0, 3).map((g) => esc(title(g))).join("<br>")}</span>
      </li>`;
    }).join("");
    const left = Math.max(target - done, 0);
    return `<header class="week">
      <h1><b>${done}</b><span> of ${target} workouts this week</span></h1>
      <p class="sub">${left ? `${left} to go` : "Weekly target reached"} \u00b7 ${a.total_workouts} workouts logged in total</p>
      <ol class="days">${tiles}</ol>
    </header>`;
  }

  // Weight progression --------------------------------------------------
  _progress(a, W) {
    const exs = (a.exercises || []).filter((e) => e.points.length);
    if (!exs.length) return `<section class="panel span"><h2>Weight progression</h2><p class="hint">Log a few sessions and your progression shows up here.</p></section>`;
    const order = [...(a.balance || []).map((b) => b.group)];
    exs.forEach((e) => { if (!order.includes(e.group)) order.push(e.group); });
    const groups = order.filter((g) => exs.some((e) => e.group === g));
    if (!this._grp || (this._grp !== "all" && !groups.includes(this._grp))) this._grp = "all";
    const inGroup = this._grp === "all" ? exs : exs.filter((e) => e.group === this._grp);
    if (!this._ex || (this._ex !== "all" && !inGroup.find((e) => e.name === this._ex))) {
      this._ex = this._grp === "all" ? inGroup[0].name : "all";
    }
    const filters = `<div class="filters">
        <label>Muscle group<select id="grp-pick">
          <option value="all" ${this._grp === "all" ? "selected" : ""}>All groups</option>
          ${groups.map((g) => `<option value="${esc(g)}" ${g === this._grp ? "selected" : ""}>${esc(title(g))}</option>`).join("")}
        </select></label>
        <label>Exercise<select id="ex-pick">
          <option value="all" ${this._ex === "all" ? "selected" : ""}>All exercises (compare)</option>
          ${inGroup.map((e) => `<option value="${esc(e.name)}" ${e.name === this._ex ? "selected" : ""}>${esc(e.name)}</option>`).join("")}
        </select></label>
      </div>`;
    const body = this._ex === "all" ? this._progressMulti(inGroup, W) : this._progressOne(inGroup.find((e) => e.name === this._ex), W);
    return `<section class="panel span">
      <h2>Weight progression</h2>
      ${filters}
      ${body}
    </section>`;
  }

  _progressOne(ex, W) {
    const pts = ex.points;
    const bw = pts.every((p) => !p.top);
    const col = SERIES[this._dark ? "dark" : "light"];
    const series = bw
      ? [{ label: "Reps (bodyweight)", color: col.weight, vals: pts.map((p) => p.reps), unit: " reps" }]
      : [
          { label: "Working weight", color: col.weight, vals: pts.map((p) => p.top), unit: " kg" },
          { label: "Estimated max", color: col.max, vals: pts.map((p) => p.strength), unit: " kg" },
        ];
    const H = 220, padL = 40, padR = 52, padT = 12, padB = 26;
    const all = series.flatMap((s) => s.vals);
    let lo = Math.min(...all), hi = Math.max(...all);
    const span = hi - lo || Math.max(hi * 0.2, 5);
    lo = Math.max(0, lo - span * 0.15); hi = hi + span * 0.15;
    const ticks = niceTicks(lo, hi, 4);
    lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
    const n = pts.length;
    const x = (i) => (n === 1 ? padL + (W - padL - padR) / 2 : padL + (i * (W - padL - padR)) / (n - 1));
    const y = (v) => padT + (H - padT - padB) * (1 - (v - lo) / (hi - lo));
    this._line = {
      H, padB,
      hits: pts.map((p, i) => ({
        x: x(i),
        top: y(Math.max(...series.map((s) => s.vals[i]))),
        html: `<b>${fmtDayLong(p.day)}</b><br>` +
          series.map((s) => `<span class="sw" style="background:${s.color}"></span>${s.label}: <b>${num(s.vals[i])}${s.unit}</b>`).join("<br>") +
          (bw ? "" : `<br><span class="mute">Top set ${num(p.top)} kg \u00d7 ${p.reps}, ${p.sets} sets</span>`),
      })),
    };
    const grid = ticks.map((t) => `<line x1="${padL}" x2="${W - padR}" y1="${y(t)}" y2="${y(t)}" class="gl"/>
      <text x="${padL - 8}" y="${y(t) + 4}" class="ax" text-anchor="end">${num(t)}</text>`).join("");
    const xl = n > 1
      ? `<text x="${x(0)}" y="${H - 6}" class="ax" text-anchor="start">${fmtDay(pts[0].day)}</text>
         <text x="${x(n - 1)}" y="${H - 6}" class="ax" text-anchor="end">${fmtDay(pts[n - 1].day)}</text>`
      : `<text x="${x(0)}" y="${H - 6}" class="ax" text-anchor="middle">${fmtDay(pts[0].day)}</text>`;
    const lines = series.map((s) => {
      const path = s.vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
      const lv = s.vals[n - 1];
      return `<path d="${path}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
        ${n <= 12 ? s.vals.map((v, i) => `<circle cx="${x(i)}" cy="${y(v)}" r="3" fill="${s.color}" class="pt"/>`).join("") : ""}
        <circle cx="${x(n - 1)}" cy="${y(lv)}" r="4.5" fill="${s.color}" class="pt"/>
        <text x="${x(n - 1) + 8}" y="${y(lv) + 4}" class="endl">${num(lv)}${s.unit.trim() === "kg" ? " kg" : ""}</text>`;
    }).join("");
    const first = series[0].vals[0], last = series[0].vals[n - 1];
    const delta = last - first;
    const what = bw ? "Reps" : "Working weight";
    const summary = n <= 1 ? "One session so far"
      : delta === 0 ? `${what} unchanged over ${n} sessions`
      : `${what} ${delta > 0 ? "up" : "down"} ${num(Math.abs(delta))}${series[0].unit} over ${n} sessions`;
    const table = pts.slice().reverse().map((p) => `<tr><td>${fmtDayLong(p.day)}</td><td>${bw ? "bw" : num(p.top) + " kg"} \u00d7 ${p.reps}</td><td>${p.sets}</td><td>${bw ? "\u2013" : num(p.strength) + " kg"}</td></tr>`).join("");
    return `<p class="sub">${esc(ex.name)}: ${summary}</p>
      ${series.length > 1 ? `<ul class="legend">${series.map((s) => `<li><i style="background:${s.color}"></i>${s.label}</li>`).join("")}</ul>` : ""}
      ${this._lineSvg(W, H, padT, padB, grid + xl, lines, `${ex.name}: ${summary}`)}
      <details class="numbers"><summary>Show the numbers</summary>
        <table><thead><tr><th>Session</th><th>Top set</th><th>Sets</th><th>Est. max</th></tr></thead><tbody>${table}</tbody></table>
      </details>`;
  }

  _progressMulti(list, W) {
    const MAX = 8;
    let shown = list;
    if (list.length > MAX) shown = list.slice().sort((p, q) => q.sessions - p.sessions).slice(0, MAX);
    // stable order (and so stable colours) as you keep training
    shown = shown.slice().sort((p, q) => p.name.localeCompare(q.name));
    const pal = CATS[this._dark ? "dark" : "light"];
    // % change since each exercise's first session: estimated max, or reps for bodyweight
    const series = shown.map((e, k) => {
      const bw = e.points.every((p) => !p.top);
      const val = (p) => (bw ? p.reps : p.strength);
      const base = val(e.points[0]) || 1;
      return {
        name: e.name, bw, color: pal[k % pal.length],
        pts: e.points.map((p) => ({ day: p.day, v: (val(p) / base - 1) * 100, raw: p })),
      };
    });
    const days = [...new Set(series.flatMap((s) => s.pts.map((p) => p.day)))].sort();
    if (!days.length) return `<p class="hint">No sessions yet in this group.</p>`;
    const H = 240, padL = 44, padR = 16, padT = 12, padB = 26;
    const t0 = pd(days[0]).getTime(), t1 = pd(days[days.length - 1]).getTime();
    const x = (d) => (t1 === t0 ? padL + (W - padL - padR) / 2 : padL + ((pd(d).getTime() - t0) / (t1 - t0)) * (W - padL - padR));
    const vals = series.flatMap((s) => s.pts.map((p) => p.v));
    let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
    if (hi - lo < 10) { hi += 5; lo -= 5; }
    const ticks = niceTicks(lo, hi, 4);
    lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
    const y = (v) => padT + (H - padT - padB) * (1 - (v - lo) / (hi - lo));
    const pct = (v) => `${v > 0 ? "+" : ""}${Math.round(v)}%`;
    const grid = ticks.map((t) => `<line x1="${padL}" x2="${W - padR}" y1="${y(t)}" y2="${y(t)}" class="gl ${t === 0 ? "zero" : ""}"/>
      <text x="${padL - 8}" y="${y(t) + 4}" class="ax" text-anchor="end">${pct(t)}</text>`).join("");
    const xl = `<text x="${padL}" y="${H - 6}" class="ax" text-anchor="start">${fmtDay(days[0])}</text>
      <text x="${W - padR}" y="${H - 6}" class="ax" text-anchor="end">${fmtDay(days[days.length - 1])}</text>`;
    const lines = series.map((s) => {
      const path = s.pts.map((p, i) => `${i ? "L" : "M"}${x(p.day).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
      return `<path d="${path}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
        ${s.pts.map((p, i) => `<circle cx="${x(p.day)}" cy="${y(p.v)}" r="${i === s.pts.length - 1 ? 4 : 2.5}" fill="${s.color}" class="pt"/>`).join("")}`;
    }).join("");
    this._line = {
      H, padB,
      hits: days.map((d) => {
        const rows = series.map((s) => ({ s, p: s.pts.find((q) => q.day === d) })).filter((r) => r.p);
        return {
          x: x(d),
          top: y(Math.max(...rows.map((r) => r.p.v))),
          html: `<b>${fmtDayLong(d)}</b><br>` + rows.map((r) => `<span class="sw" style="background:${r.s.color}"></span>${esc(r.s.name)}: <b>${pct(r.p.v)}</b> <span class="mute">${r.s.bw ? `${r.p.raw.reps} reps` : `${num(r.p.raw.top)} kg \u00d7 ${r.p.raw.reps}`}</span>`).join("<br>"),
        };
      }),
    };
    const legend = series.map((s) => {
      const last = s.pts[s.pts.length - 1];
      return `<li><button data-act="pick-ex" data-name="${esc(s.name)}" title="Show ${esc(s.name)} on its own">
        <i style="background:${s.color}"></i><span>${esc(s.name)}</span><b>${s.pts.length > 1 ? pct(last.v) : "new"}</b></button></li>`;
    }).join("");
    const table = series.map((s) => {
      const f = s.pts[0], l = s.pts[s.pts.length - 1];
      const fmt = (p) => (s.bw ? `${p.raw.reps} reps` : `${num(p.raw.top)} kg, max ${num(p.raw.strength)}`);
      return `<tr><td>${esc(s.name)}</td><td>${fmtDay(f.day)}: ${fmt(f)}</td><td>${fmtDay(l.day)}: ${fmt(l)}</td><td>${pct(l.v)}</td></tr>`;
    }).join("");
    const label = this._grp === "all" ? "all groups" : title(this._grp);
    const note = list.length > MAX ? ` Showing your ${MAX} most-trained exercises; pick a muscle group to see the rest.` : "";
    return `<p class="sub">Change in estimated max since each exercise's first session (${esc(label)}). Tap a name to see it on its own.${note}</p>
      ${this._lineSvg(W, H, padT, padB, grid + xl, lines, `Progression of ${series.length} exercises in ${label}`)}
      <ul class="mlegend">${legend}</ul>
      <details class="numbers"><summary>Show the numbers</summary>
        <table><thead><tr><th>Exercise</th><th>First</th><th>Latest</th><th>Change</th></tr></thead><tbody>${table}</tbody></table>
      </details>`;
  }

  _lineSvg(W, H, padT, padB, axes, lines, label) {
    return `<div class="chart" id="line-wrap">
        <svg id="line" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">
          ${axes}
          <line id="xh" class="xh" x1="0" x2="0" y1="${padT}" y2="${H - padB}" visibility="hidden"/>
          ${lines}
          <rect id="hit" x="0" y="0" width="${W}" height="${H}" fill="transparent"/>
        </svg>
      </div>`;
  }

  _wireLine() {
    const svg = this.shadowRoot.getElementById("line");
    const L = this._line;
    if (!svg || !L || !L.hits.length) return;
    const xh = this.shadowRoot.getElementById("xh");
    const hit = this.shadowRoot.getElementById("hit");
    const move = (e) => {
      const r = svg.getBoundingClientRect();
      const px = e.clientX - r.left;
      let best = L.hits[0], bd = Infinity;
      for (const h of L.hits) { const d = Math.abs(h.x - px); if (d < bd) { bd = d; best = h; } }
      xh.setAttribute("x1", best.x); xh.setAttribute("x2", best.x); xh.setAttribute("visibility", "visible");
      this._showTip(best.html, { left: r.left + best.x - 1, width: 2, top: r.top + best.top, bottom: r.top + L.H - L.padB });
    };
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerdown", move);
    hit.addEventListener("pointerleave", () => { xh.setAttribute("visibility", "hidden"); this._hideTip(); });
  }

  // Weekly sets ---------------------------------------------------------
  _weekly(a, W) {
    const weeks = a.weeks || [];
    const H = 160, padL = 30, padB = 22, padT = 18;
    const max = Math.max(10, ...weeks.map((w) => w.sets));
    const ticks = niceTicks(0, max, 3);
    const top = ticks[ticks.length - 1];
    const bw = (W - padL) / weeks.length;
    const barW = Math.max(6, Math.min(28, bw - 4));
    const y = (v) => padT + (H - padT - padB) * (1 - v / top);
    const col = SERIES[this._dark ? "dark" : "light"].weight;
    const active = weeks.filter((w) => w.sets);
    const avg = active.length ? Math.round(active.reduce((s, w) => s + w.sets, 0) / active.length) : 0;

    const bars = weeks.map((w, i) => {
      const cx = padL + bw * i + bw / 2;
      const h = Math.max(0, y(0) - y(w.sets));
      const groups = Object.entries(w.by_group).sort((p, q) => q[1] - p[1]).map(([g, c]) => `${title(g)} ${c}`).join(", ");
      const tip = `<b>Week of ${fmtDay(w.start)}</b><br>${w.sets} sets in ${w.workouts} workout${w.workouts === 1 ? "" : "s"}${groups ? `<br><span class="mute">${groups}</span>` : ""}`;
      const isLast = i === weeks.length - 1;
      const r = Math.min(4, barW / 2, h);
      const bar = w.sets
        ? `<path d="M${cx - barW / 2},${y(0)} V${y(w.sets) + r} q0,-${r} ${r},-${r} H${cx + barW / 2 - r} q${r},0 ${r},${r} V${y(0)} Z" fill="${col}" class="${isLast ? "" : "past"}"/>`
        : "";
      return `<g data-tip="${esc(tip)}"><rect x="${cx - bw / 2}" y="${padT}" width="${bw}" height="${H - padT - padB}" fill="transparent"/>${bar}
        ${isLast && w.sets ? `<text x="${cx}" y="${y(w.sets) - 6}" class="endl" text-anchor="middle">${w.sets}</text>` : ""}</g>`;
    }).join("");
    const grid = ticks.map((t) => `<line x1="${padL}" x2="${W}" y1="${y(t)}" y2="${y(t)}" class="gl"/><text x="${padL - 6}" y="${y(t) + 4}" class="ax" text-anchor="end">${t}</text>`).join("");
    const xl = weeks.length ? `<text x="${padL + bw / 2}" y="${H - 5}" class="ax" text-anchor="start">${fmtDay(weeks[0].start)}</text><text x="${W}" y="${H - 5}" class="ax" text-anchor="end">This week</text>` : "";

    return `<section class="panel">
      <h2>Sets per week</h2>
      <p class="sub">${avg ? `About ${avg} sets in an active week` : "No sets logged in the last 12 weeks"}</p>
      <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Sets per week for the last ${weeks.length} weeks">${grid}${bars}${xl}</svg>
    </section>`;
  }

  // Consistency calendar ---------------------------------------------
  _calendar(a, W) {
    const cal = a.calendar || [];
    const ramp = SEQ[this._dark ? "dark" : "light"];
    const nW = Math.ceil(cal.length / 7);
    const gap = 3, labW = 30;
    const cell = Math.max(10, Math.min(24, Math.floor((W - labW - gap * nW) / nW)));
    const H = 7 * (cell + gap) + 22;
    const cells = cal.map((d, i) => {
      const wk = Math.floor(i / 7), dw = i % 7;
      const b = bin(d.sets);
      const fill = b < 0 ? "var(--wc-sunk)" : ramp[b];
      const tip = `<b>${fmtDayLong(d.day)}</b><br>${d.sets ? `${d.sets} sets: ${d.groups.map(title).join(", ")}` : "Rest day"}`;
      return `<rect x="${labW + wk * (cell + gap)}" y="${dw * (cell + gap)}" width="${cell}" height="${cell}" rx="3" fill="${fill}" ${d.day === a.today ? 'class="todaycell"' : ""} data-tip="${esc(tip)}"/>`;
    }).join("");
    const labels = [0, 2, 4].map((dw) => `<text x="0" y="${dw * (cell + gap) + cell - 2}" class="ax">${DOW[dw]}</text>`).join("");
    // month labels: one per month start; drop a label crowded by the next one
    const starts = [];
    let lastM = -1;
    for (let wk = 0; wk < nW; wk++) {
      const d = cal[wk * 7]; if (!d) continue;
      const m = pd(d.day).getMonth();
      if (m !== lastM) { starts.push({ wk, m }); lastM = m; }
    }
    const months = starts.filter((s0, k) => !starts[k + 1] || starts[k + 1].wk - s0.wk >= 3)
      .map((s0) => `<text x="${labW + s0.wk * (cell + gap)}" y="${7 * (cell + gap) + 14}" class="ax">${MONTHS[s0.m]}</text>`);
    const trained = cal.filter((d) => d.sets).length;
    const legend = `<div class="scale"><span>Fewer sets</span>${ramp.map((c) => `<i style="background:${c}"></i>`).join("")}<span>More</span></div>`;
    return `<section class="panel">
      <h2>Consistency</h2>
      <p class="sub">${trained} training days in the last ${nW} weeks</p>
      <svg width="${labW + nW * (cell + gap)}" height="${H}" role="img" aria-label="Training calendar, ${trained} training days">${labels}${cells}${months.join("")}</svg>
      ${legend}
    </section>`;
  }

  // Muscle balance ----------------------------------------------------
  _balance(a) {
    const rows = a.balance || [];
    const max = Math.max(1, ...rows.map((r) => r.sets));
    const col = SERIES[this._dark ? "dark" : "light"].weight;
    const row = (r) => {
      const when = r.days == null ? "not yet" : r.days === 0 ? "today" : r.days === 1 ? "yesterday" : `${r.days} days ago`;
      return `<li data-tip="${esc(`<b>${title(r.group)}</b><br>${r.sets} sets in the last 4 weeks<br><span class='mute'>Last trained ${when}</span>`)}">
        <span class="bn">${esc(title(r.group))}</span>
        <span class="track"><i style="width:${(r.sets / max) * 100}%;background:${col}"></i></span>
        <span class="bv">${r.sets}</span>
        <span class="bd">${when}</span>
      </li>`;
    };
    return `<section class="panel">
      <h2>Muscle balance</h2>
      <p class="sub">Sets in the last 4 weeks, and when you last trained each group</p>
      <ul class="bars">${rows.filter((r) => !r.addon).map(row).join("")}</ul>
      <ul class="bars addon">${rows.filter((r) => r.addon).map(row).join("")}</ul>
    </section>`;
  }

  // Personal bests ----------------------------------------------------
  _bests(a) {
    const prs = a.prs || [];
    if (!prs.length) return `<section class="panel"><h2>Personal bests</h2><p class="hint">Your heaviest lifts will collect here.</p></section>`;
    return `<section class="panel">
      <h2>Personal bests</h2>
      <p class="sub">Heaviest working weight per exercise, most recent first</p>
      <ol class="prs">${prs.map((p) => `<li>
        <span class="pn">${esc(p.name)}${p.new ? `<em>New</em>` : ""}</span>
        <b>${num(p.weight)} kg</b>
        <time>${fmtDay(p.weight_day)}</time>
      </li>`).join("")}</ol>
    </section>`;
  }
}

function niceTicks(lo, hi, count) {
  const span = hi - lo || 1;
  const raw = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
  const out = [];
  for (let v = Math.floor(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Math.round(v * 100) / 100);
  if (out[out.length - 1] < hi) out.push(Math.round((out[out.length - 1] + step) * 100) / 100);
  return out;
}

const OV_CSS = `
.ov { position: relative; padding-bottom: 16px; }
.week h1 { margin: 0; display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; font-weight: 400; }
.week h1 b { font: 600 54px/0.9 var(--wc-num); }
.week h1 span { font-size: 18px; }
.sub { margin: 4px 0 12px; color: var(--wc-mute); font-size: 14px; }
.days { list-style: none; margin: 14px 0 0; padding: 0; display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }
.days li { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 8px 2px 10px; border-radius: 12px; min-width: 0; }
.days li.today { box-shadow: inset 0 0 0 1px var(--wc-line); }
.days .dow { font-size: 12px; color: var(--wc-mute); }
.days .dot { width: 34px; height: 34px; border-radius: 50%; border: 2px dashed var(--wc-line);
  display: flex; align-items: center; justify-content: center; font: 600 13px/1 var(--wc-num); }
.days li.on .dot { border: 0; background: var(--wc-fill); color: var(--wc-on-fill);
  box-shadow: inset 0 0 0 3px var(--wc-bg), inset 0 0 0 4px color-mix(in srgb, var(--wc-bg) 35%, transparent); }
.days li.future .dot { border-style: solid; opacity: .45; }
.days .gs { font-size: 11px; line-height: 1.25; text-align: center; color: var(--wc-mute); min-height: 2.5em; overflow: hidden; max-width: 100%; text-overflow: ellipsis; }

.grid { display: grid; grid-template-columns: 1fr; gap: 0; margin: 8px -18px 0; }
.ov.wide .grid { grid-template-columns: 1fr 1fr; }
.panel { padding: 18px 18px 10px; border-top: 1px solid var(--wc-line); min-width: 0; }
.ov.wide .panel:nth-child(even):not(.span) { border-left: 1px solid var(--wc-line); }
.panel.span { grid-column: 1 / -1; }
.panel h2 { margin: 0; font-size: 17px; font-weight: 600; }
.phead { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.phead select { font: inherit; font-size: 15px; color: var(--wc-ink); background: var(--wc-sunk); border: 0; border-radius: 10px; padding: 8px 10px; max-width: 100%; }
.filters { display: grid; grid-template-columns: 1fr 1.4fr; gap: 8px; margin: 10px 0 6px; }
.filters label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--wc-mute); min-width: 0; }
.filters select { font: inherit; font-size: 15px; color: var(--wc-ink); background: var(--wc-sunk); border: 0; border-radius: 10px;
  padding: 8px 10px; width: 100%; min-width: 0; }
.ov.wide .filters { max-width: 560px; }
.gl.zero { stroke: var(--wc-mute); stroke-opacity: .6; }
.mlegend { list-style: none; margin: 8px 0 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 2px 16px; }
.mlegend button { display: flex; align-items: center; gap: 8px; width: 100%; padding: 5px 0; border: 0; background: none;
  color: var(--wc-ink); font: inherit; font-size: 14px; text-align: left; cursor: pointer; }
.mlegend button:hover span { text-decoration: underline; }
.mlegend i { flex: none; width: 12px; height: 3px; border-radius: 2px; }
.mlegend span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mlegend b { font: 600 13px/1 var(--wc-num); }
.legend { list-style: none; margin: 0 0 6px; padding: 0; display: flex; gap: 16px; font-size: 13px; color: var(--wc-mute); }
.legend i { display: inline-block; width: 12px; height: 3px; border-radius: 2px; vertical-align: middle; margin-right: 6px; }
svg { display: block; overflow: visible; touch-action: pan-y; }
.gl { stroke: var(--wc-line); stroke-width: 1; }
path.past { opacity: .85; }
:host([data-dark]) path.past { opacity: .6; }
:host([data-dark]) .track i { opacity: .8; }
.ax { fill: var(--wc-mute); font-size: 11px; font-family: inherit; }
.endl { fill: var(--wc-ink); font-size: 12px; font-weight: 600; font-family: inherit; }
.pt { stroke: var(--wc-bg); stroke-width: 2; }
.xh { stroke: var(--wc-mute); stroke-width: 1; }
.todaycell { stroke: var(--wc-ink); stroke-width: 1.5; }
.numbers { margin: 8px 0 4px; }
.numbers summary { color: var(--wc-mute); font-size: 14px; cursor: pointer; padding: 4px 0; }
.numbers table { width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 6px; font-variant-numeric: tabular-nums; }
.numbers th { text-align: left; font-weight: 500; color: var(--wc-mute); padding: 4px 6px 4px 0; }
.numbers td { padding: 5px 6px 5px 0; border-top: 1px solid var(--wc-line); }
.scale { display: flex; align-items: center; gap: 3px; margin-top: 8px; font-size: 12px; color: var(--wc-mute); }
.scale i { width: 14px; height: 14px; border-radius: 3px; }
.scale span:first-child { margin-right: 4px; } .scale span:last-child { margin-left: 4px; }
.bars { list-style: none; margin: 0; padding: 0; }
.bars.addon { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--wc-line); }
.bars li { display: grid; grid-template-columns: 76px 1fr 28px 84px; align-items: center; gap: 8px; padding: 5px 0; font-size: 14px; }
.track { height: 8px; border-radius: 4px; background: var(--wc-sunk); overflow: hidden; }
.track i { display: block; height: 100%; border-radius: 4px; }
.bv { text-align: right; font: 600 14px/1 var(--wc-num); }
.bd { color: var(--wc-mute); font-size: 12px; text-align: right; }
.prs { list-style: none; margin: 0; padding: 0; }
.prs li { display: grid; grid-template-columns: 1fr auto 56px; gap: 10px; align-items: baseline; padding: 7px 0; font-size: 14px; }
.prs li + li { border-top: 1px solid var(--wc-line); }
.prs b { font: 600 16px/1 var(--wc-num); }
.prs time { color: var(--wc-mute); font-size: 12px; text-align: right; }
.prs em { font-style: normal; font-size: 11px; font-weight: 600; margin-left: 8px; padding: 2px 6px; border-radius: 999px; background: var(--plate-green); color: #fff; }
.mnav { display: flex; align-items: center; gap: 4px; }
.mnav span { min-width: 9.5em; text-align: center; font-weight: 500; }
.mnav button { width: 36px; height: 36px; border-radius: 50%; border: 1px solid var(--wc-line); background: none; font-size: 20px; line-height: 1; }
.mnav button:disabled { opacity: .3; cursor: default; }
.calbody { display: grid; grid-template-columns: 1fr; gap: 0; }
.mgrid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; max-width: 360px; }
.ov.wide .calbody.open { grid-template-columns: 360px 1fr; gap: 24px; align-items: start; }
.mh { font-size: 11px; color: var(--wc-mute); text-align: center; padding-bottom: 2px; }
.mday { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 1px; min-width: 0;
  height: 44px; padding: 2px; border: 0; border-radius: 8px; background: none; }
.mday:not(.blank):not(:disabled):hover { background: var(--wc-sunk); }
.mday:disabled { opacity: .3; cursor: default; }
.mday .dn { font-size: 11px; color: var(--wc-mute); line-height: 1; font-variant-numeric: tabular-nums; }
.mday.today .dn { color: var(--wc-ink); font-weight: 700; }
.mday .pl { width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
  font: 600 11px/1 var(--wc-num); }
.mday.on .pl { background: var(--wc-fill); color: var(--wc-on-fill); }
.mday.sel { box-shadow: inset 0 0 0 2px var(--wc-sel-line); }
.dayd { margin: 14px -18px 0; padding: 16px 18px 6px; border-top: 1px solid var(--wc-line); background: var(--wc-sunk); }
.ov.wide .dayd { margin: 0; border-top: 0; border-radius: 12px; }
.dhead { display: flex; align-items: baseline; gap: 10px; }
.dhead h3 { margin: 0; font: 600 22px/1 var(--wc-num); }
.dmsg { color: var(--plate-green); font-size: 13px; font-weight: 600; margin-left: auto; }
.dhead .link { margin-left: 8px; }
.dmsg:empty + .link { margin-left: auto; }
.err { color: var(--error-color, #db4437); font-size: 14px; }
.dex { margin: 14px 0 6px; }
.dex h4 { margin: 0 0 4px; font-size: 15px; font-weight: 600; }
.dex h4 small { font-weight: 400; color: var(--wc-mute); margin-left: 6px; }
.dsets { border-collapse: collapse; width: 100%; max-width: 460px; font-size: 14px; }
.dsets th { text-align: left; font-weight: 500; font-size: 12px; color: var(--wc-mute); padding: 2px 6px 4px 0; }
.dsets td { padding: 3px 6px 3px 0; }
.dsets .sn { color: var(--wc-mute); width: 32px; font-variant-numeric: tabular-nums; }
.dsets input, .dsets select, .addex select {
  font: inherit; font-size: 16px; color: var(--wc-ink); background: var(--wc-bg);
  border: 1px solid var(--wc-line); border-radius: 8px; padding: 7px 8px; width: 100%; min-width: 0; }
.dsets input[data-f="reps"] { max-width: 64px; }
.dsets select { max-width: 76px; min-width: 62px; padding-right: 2px; }
.unit { display: flex; align-items: center; gap: 4px; max-width: 96px; }
.unit span { color: var(--wc-mute); font-size: 12px; }
.del { border: 0; background: none; color: var(--wc-mute); font-size: 15px; padding: 8px; border-radius: 8px; white-space: nowrap; }
.del.armed { color: #fff; background: var(--error-color, #db4437); font-size: 13px; }
.addex { display: flex; flex-direction: column; gap: 4px; margin: 16px 0 12px; font-size: 13px; color: var(--wc-mute); max-width: 460px; }
.stat { margin: 8px 0 0; display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.stat b { font: 600 30px/1 var(--wc-num); }
.stat span { color: var(--wc-mute); font-size: 14px; }
.btiles { list-style: none; margin: 10px 0 12px; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 10px; }
.btiles li { display: flex; flex-direction: column; gap: 2px; }
.btiles span { font-size: 12px; color: var(--wc-mute); }
.btiles b { font: 600 22px/1 var(--wc-num); }
.btiles b i { font: 500 12px/1 inherit; font-style: normal; color: var(--wc-mute); margin-left: 3px; font-family: inherit; }
.btiles small { font-size: 12px; color: var(--wc-mute); }
.btiles.derived { margin-top: 0; padding-top: 10px; border-top: 1px solid var(--wc-line); }
.btiles.derived b { font-size: 22px; display: flex; align-items: center; gap: 8px; }
.spark { color: var(--wc-mute); flex: none; }
.spark .last { fill: var(--wc-ink); }
.warn { margin: 6px 0 10px; padding: 10px 12px; border-radius: 10px; font-size: 13px; line-height: 1.4;
  background: color-mix(in srgb, var(--plate-yellow) 16%, transparent); border-left: 4px solid var(--plate-yellow); }
.warn code { font-size: 12px; }
.hline { margin: 2px 0 6px; font-size: 13px; color: var(--wc-mute); display: flex; align-items: center; gap: 10px; }
.hline .link { padding: 0; font-size: 13px; }
.bform { display: grid; grid-template-columns: 1fr 1fr auto; gap: 8px; align-items: end; margin-top: 12px; }
.bform label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--wc-mute); min-width: 0; }
.bform select, .bform input { font: inherit; font-size: 16px; color: var(--wc-ink); background: var(--wc-sunk);
  border: 1px solid transparent; border-radius: 10px; padding: 9px 10px; width: 100%; min-width: 0; }
.bform .unit { max-width: none; }
.bform .add-btn { height: 42px; padding: 0 16px; grid-column: auto; }
.exp .add-btn { grid-column: auto; }
.add-btn { border-radius: 12px; border: 1px solid var(--wc-ink); background: none; font-weight: 600; color: var(--wc-ink); cursor: pointer; }
.add-btn.small { height: 36px; padding: 0 14px; font-size: 14px; }
.add-btn:disabled { opacity: .5; }
.brecent { list-style: none; margin: 6px 0 0; padding: 0; }
.brecent li { display: grid; grid-template-columns: 56px 1fr auto auto; gap: 8px; align-items: center; font-size: 14px; padding: 2px 0; }
.brecent time { color: var(--wc-mute); font-size: 12px; }
.files { list-style: none; margin: 4px 0 0; padding: 0; display: flex; gap: 20px; flex-wrap: wrap; }
.files a { color: var(--wc-link); font-weight: 500; }
.files small { color: var(--wc-mute); margin-left: 6px; }
.wtog { width: 30px; height: 30px; border-radius: 50%; border: 1px dashed var(--wc-line); background: none; color: var(--wc-mute);
  font: 600 11px/1 var(--wc-num); padding: 0; }
.wtog.on { border: 2px solid var(--plate-yellow); color: var(--wc-ink); }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
.tip { position: absolute; left: 0; top: 0; z-index: 5; pointer-events: none; max-width: 240px;
  background: var(--wc-fill); color: var(--wc-on-fill); border-radius: 8px; padding: 7px 10px; font-size: 13px; line-height: 1.35;
  box-shadow: 0 4px 14px rgba(0,0,0,.25); }
.tip .mute { opacity: .75; }
.tip .sw { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
[data-tip] { cursor: default; }

/* ======== home-dashboard look ======== */
.wc.ov { padding: 0; }
.week { padding: 4px 2px 0; }
.week h1 b { font: 500 clamp(56px, 9vw, 84px)/.9 var(--display); letter-spacing: -.03em; }
.week h1 span { font-size: 17px; color: var(--muted); }
.sub { color: var(--muted); font-size: 15px; }
.days li { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-md); }
.days li.today { box-shadow: none; border-color: var(--lamp); }
.days .dot { border-color: #3b4657; font: 600 14px/1 var(--display); }
.days li.on .dot { background: var(--lamp); color: #1b1406; box-shadow: none; }
.grid { gap: 16px; margin: 18px 0 0; }
.panel { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-lg); padding: 18px 18px 14px; }
.ov.wide .panel:nth-child(even):not(.span) { border-left: 1px solid var(--line); }
.panel h2 { font: 600 19px/1.2 var(--display); letter-spacing: -.01em; }
.phead select, .filters select, .bform select, .bform input, .dsets input, .dsets select, .addex select {
  background: var(--surface-2); border: 1px solid var(--line); color: var(--text); }
.gl { stroke: #26303d; }
.pt { stroke: var(--surface); }
.todaycell { stroke: var(--lamp); }
.endl { fill: var(--text); }
.track { background: var(--surface-2); }
.bv, .prs b, .stat b, .btiles b { font-family: var(--display); font-weight: 600; letter-spacing: -.02em; }
.prs li + li, .numbers td, .bars.addon, .btiles.derived { border-color: var(--line); }
.prs em { background: var(--presence); color: #0c1d1a; }
.mnav button { border-color: var(--line); background: var(--surface-2); color: var(--text); }
.mday.on .pl { background: var(--lamp); color: #1b1406; }
.mday.sel { box-shadow: inset 0 0 0 2px var(--lamp); }
.mday.today .dn { color: var(--lamp); }
.dayd { background: var(--surface-2); }
.ov.wide .dayd { border-radius: var(--r-md); }
.dhead h3 { font: 600 24px/1 var(--display); letter-spacing: -.02em; }
.dmsg { color: var(--presence); }
.del.armed { background: var(--alert); }
.err { color: var(--alert); }
.add-btn { background: var(--surface-2); border: 1px solid var(--line); color: var(--text); }
.warn { background: var(--lamp-soft); border-left-color: var(--lamp); }
.wtog.on { border-color: var(--lamp); }
.files a { color: var(--lamp); }
.tip { background: #0c1118; color: var(--text); border: 1px solid var(--line); box-shadow: 0 8px 24px rgba(0,0,0,.4); }
`;

if (!customElements.get("workout-overview-card")) customElements.define("workout-overview-card", WorkoutOverviewCard);
if (!window.customCards.some((c) => c.type === "workout-overview-card")) window.customCards.push({
  type: "workout-overview-card",
  name: "Workout overview card",
  description: "Weekly overview, consistency calendar, muscle balance and weight progression.",
});

/* ======================================================================
 * workout-tracker-panel: the "Workout" page in the sidebar (integration).
 * Two tabs: Workout (logging) and Overview (charts, calendar, body), on a
 * side rail (bottom bar on phones), in the same look as the home dashboard.
 * ==================================================================== */
const PANEL_CSS = `
:host {
  --bg: #141a22; --surface: #1c2430; --surface-2: #232d3b; --line: #2a3442;
  --text: #e6eaf0; --muted: #8a96a8; --faint: #5d6878; --lamp: #f2b544;
  --display: "Space Grotesk", var(--ha-font-family-body, system-ui), sans-serif;
  --body: var(--ha-font-family-body, var(--paper-font-body1_-_font-family, system-ui)), sans-serif;
  display: block; min-height: 100vh; background: var(--bg); color: var(--text); font-family: var(--body);
}
* { box-sizing: border-box; }
button { font: inherit; color: inherit; background: none; border: 0; padding: 0; cursor: pointer; }
button:focus-visible, select:focus-visible { outline: 2px solid var(--lamp); outline-offset: 2px; }
ha-icon { --mdc-icon-size: 22px; display: inline-flex; }
.app { display: grid; grid-template-columns: 88px 1fr; min-height: 100vh; }
nav { display: flex; flex-direction: column; gap: 6px; padding: 18px 10px; border-right: 1px solid var(--line);
  position: sticky; top: 0; height: 100vh; }
nav button { display: grid; justify-items: center; gap: 4px; padding: 10px 4px; border-radius: 14px; color: var(--muted); font-size: 12px; }
nav button[aria-selected="true"] { background: var(--surface-2); color: var(--text); }
nav button[aria-selected="true"] ha-icon { color: var(--lamp); }
nav .logo { display: grid; place-items: center; height: 44px; margin-bottom: 8px; color: var(--lamp); }
nav .logo ha-icon { --mdc-icon-size: 28px; }
main { padding: 22px 32px 40px; min-width: 0; }
.top { display: flex; align-items: center; gap: 10px; margin-bottom: 18px; min-height: 40px; }
.top #menu { margin-left: -8px; }
.top h1 { margin: 0; font: 600 22px/1.2 var(--display); letter-spacing: -.01em; }
.top .date { color: var(--muted); font-size: 14px; margin-left: 4px; }
.top .grow { flex: 1; }
.who select { font: inherit; font-size: 14px; color: var(--text); background-color: var(--surface); border: 1px solid var(--line);
  border-radius: 999px; padding: 8px 32px 8px 14px; appearance: none; cursor: pointer;
  background-image: linear-gradient(45deg, transparent 50%, var(--muted) 50%), linear-gradient(135deg, var(--muted) 50%, transparent 50%);
  background-position: calc(100% - 17px) 55%, calc(100% - 12px) 55%; background-size: 5px 5px; background-repeat: no-repeat; }
.who select option { color: #1d1f21; }
.page.workout { max-width: 1240px; }
.page.overview { max-width: 1240px; }
[hidden] { display: none !important; }
@media (max-width: 760px) {
  .app { grid-template-columns: 1fr; }
  nav { position: fixed; bottom: 0; left: 0; right: 0; top: auto; height: auto; flex-direction: row; justify-content: space-around;
    padding: 6px 4px calc(6px + env(safe-area-inset-bottom)); border-right: 0; border-top: 1px solid var(--line); background: var(--bg); z-index: 5; }
  nav .logo { display: none; }
  nav button { flex: 1; font-size: 11px; padding: 6px 2px; }
  main { padding: 10px 14px 96px; }
  .top { margin-bottom: 12px; }
  .top .date { display: none; }
  .who select { max-width: 150px; text-overflow: ellipsis; }
}
`;

class WorkoutTrackerPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    let tab = "workout";
    try { tab = localStorage.getItem("workout-tracker-tab") || "workout"; } catch (e) { /* storage blocked */ }
    this._tab = tab === "overview" ? "overview" : "workout";
  }

  set hass(hass) {
    this._hass = hass;
    this._build();
    WT.ensure(hass);
    this._renderWho();
    this._menu.hass = hass;
    this._workout.hass = hass;
    this._overview.hass = hass;
  }

  set narrow(narrow) {
    this._narrow = narrow;
    if (this._menu) this._menu.narrow = narrow;
  }

  set panel(_p) { /* nothing to configure */ }

  _build() {
    if (this._built) return;
    this._built = true;
    this.shadowRoot.innerHTML = `<style>${PANEL_CSS}</style>
      <div class="app">
        <nav class="tabs" role="tablist" aria-label="Workout tracker">
          <span class="logo" aria-hidden="true"><ha-icon icon="mdi:dumbbell"></ha-icon></span>
          <button role="tab" data-tab="workout"><ha-icon icon="mdi:weight-lifter"></ha-icon><span>Workout</span></button>
          <button role="tab" data-tab="overview"><ha-icon icon="mdi:chart-timeline-variant"></ha-icon><span>Overview</span></button>
        </nav>
        <main>
          <header class="top">
            <span id="menu"></span>
            <h1 id="title">Workout</h1><span class="date" id="date"></span>
            <span class="grow"></span>
            <span class="who" id="who"></span>
          </header>
          <div class="page workout" id="p-workout"></div>
          <div class="page overview" id="p-overview"></div>
        </main>
      </div>`;
    this._menu = document.createElement("ha-menu-button");
    this._menu.narrow = this._narrow;
    this.shadowRoot.getElementById("menu").appendChild(this._menu);
    this._workout = document.createElement("workout-card");
    this._workout.setConfig({});
    this._overview = document.createElement("workout-overview-card");
    this._overview.setConfig({});
    this.shadowRoot.getElementById("p-workout").appendChild(this._workout);
    this.shadowRoot.getElementById("p-overview").appendChild(this._overview);
    this.shadowRoot.querySelector(".tabs").addEventListener("click", (e) => {
      const b = e.target.closest("[data-tab]");
      if (b) this._show(b.dataset.tab);
    });
    this.shadowRoot.getElementById("who").addEventListener("change", (e) => {
      if (e.target.id === "who-pick") WT.setProfile(e.target.value);
    });
    this._onStore = () => this._renderWho();
    WT.listeners.add(this._onStore);
    this._show(this._tab);
  }

  disconnectedCallback() {
    if (this._onStore) WT.listeners.delete(this._onStore);
  }

  connectedCallback() {
    if (this._onStore) WT.listeners.add(this._onStore);
  }

  /** Profile picker: only when more than one person can use the tracker. */
  _renderWho() {
    const host = this.shadowRoot.getElementById("who");
    const list = WT.profiles?.profiles || [];
    if (!host) return;
    if (list.length < 2) { host.innerHTML = ""; return; }
    const cur = WT.current();
    const me = WT.profiles.me;
    const key = list.map((p) => p.id).join() + cur;
    if (host.dataset.key === key) return;
    host.dataset.key = key;
    const esc2 = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    host.innerHTML = `<select id="who-pick" aria-label="Whose workouts">
      ${list.map((p) => `<option value="${esc2(p.id)}" ${p.id === cur ? "selected" : ""}>${esc2(p.name)}${p.id === me ? " (you)" : ""}</option>`).join("")}
    </select>`;
  }

  _show(tab) {
    this._tab = tab;
    try { localStorage.setItem("workout-tracker-tab", tab); } catch (e) { /* storage blocked */ }
    this.shadowRoot.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
    this.shadowRoot.getElementById("p-workout").hidden = tab !== "workout";
    this.shadowRoot.getElementById("p-overview").hidden = tab !== "overview";
    this.shadowRoot.getElementById("title").textContent = tab === "overview" ? "Overview" : "Workout";
    const d = new Date();
    this.shadowRoot.getElementById("date").textContent =
      `${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`;
    window.scrollTo(0, 0);
  }
}

if (!customElements.get("workout-tracker-panel")) customElements.define("workout-tracker-panel", WorkoutTrackerPanel);
