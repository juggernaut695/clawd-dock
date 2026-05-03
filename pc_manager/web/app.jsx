// Side Dock — App layer rewired to the Python `pywebview` bridge.
//
// State lives in `window.PCStore`. Python pushes deltas in via:
//   window.PCStore.set({...})
// React components subscribe with usePCStore(). UI events call back into
// Python through `window.pywebview.api.<method>(...)`.

const { useState, useEffect, useRef, useCallback } = React;

// ─── Store ────────────────────────────────────────────────────────────
const PCStore = {
  state: {
    stats: null,                    // { cpu, ram, gpu, disk, network, battery, uptime_seconds }
    voiceState: 'idle',             // idle | listening | thinking | speaking | happy | done
    voiceStatusLabel: '○ idle',
    transcript: [],                 // [{ who: 'you'|'claude', text }]
    listening: false,
    pendingTextDraft: '',
    actionResult: null,             // { ok, trimmed, freed_mb, error }
    notes: '',
    notesSaved: true,
  },
  listeners: new Set(),
  set(updates) {
    this.state = { ...this.state, ...updates };
    this.listeners.forEach((fn) => fn(this.state));
  },
  setStats(stats) { this.set({ stats }); },
  appendBubble(b) {
    this.set({ transcript: [...this.state.transcript, b] });
  },
  clearTranscript() { this.set({ transcript: [] }); },
};
window.PCStore = PCStore;

function usePCStore() {
  const [s, setS] = useState(PCStore.state);
  useEffect(() => {
    const sub = (next) => setS(next);
    PCStore.listeners.add(sub);
    return () => PCStore.listeners.delete(sub);
  }, []);
  return s;
}

const api = () => (window.pywebview && window.pywebview.api) || {};

// Pull a Claude Code session's history (parsed user/assistant text turns)
// over the localhost WebSocket bridge instead of the pywebview RPC — the
// chat-window RPC channel sometimes hangs after `load_url`. Resolves to
// the array of `{role, text}` messages, rejects on timeout/error.
function loadHistoryViaWS(sessionId, timeoutMs = 8000) {
  return new Promise((resolve, reject) => {
    let ws;
    const t = setTimeout(() => {
      try { ws && ws.close(); } catch (_) {}
      reject(new Error('history timeout'));
    }, timeoutMs);
    try {
      ws = new WebSocket(`ws://127.0.0.1:7654/history/${sessionId}`);
    } catch (e) { clearTimeout(t); reject(e); return; }
    ws.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch { return; }
      if (data.type === 'messages') {
        clearTimeout(t);
        try { ws.close(); } catch (_) {}
        resolve(data.messages || []);
      } else if (data.type === 'error') {
        clearTimeout(t);
        try { ws.close(); } catch (_) {}
        reject(new Error(data.error || 'history error'));
      }
    };
    ws.onerror = (e) => { clearTimeout(t); reject(e); };
    ws.onclose = () => { clearTimeout(t); };
  });
}

// pywebview injects `window.pywebview.api` AFTER the page's scripts run, so
// when load_url() navigates a window we briefly have a mounted React tree
// with no API. Standalone-window components call this before issuing their
// first API call so their bootstrap doesn't silently no-op.
async function waitForApi(timeoutMs = 4000) {
  const ready = () =>
    typeof window !== 'undefined' &&
    window.pywebview && window.pywebview.api &&
    Object.keys(window.pywebview.api).length > 0;
  if (ready()) return true;
  return new Promise((resolve) => {
    let done = false;
    const finish = (ok) => { if (!done) { done = true; resolve(ok); } };
    const onReady = () => finish(true);
    window.addEventListener('pywebviewready', onReady, { once: true });
    const start = Date.now();
    const tick = () => {
      if (ready()) return finish(true);
      if (Date.now() - start >= timeoutMs) {
        window.removeEventListener('pywebviewready', onReady);
        return finish(false);
      }
      setTimeout(tick, 40);
    };
    tick();
  });
}

// Parse `#k1=v1&k2=v2` into an object — used to drive the dedicated chat window
function parseHashParams(hash) {
  const out = {};
  (hash || '').replace(/^#/, '').split('&').forEach((p) => {
    if (!p) return;
    const i = p.indexOf('=');
    const k = i >= 0 ? p.slice(0, i) : p;
    const v = i >= 0 ? p.slice(i + 1) : '';
    if (k) out[k] = decodeURIComponent(v);
  });
  return out;
}
const HASH = parseHashParams(typeof window !== 'undefined' ? window.location.hash : '');
const IS_FULL_CHAT = HASH.mode === 'chat';
const IS_TASK_EDIT = HASH.mode === 'task';
// Both standalone windows reuse the `fullchat` body class — gives them the
// transparent / frosted-glass body styling and disables the dock's gradient.
if ((IS_FULL_CHAT || IS_TASK_EDIT) && typeof document !== 'undefined') {
  document.body.classList.add('fullchat');
  document.documentElement.classList.add('fullchat-html');
}

// Claude CLI–style adjective pool, cycled while the model is working.
const THINKING_VERBS = [
  'Thinking', 'Pondering', 'Brewing', 'Cooking', 'Crafting',
  'Wrangling', 'Computing', 'Considering', 'Deliberating',
  'Hatching', 'Mulling', 'Musing', 'Noodling', 'Percolating',
  'Processing', 'Reasoning', 'Reflecting', 'Ruminating',
  'Stewing', 'Wondering', 'Synthesizing', 'Untangling',
  'Forging', 'Conjuring', 'Cogitating', 'Contemplating',
  'Plotting', 'Scheming', 'Unraveling', 'Distilling',
];

// ─── Helpers ──────────────────────────────────────────────────────────
function ThickArrow({ dir = 'right', size = 14, color = 'currentColor' }) {
  const rot = { right: 0, down: 90, left: 180, up: 270 }[dir] || 0;
  return (
    <svg width={size} height={size} viewBox="0 0 16 16"
      style={{ transform: `rotate(${rot}deg)`, display: 'block' }}
      fill="none" stroke={color} strokeWidth="2.6"
      strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 8 H13" />
      <path d="M9 4 L13 8 L9 12" />
    </svg>
  );
}

function Ring({ value, color = '#5eead4' }) {
  const r = 13;
  const c = 2 * Math.PI * r;
  const off = c - (Math.max(0, Math.min(value, 100)) / 100) * c;
  return (
    <svg className="ring" viewBox="0 0 32 32">
      <circle cx="16" cy="16" r={r} fill="none"
        stroke="rgba(255,255,255,0.1)" strokeWidth="3" />
      <circle cx="16" cy="16" r={r} fill="none"
        stroke={color} strokeWidth="3" strokeLinecap="round"
        strokeDasharray={c} strokeDashoffset={off}
        transform="rotate(-90 16 16)" />
    </svg>
  );
}

function Panel({ title, icon, dark = false, action = false, children }) {
  return (
    <div className={`panel ${dark ? 'dark' : ''}`}>
      <div className="panel-head">
        <div className="panel-title">
          {icon && <span className="panel-icon">{icon}</span>}
          {title}
        </div>
        {action && <div className="panel-arrow"><ThickArrow dir="right" /></div>}
      </div>
      {children}
    </div>
  );
}

// ─── Voice tab ────────────────────────────────────────────────────────
function VoiceTab() {
  const s = usePCStore();
  const [draft, setDraft] = useState('');
  const transcriptRef = useRef(null);
  const [demoState, setDemoState] = useState(null);
  const [demoLabel, setDemoLabel] = useState(null);

  // Auto-scroll bubbles to the bottom on new entries
  useEffect(() => {
    const el = transcriptRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [s.transcript.length]);

  const onSubmit = (e) => {
    e.preventDefault();
    const text = draft.trim();
    if (!text) return;
    setDraft('');
    api().submit_text && api().submit_text(text);
  };

  // Tap the mascot to cycle through every state for ~2.4s each
  const playAnimReel = () => {
    if (demoState) return;  // already playing
    const reel = [
      { state: 'idle',       label: '○ idle — calm, no sparkles' },
      { state: 'listening',  label: '● listening — still + sparkles' },
      { state: 'thinking',   label: '○ thinking — looking left ↔ right' },
      { state: 'speaking',   label: '◆ speaking — gentle sway' },
      { state: 'happy',      label: '✨ happy — both arms waving' },
      { state: 'finish',     label: '🏁 finish — racing flag up' },
      { state: 'exercising', label: '💪 exercising — dumbbells out' },
    ];
    let i = 0;
    setDemoState(reel[0].state);
    setDemoLabel(reel[0].label);
    const id = setInterval(() => {
      i += 1;
      if (i >= reel.length) {
        clearInterval(id);
        setDemoState(null);
        setDemoLabel(null);
        return;
      }
      setDemoState(reel[i].state);
      setDemoLabel(reel[i].label);
    }, 2400);
  };

  // Use demo state if active, otherwise the real voice state
  const effectiveState = demoState || s.voiceState;

  // While thinking, swap in a random verb every ~1.4s for a Claude-CLI vibe
  const [thinkVerb, setThinkVerb] = useState(null);
  useEffect(() => {
    if (effectiveState !== 'thinking') {
      setThinkVerb(null);
      return;
    }
    const pick = () => {
      const v = THINKING_VERBS[Math.floor(Math.random() * THINKING_VERBS.length)];
      setThinkVerb(v);
    };
    pick();
    const id = setInterval(pick, 1400);
    return () => clearInterval(id);
  }, [effectiveState]);

  const effectiveLabel = demoLabel
    || (thinkVerb ? `○ ${thinkVerb}…` : s.voiceStatusLabel);

  return (
    <Panel title="Claude voice" icon="✱" action={false}>
      {/* Mascot is pinned — does NOT scroll with the transcript.
          Tap the mascot to play through every animation state. */}
      <div className="voice-orb-wrap"
           onClick={playAnimReel}
           title="Tap to preview all animations"
           style={{ flex: '0 0 auto', padding: '4px 0 12px',
                    cursor: 'pointer' }}>
        {window.ClaudeMascot
          ? <ClaudeMascot state={effectiveState} />
          : <div style={{ width: 160, height: 160 }} />}
      </div>

      {/* Transcript fills the remaining height and scrolls; auto-pinned to bottom */}
      <div className="transcript"
           ref={transcriptRef}
           style={{
             flex: '1 1 0',
             minHeight: 0,
             overflowY: 'auto',
             overflowX: 'hidden',
             paddingRight: 4,
           }}>
        {s.transcript.length === 0 && (
          <div className="bubble claude" style={{ opacity: 0.7 }}>
            Tap "Start listening" or type below.
          </div>
        )}
        {s.transcript.map((m, i) => (
          <div key={i} className={`bubble ${m.who}`}>{m.text}</div>
        ))}
      </div>

      <div className="voice-status" style={{ flex: '0 0 auto' }}>
        <span style={{ color: 'var(--mint)', fontWeight: 500 }}>
          {effectiveLabel}
        </span>
        {!demoLabel && (<>
          <span>·</span>
          <span>en-US</span>
        </>)}
      </div>

      <form className="voice-text-fallback"
            style={{ flex: '0 0 auto', marginTop: 10 }}
            onSubmit={onSubmit}>
        <span className="vtf-icon">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none"
            stroke="currentColor" strokeWidth="2"
            strokeLinecap="round" strokeLinejoin="round">
            <path d="M2 4 H14 V11 H6 L3 13 V11 H2 Z" />
          </svg>
        </span>
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Type instead…"
          className="vtf-input"
        />
        <button type="submit" className="vtf-send"
          disabled={!draft.trim()} aria-label="Send">
          <ThickArrow dir="up" size={14} />
        </button>
      </form>
    </Panel>
  );
}

// ─── Monitor tab ──────────────────────────────────────────────────────
const fmtRate = (b) => {
  if (b == null) return '—';
  if (b >= 1024 * 1024) return `${(b / 1048576).toFixed(1)} MB/s`;
  if (b >= 1024) return `${(b / 1024).toFixed(0)} KB/s`;
  return `${Math.round(b)} B/s`;
};

function MonitorTab() {
  const s = usePCStore();
  const stats = s.stats;
  const [busy, setBusy] = useState(false);
  const onClear = async () => {
    if (busy) return;
    setBusy(true);
    try { await api().clear_ram?.(); } finally { setBusy(false); }
  };
  if (!stats) {
    return (
      <Panel title="System" icon="◐" action={true}>
        <div style={{ color: 'var(--ink-mute)', fontSize: 12 }}>loading…</div>
      </Panel>
    );
  }
  const cpu = stats.cpu || {};
  const ram = stats.ram || {};
  const gpu = (stats.gpu && stats.gpu[0]) || null;
  const disk = stats.disk && stats.disk.length
    ? stats.disk.reduce((a, b) => (a.percent > b.percent ? a : b))
    : null;
  const net = stats.net_rate || { down_bps: 0, up_bps: 0 };

  return (
    <>
      <Panel title="System" icon="◐" action={true}>
        <div className="metric-grid">
          <div className="metric-card">
            <Ring value={cpu.percent || 0} />
            <div className="ml">CPU</div>
            <div className="mv">
              {Math.round(cpu.percent || 0)}<span className="unit">%</span>
            </div>
            <div className="bar">
              <div className="bar-fill" style={{ width: `${cpu.percent || 0}%` }} />
            </div>
            <div className="sub">
              {cpu.count_logical || 0} cores
              {cpu.freq_mhz ? ` · ${(cpu.freq_mhz / 1000).toFixed(2)} GHz` : ''}
            </div>
          </div>

          <div className="metric-card">
            <Ring value={ram.percent || 0} />
            <div className="ml">MEMORY</div>
            <div className="mv">
              {(ram.used_gb || 0).toFixed(1)}
              <span className="unit"> / {Math.round(ram.total_gb || 0)} GB</span>
            </div>
            <div className="bar">
              <div className="bar-fill" style={{ width: `${ram.percent || 0}%` }} />
            </div>
            <div className="sub">{Math.round(ram.percent || 0)}% used</div>
          </div>

          <div className="metric-card">
            <Ring value={disk ? disk.percent : 0}
              color={disk && disk.percent >= 80 ? '#fbbf24' : '#5eead4'} />
            <div className="ml">STORAGE</div>
            <div className="mv">
              {disk ? Math.round(disk.used_gb) : 0}
              <span className="unit"> / {disk ? Math.round(disk.total_gb) : 0} GB</span>
            </div>
            <div className="bar">
              <div className={`bar-fill ${disk && disk.percent >= 80 ? 'warn' : ''}`}
                style={{ width: `${disk ? disk.percent : 0}%` }} />
            </div>
            <div className="sub">
              {disk ? `${disk.device} · ${Math.round(disk.percent)}% used` : '—'}
            </div>
          </div>

          <div className="metric-card">
            <Ring value={gpu ? gpu.util_percent : 0} />
            <div className="ml">GPU</div>
            <div className="mv">
              {gpu ? Math.round(gpu.util_percent) : '—'}
              {gpu ? <span className="unit">%</span> : null}
            </div>
            <div className="bar">
              <div className="bar-fill"
                style={{ width: `${gpu ? gpu.util_percent : 0}%` }} />
            </div>
            <div className="sub">
              {gpu
                ? `${gpu.name.replace(/^NVIDIA /, '')}${gpu.temp_c != null ? ` · ${gpu.temp_c}°C` : ''}`
                : 'no GPU'}
            </div>
          </div>
        </div>
      </Panel>

      <Panel title="Memory" icon="✦" action={false}>
        <button className="action-primary" onClick={onClear} disabled={busy}>
          <span>{busy ? 'Clearing…' : 'Clear RAM'}</span>
          <span className="ap-arr"><ThickArrow dir="right" size={14} /></span>
        </button>
        {s.actionResult && (
          s.actionResult.ok
            ? <div className="action-result">
                Trimmed {s.actionResult.trimmed} processes · freed {Math.round(s.actionResult.freed_mb)} MB
              </div>
            : <div className="action-result" style={{ color: '#fbbf24' }}>
                {s.actionResult.error || 'failed'}
              </div>
        )}
      </Panel>

      <Panel title="Network" icon="≋" action={false}>
        <div className="kv">
          <span className="k">
            <span className="arr"><ThickArrow dir="down" size={11} /></span>
            Download
          </span>
          <span className="v">{fmtRate(net.down_bps)}</span>
        </div>
        <div className="kv">
          <span className="k">
            <span className="arr"><ThickArrow dir="up" size={11} /></span>
            Upload
          </span>
          <span className="v">{fmtRate(net.up_bps)}</span>
        </div>
        <div className="kv">
          <span className="k">⌖ Public IP</span>
          <span className="v" style={{ color: stats.public_ip ? null : 'var(--ink-mute)' }}>
            {stats.public_ip || 'fetching…'}
          </span>
        </div>
        <div className="kv">
          <span className="k">⌖ Local IP</span>
          <span className="v">{stats.local_ip || '—'}</span>
        </div>
      </Panel>
    </>
  );
}

// ─── Control tab ──────────────────────────────────────────────────────
function ControlTab() {
  return (
    <Panel title="Quick actions" icon="✦" action={false}>
      <div className="action-grid">
        <button className="action-btn" onClick={() => api().lock?.()}>
          <span className="icon">⊟</span>Lock
        </button>
        <button className="action-btn" onClick={() => api().sleep?.()}>
          <span className="icon">☾</span>Sleep
        </button>
        <button className="action-btn" onClick={() => api().show_desktop?.()}>
          <span className="icon">▦</span>Desktop
        </button>
        <button className="action-btn" onClick={() => api().wake?.()}>
          <span className="icon">☀</span>Wake
        </button>
        <button className="action-btn" onClick={() => api().mute?.()}>
          <span className="icon">🔇</span>Mute
        </button>
        <button className="action-btn" onClick={() => api().capture?.()}>
          <span className="icon">◧</span>Capture
        </button>
      </div>
    </Panel>
  );
}

// ─── Tasks tab ────────────────────────────────────────────────────────
function TasksTab() {
  const [tasks, setTasks] = useState([]);
  const [draft, setDraft] = useState('');
  const [selectedId, setSelectedId] = useState(null);

  // Reload on mount AND every 3s while the tab is open, so changes made
  // through Claude (via MCP) show up without a full restart.
  useEffect(() => {
    let cancelled = false;
    const reload = () => {
      if (cancelled) return;
      if (api().load_tasks) {
        api().load_tasks().then((t) => {
          if (!cancelled) setTasks(Array.isArray(t) ? t : []);
        });
      }
    };
    reload();
    const id = setInterval(reload, 3000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const persist = (next) => {
    setTasks(next);
    if (api().save_tasks) api().save_tasks(next);
  };
  const addTask = (e) => {
    e.preventDefault();
    const t = draft.trim();
    if (!t) return;
    persist([...tasks, { id: Date.now(), text: t, done: false, description: '' }]);
    setDraft('');
  };
  const toggle = (id) =>
    persist(tasks.map((t) => (t.id === id ? { ...t, done: !t.done } : t)));
  const remove = (id) => persist(tasks.filter((t) => t.id !== id));
  const updateTask = (id, patch) =>
    persist(tasks.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  const clearDone = () => persist(tasks.filter((t) => !t.done));

  const remaining = tasks.filter((t) => !t.done).length;
  const total = tasks.length;

  // Task-detail editor view — shown when the user clicks a task's text
  const selected = selectedId != null
    ? tasks.find((t) => t.id === selectedId)
    : null;
  if (selected) {
    return (
      <Panel title="Task" icon="✓" action={false}>
        <TaskDetail
          task={selected}
          onChange={(patch) => updateTask(selected.id, patch)}
          onBack={() => setSelectedId(null)}
          onDelete={() => { remove(selected.id); setSelectedId(null); }}
        />
      </Panel>
    );
  }

  return (
    <Panel title="Tasks" icon="✓" action={false}>
      <form className="task-add" onSubmit={addTask}>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Add a task…"
        />
        <button type="submit" disabled={!draft.trim()} aria-label="Add">
          <ThickArrow dir="up" size={14} />
        </button>
      </form>

      {total === 0 && (
        <div className="cc-empty">no tasks yet — add one above</div>
      )}

      <div className="task-list">
        {tasks.map((t) => (
          <div key={t.id} className={`task ${t.done ? 'done' : ''}`}>
            <button className="task-check" onClick={() => toggle(t.id)}>
              {t.done ? '✓' : ''}
            </button>
            <span className="task-text"
                  onClick={() => {
                    // Prefer the standalone window; fall back to inline
                    if (api().open_task_window) {
                      try {
                        api().open_task_window(t.id);
                        return;
                      } catch (_) {}
                    }
                    setSelectedId(t.id);
                  }}
                  title="Click to open details">
              {t.text}
              {t.description && t.description.trim() && (
                <span className="task-has-notes" title="Has notes">·</span>
              )}
            </span>
            <button className="task-delete" onClick={() => remove(t.id)}
                    aria-label="Delete">×</button>
          </div>
        ))}
      </div>

      {total > 0 && (
        <div style={{
          marginTop: 12, padding: '10px 4px 0',
          borderTop: '1px solid rgba(255,255,255,0.14)',
          display: 'flex', justifyContent: 'space-between',
          fontSize: 11, color: 'var(--ink-mute)',
        }}>
          <span>{remaining} of {total} remaining</span>
          {total - remaining > 0 && (
            <span onClick={clearDone}
                  style={{ cursor: 'pointer', color: 'var(--mint)' }}>
              clear completed
            </span>
          )}
        </div>
      )}
    </Panel>
  );
}

// ─── Task detail editor ───────────────────────────────────────────────
function TaskDetail({ task, onChange, onBack, onDelete }) {
  const [title, setTitle] = useState(task.text);
  const [desc,  setDesc]  = useState(task.description || '');
  const [savedAt, setSavedAt] = useState(null);
  const debounceRef = useRef(null);
  const pendingRef  = useRef(null);

  // Re-sync local state if the underlying task swaps out (e.g., poll
  // reload from MCP). Only when the id changes — don't clobber typing.
  useEffect(() => {
    setTitle(task.text);
    setDesc(task.description || '');
  }, [task.id]);

  const scheduleSave = (patch) => {
    pendingRef.current = { ...(pendingRef.current || {}), ...patch };
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      const p = pendingRef.current;
      pendingRef.current = null;
      debounceRef.current = null;
      if (p) {
        onChange(p);
        setSavedAt(Date.now());
      }
    }, 350);
  };

  // Flush any pending edit on unmount or back-navigation so nothing is lost
  const flush = () => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = null;
    if (pendingRef.current) {
      onChange(pendingRef.current);
      pendingRef.current = null;
    }
  };
  useEffect(() => flush, []);  // run on unmount

  const onBackClick = () => { flush(); onBack(); };

  const onTitleChange = (e) => {
    const v = e.target.value;
    setTitle(v);
    scheduleSave({ text: v });
  };
  const onDescChange = (e) => {
    const v = e.target.value;
    setDesc(v);
    scheduleSave({ description: v });
  };
  const toggleDone = () => {
    flush();
    onChange({ done: !task.done });
  };

  const charCount = desc.length;
  const wordCount = desc.trim() ? desc.trim().split(/\s+/).length : 0;
  const dirty = pendingRef.current != null;

  return (
    <div className="task-detail">
      <div className="task-detail-head">
        <button className="td-back" onClick={onBackClick} title="Back">‹</button>
        <button className={`td-check ${task.done ? 'done' : ''}`}
                onClick={toggleDone}
                title={task.done ? 'Mark as not done' : 'Mark as done'}>
          {task.done ? '✓' : ''}
        </button>
        <span className="td-meta-status">
          {dirty ? 'editing…' : savedAt ? 'saved' : ''}
        </span>
        <button className="td-delete"
                onClick={() => {
                  if (confirm('Delete this task?')) onDelete();
                }}
                title="Delete task">×</button>
      </div>

      <input
        className={`td-title ${task.done ? 'done' : ''}`}
        value={title}
        onChange={onTitleChange}
        placeholder="Task title…"
        autoFocus
      />

      <textarea
        className="td-desc"
        value={desc}
        onChange={onDescChange}
        placeholder="Add details, notes, links — anything that helps you remember the context. Auto-saves as you type."
      />

      <div className="td-foot">
        <span>{wordCount} words · {charCount} chars</span>
      </div>
    </div>
  );
}

// ─── Claude Code instances tab ────────────────────────────────────────
// Aggregate usage stats from all chat sessions for the user-pill popover.
function aggregateUsage(codeSessions) {
  let activeSessions = 0;
  let totalReplies  = 0;
  let totalTokens   = 0;
  let totalDurationMs = 0;
  for (const c of (codeSessions || [])) {
    if ((c.messages || []).length > 0) activeSessions += 1;
    for (const m of (c.messages || [])) {
      if (m.role === 'assistant') {
        totalReplies += 1;
        if (m.meta?.output_tokens) totalTokens     += m.meta.output_tokens;
        if (m.meta?.duration_ms)   totalDurationMs += m.meta.duration_ms;
      }
    }
  }
  return { activeSessions, totalReplies, totalTokens, totalDurationMs };
}

function ClaudeTab() {
  const s = usePCStore();
  const [groups, setGroups] = useState([]);
  const [openTags, setOpenTags] = useState({});  // groupId -> bool
  const [subTab, setSubTab] = useState('code');   // 'chat' | 'cowork' | 'code'
  const [usageOpen, setUsageOpen] = useState(false);
  const [coworkDraft, setCoworkDraft] = useState('');
  const [coworkSending, setCoworkSending] = useState(false);
  // Flat list of recent Claude chats — mirrors the Claude desktop sidebar
  const [claudeChats, setClaudeChats] = useState([]);

  useEffect(() => {
    let cancelled = false;
    const reload = async () => {
      if (cancelled) return;
      const fn = api().get_claude_chats || api().get_claude_projects;
      if (!fn) return;
      try {
        const rows = await fn(30);
        if (!cancelled) setClaudeChats(Array.isArray(rows) ? rows : []);
      } catch (_) {}
    };
    reload();
    const id = setInterval(reload, 8000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Close the user-pill usage popover on outside click / Escape
  useEffect(() => {
    if (!usageOpen) return;
    const onDown = (e) => {
      if (e.target.closest('.usage-popover, .ca-user')) return;
      setUsageOpen(false);
    };
    const onKey = (e) => { if (e.key === 'Escape') setUsageOpen(false); };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [usageOpen]);

  // Same poll as Tasks — pick up MCP-driven changes from Claude
  useEffect(() => {
    let cancelled = false;
    const reload = () => {
      if (cancelled) return;
      if (api().load_claude_groups) {
        api().load_claude_groups().then((g) => {
          if (!cancelled) {
            const arr = Array.isArray(g) ? g : [];
            setGroups(arr);
            // Auto-expand all tags by default for sidebar visibility
            setOpenTags((cur) => {
              const next = { ...cur };
              arr.forEach((grp) => { if (next[grp.id] === undefined) next[grp.id] = true; });
              return next;
            });
          }
        });
      }
    };
    reload();
    const id = setInterval(reload, 3000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const persist = (next) => {
    setGroups(next);
    if (api().save_claude_groups) api().save_claude_groups(next);
  };

  const toggleTag = (id) => setOpenTags((c) => ({ ...c, [id]: !c[id] }));

  const addGroup = () => {
    const name = (prompt('Tag name (e.g. "Work", "Side project")') || '').trim();
    if (!name) return;
    const g = { id: Date.now(), name, instances: [] };
    persist([...groups, g]);
    setOpenTags((c) => ({ ...c, [g.id]: true }));
  };

  const removeGroup = (id, e) => {
    e?.stopPropagation();
    if (!confirm('Delete this tag and its instances?')) return;
    persist(groups.filter((g) => g.id !== id));
  };

  const addInstance = async (groupId, e) => {
    e?.stopPropagation();
    if (!api().pick_directory) return;
    const dir = await api().pick_directory();
    if (!dir) return;
    const name = (prompt('Instance name', dir.split(/[\\/]/).filter(Boolean).pop() || 'claude') || '').trim();
    if (!name) return;
    persist(groups.map((g) =>
      g.id === groupId
        ? { ...g, instances: [...g.instances, { id: Date.now(), name, dir }] }
        : g
    ));
    setOpenTags((c) => ({ ...c, [groupId]: true }));
  };

  const removeInstance = (groupId, instId, e) => {
    e?.stopPropagation();
    persist(groups.map((g) =>
      g.id === groupId
        ? { ...g, instances: g.instances.filter((i) => i.id !== instId) }
        : g
    ));
  };

  // Click on an instance: spawns a terminal if needed, then makes it the
  // visible "screen" in the main pane. Sidebar-app pattern.
  const activate = async (dir, name, groupId, instanceId) => {
    const cur = PCStore.state.terminals || [];
    const existing = cur.find((t) => t.instanceId === instanceId);
    if (existing) {
      PCStore.set({ activeTerminalId: existing.id });
      return;
    }
    if (!api().start_terminal) {
      api().launch_claude_instance?.(dir, name);
      return;
    }
    try {
      const r = await api().start_terminal(dir, name);
      if (r && r.ok) {
        const term = {
          id: r.id, instanceId, name: r.name || name,
          cwd: r.cwd || dir, ws_url: r.ws_url, groupId,
        };
        PCStore.set({
          terminals: [...cur, term],
          activeTerminalId: r.id,
        });
      } else {
        console.error('start_terminal failed:', r);
        api().launch_claude_instance?.(dir, name);
      }
    } catch (e) {
      console.error(e);
      api().launch_claude_instance?.(dir, name);
    }
  };

  const stopForInstance = async (instanceId, e) => {
    e?.stopPropagation();
    const cur = PCStore.state.terminals || [];
    const t = cur.find((x) => x.instanceId === instanceId);
    if (!t) return;
    try { await api().stop_terminal?.(t.id); } catch (_) {}
    const next = cur.filter((x) => x.id !== t.id);
    PCStore.set({
      terminals: next,
      activeTerminalId: PCStore.state.activeTerminalId === t.id
        ? (next[next.length - 1]?.id || null)
        : PCStore.state.activeTerminalId,
    });
  };

  // Click → open a dedicated chat WINDOW for this instance (separate from
  // the dock). The new window resumes the saved claude session id if any,
  // so the conversation continues across reopens.
  const openInstance = async (inst) => {
    if (api().open_chat_window) {
      try {
        await api().open_chat_window(inst.id, inst.dir, inst.name);
        return;
      } catch (e) {
        console.error('open_chat_window failed:', e);
      }
    }
    // Fallback: in-dock chat view if the API is missing
    const cur = PCStore.state.codeSessions || [];
    let session = cur.find((c) => c.id === inst.id);
    if (!session) {
      session = {
        id: inst.id, name: inst.name, cwd: inst.dir,
        claudeSessionId: null, messages: [],
      };
      PCStore.set({ codeSessions: [...cur, session] });
    }
    PCStore.set({ activeChatId: inst.id, ccView: 'chat' });
  };

  // + New session: pick directory, name, drop into the most recent tag (or
  // create a default "Recents" tag if there are none yet).
  const newSession = async () => {
    if (!api().pick_directory) return;
    const dir = await api().pick_directory();
    if (!dir) return;
    const guess = dir.split(/[\\/]/).filter(Boolean).pop() || 'session';
    const name = (prompt('Session name', guess) || '').trim();
    if (!name) return;
    let target = groups[groups.length - 1];
    let nextGroups = groups;
    if (!target) {
      target = { id: Date.now(), name: 'Recents', instances: [] };
      nextGroups = [target];
    }
    const inst = { id: Date.now() + 1, name, dir };
    nextGroups = nextGroups.map((g) =>
      g.id === target.id
        ? { ...g, instances: [...g.instances, inst] }
        : g
    );
    persist(nextGroups);
    // Auto-launch and view
    await openInstance({ ...inst, groupId: target.id });
  };

  // Find the currently-active instance + group based on activeTerminalId
  const activeTerm = (s.terminals || []).find((t) => t.id === s.activeTerminalId);
  const allInstances = groups.flatMap((g) =>
    g.instances.map((i) => ({ ...i, groupId: g.id, groupName: g.name }))
  );
  const activeInstance = activeTerm
    ? allInstances.find((i) => i.id === activeTerm.instanceId)
    : null;

  // Cowork: open one chat WebSocket per instance and broadcast the same
  // prompt to all of them in parallel. Each instance keeps its own session
  // continuity (we --resume their saved claude_session_id when present).
  const coworkBroadcast = async () => {
    const text = coworkDraft.trim();
    if (!text || !allInstances.length || coworkSending) return;
    setCoworkSending(true);
    try {
      const tasks = allInstances.map(async (inst) => {
        const prior = (api().get_chat_session_id
          ? await api().get_chat_session_id(inst.id).catch(() => null)
          : null) || null;
        return new Promise((resolve) => {
          let ws;
          let timeout;
          try {
            ws = new WebSocket(`ws://127.0.0.1:7654/chat/${inst.id}`);
          } catch (_) { resolve(); return; }
          ws.onopen = () => {
            ws.send(JSON.stringify({
              type: 'msg', text,
              cwd: inst.dir,
              prior_session_id: prior,
              accept_edits: true,
            }));
            timeout = setTimeout(() => { try { ws.close(); } catch (_) {} resolve(); }, 180000);
          };
          ws.onmessage = (ev) => {
            try {
              const data = JSON.parse(ev.data);
              if (data.type === 'session' && data.session_id
                  && api().update_chat_session_id) {
                try { api().update_chat_session_id(inst.id, data.session_id); } catch (_) {}
              }
              if (data.type === 'done') {
                clearTimeout(timeout);
                try { ws.close(); } catch (_) {}
                resolve();
              }
            } catch (_) {}
          };
          ws.onerror = () => { clearTimeout(timeout); resolve(); };
          ws.onclose = () => { clearTimeout(timeout); resolve(); };
        });
      });
      await Promise.all(tasks);
    } finally {
      setCoworkSending(false);
      setCoworkDraft('');
    }
  };

  // Aggregated usage for the user-pill popover
  const usage = aggregateUsage(s.codeSessions);

  // Open a chat window pinned to a real Claude Code session, resumed at
  // its existing claude session id (so claude -p --resume picks up the
  // exact transcript). We pass the session id explicitly so the Python
  // side bakes it straight into the URL hash — no disk-write race with
  // chat_sessions.json.
  const openClaudeChat = (chat) => {
    const chatId = chat.id;  // session uuid IS the chat window id
    if (api().open_chat_window) {
      try {
        api().open_chat_window(chatId, chat.cwd, chat.project, chat.id);
        return;
      } catch (_) {}
    }
    // Fallback: in-dock chat view
    const cur = PCStore.state.codeSessions || [];
    let session = cur.find((c) => String(c.id) === String(chatId));
    if (!session) {
      session = {
        id: chatId, name: chat.project, cwd: chat.cwd,
        claudeSessionId: chat.id, messages: [],
      };
      PCStore.set({ codeSessions: [...cur, session] });
    }
    PCStore.set({ activeChatId: chatId, ccView: 'chat' });
  };

  const fmtRelative = (ts) => {
    if (!ts) return '';
    const sec = (Date.now() / 1000 - ts);
    if (sec < 60) return 'just now';
    if (sec < 3600) return `${Math.floor(sec/60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec/3600)}h ago`;
    if (sec < 86400*7) return `${Math.floor(sec/86400)}d ago`;
    return `${Math.floor(sec/86400/7)}w ago`;
  };

  // ─── Chat screen (when an instance is opened) ───
  if (s.ccView === 'chat' && s.activeChatId) {
    const chat = (s.codeSessions || []).find((c) => c.id === s.activeChatId);
    const inst = allInstances.find((i) => i.id === s.activeChatId);
    if (chat && inst) {
      return (
        <Panel title="Claude Code" icon="◆" action={false}>
          <ChatPane chat={chat} inst={inst}
                    onBack={() => PCStore.set({ ccView: 'navigator' })} />
        </Panel>
      );
    }
  }

  // ─── Sidebar navigator (default view, mirrors Claude desktop app) ───
  const username = window.PC_USER || 'you';
  return (
    <Panel title="Claude Code" icon="◆" action={false}>
      <div className="ca-shell">
        {/* Sub-tabs */}
        <div className="ca-tabs">
          <div className={`ca-tab ${subTab === 'chat' ? 'active' : ''}`}
               onClick={() => setSubTab('chat')}
               title="Voice-style back-and-forth chat (uses the Voice tab pipeline)">
            <span>💬</span>Chat
          </div>
          <div className={`ca-tab ${subTab === 'cowork' ? 'active' : ''}`}
               onClick={() => setSubTab('cowork')}
               title="Broadcast one prompt to every Recent in parallel">
            <span>⊟</span>Cowork
          </div>
          <div className={`ca-tab ${subTab === 'code' ? 'active' : ''}`}
               onClick={() => setSubTab('code')}
               title="Per-session Claude Code chat windows">
            <span>{'⟨/⟩'}</span>Code
          </div>
        </div>

        {subTab === 'chat' && (
          <CoworkChatRedirect />
        )}

        {subTab === 'cowork' && (
          <CoworkPanel
            instances={allInstances}
            terminals={s.terminals || []}
            draft={coworkDraft}
            setDraft={setCoworkDraft}
            sending={coworkSending}
            onBroadcast={coworkBroadcast}
            onOpen={openInstance}
          />
        )}

        {subTab === 'code' && (
          <>
            {/* Action items */}
            <div className="ca-actions">
              <div className="ca-action" onClick={newSession}
                   title="Pick a folder + name to start a new Claude Code session">
                <span className="ic">+</span>New session
              </div>
              <div className="ca-action" onClick={addGroup}
                   title="Create a tag to group related sessions">
                <span className="ic">⚡</span>Routines
              </div>
              <div className="ca-action"
                   onClick={() => alert('Customize: nothing here yet — let me know what you want.')}
                   title="Customize the dock (placeholder)">
                <span className="ic">⚙</span>Customize
              </div>
              <div className="ca-action"
                   onClick={() => setOpenTags((c) => ({ ...c, _more: !c._more }))}
                   title="Show / hide extra options">
                <span className="ic">∨</span>More
              </div>
            </div>

            {/* Pinned (placeholder) */}
            <div className="ca-section">
              <div className="ca-section-head">Pinned</div>
              <div className="ca-pinned-empty">
                <span style={{ width: 14, textAlign: 'center' }}>📌</span>
                Drag to pin
              </div>
            </div>

            {/* Recents — flat list of chats, latest first (Claude desktop style) */}
            <div className="ca-section">
              <div className="ca-section-head">Recents</div>
              {claudeChats.length === 0 && (
                <div style={{ padding: '6px 24px', fontSize: 12,
                              color: 'var(--ink-faint)' }}>
                  No Claude chats found yet.
                </div>
              )}
              {claudeChats.map((c) => (
                <div key={c.id} className="ca-chat-row"
                     onClick={() => openClaudeChat(c)}
                     title={`${c.title}\n\n${c.project} · ${fmtRelative(c.modified_at)}\n${c.cwd}`}>
                  <span className="ca-chat-bullet">○</span>
                  <span className="ca-chat-text">{c.title}</span>
                </div>
              ))}
            </div>

            {/* User-managed instances (kept for parity with the original
                "tags + instances" UX — these aren't tied to Claude Code's
                on-disk state) */}
            {allInstances.length > 0 && (
              <div className="ca-section">
                <div className="ca-section-head">Pinned sessions</div>
                {allInstances.map((inst) => {
                  const term = (s.terminals || []).find((t) => t.instanceId === inst.id);
                  const isActive = term && term.id === s.activeTerminalId;
                  return (
                    <div key={inst.id}
                         className={`ca-recent ${isActive ? 'active' : ''} ${term ? 'live' : ''}`}
                         onClick={() => openInstance(inst)}
                         title={`${inst.name}\n${inst.dir}\nClick to open chat window`}>
                      <span className="bullet">{isActive ? '⋯' : '○'}</span>
                      <span className="text">{inst.name}</span>
                      <span className="x"
                            onClick={(e) => term
                              ? stopForInstance(inst.id, e)
                              : removeInstance(inst.groupId, inst.id, e)}
                            title={term ? 'Stop terminal' : 'Remove from list'}>
                        {term ? '■' : '×'}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </>
        )}

        {/* User pill at the bottom — click for usage popover */}
        <div className="ca-user"
             onClick={() => setUsageOpen((v) => !v)}
             title="Click to see usage stats">
          <div className="avatar"></div>
          <div className="name">
            {username}<span className="badge">· Max</span>
          </div>
          <span className="ic">⌃</span>

          {usageOpen && (
            <div className="usage-popover" onClick={(e) => e.stopPropagation()}>
              <div className="usage-head">
                <span className="usage-name">{username}</span>
                <span className="usage-plan">Max plan</span>
              </div>
              <div className="usage-grid">
                <div className="usage-cell">
                  <span className="k">Active sessions</span>
                  <span className="v">{usage.activeSessions}</span>
                </div>
                <div className="usage-cell">
                  <span className="k">Replies</span>
                  <span className="v">{usage.totalReplies}</span>
                </div>
                <div className="usage-cell">
                  <span className="k">Output tokens</span>
                  <span className="v">{fmtTokens(usage.totalTokens) || '0'}</span>
                </div>
                <div className="usage-cell">
                  <span className="k">Thinking time</span>
                  <span className="v">{fmtDuration(usage.totalDurationMs) || '0s'}</span>
                </div>
              </div>
              <div className="usage-foot">
                Stats reset when sessions are cleared.
              </div>
            </div>
          )}
        </div>
      </div>
    </Panel>
  );
}

// ─── Cowork sub-tab — broadcast one prompt to every Recent in parallel ─
function CoworkPanel({ instances, terminals, draft, setDraft,
                      sending, onBroadcast, onOpen }) {
  const liveById = {};
  for (const t of terminals) liveById[t.instanceId] = t;
  return (
    <div className="ca-cowork">
      <div className="cowork-head">
        <div>
          <div className="cowork-title">Cowork</div>
          <div className="cowork-sub">
            Broadcast one prompt to all {instances.length} session{instances.length === 1 ? '' : 's'} at once.
          </div>
        </div>
      </div>

      <div className="cowork-composer">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
              e.preventDefault(); onBroadcast();
            }
          }}
          placeholder={instances.length
            ? "Same prompt → every Recent. Ctrl/⌘+Enter to send."
            : "Add Recents first via Code tab → New session."}
          rows={3}
          disabled={sending || instances.length === 0}
        />
        <button className="cowork-send"
                onClick={onBroadcast}
                disabled={!draft.trim() || sending || instances.length === 0}>
          {sending ? `Broadcasting to ${instances.length}…` : `Broadcast to ${instances.length}`}
        </button>
      </div>

      <div className="cowork-grid">
        {instances.map((inst) => {
          const live = !!liveById[inst.id];
          return (
            <div key={inst.id}
                 className={`cowork-card ${live ? 'live' : ''}`}
                 onClick={() => onOpen(inst)}
                 title={`${inst.name}\n${inst.dir}\nClick to open chat`}>
              <div className="cowork-name">
                <span className="dot" />
                {inst.name}
              </div>
              <div className="cowork-dir">{inst.dir}</div>
              <div className="cowork-status">
                {sending ? 'sending…' : (live ? 'terminal live' : 'idle')}
              </div>
            </div>
          );
        })}
        {instances.length === 0 && (
          <div className="cowork-empty">
            No sessions yet. Open the Code tab and create one to broadcast across.
          </div>
        )}
      </div>
    </div>
  );
}

// Chat sub-tab is just a redirect prompt — the real chat lives in Voice
function CoworkChatRedirect() {
  return (
    <div className="cowork-redirect">
      <div className="cowork-empty">
        Conversational chat with mascot + voice lives in the
        <strong style={{ color: 'var(--ink)' }}> Voice </strong>
        tab.
        <br/><br/>
        <button className="action-primary"
                onClick={() => {
                  // Switch the dock to the Voice tab via the App's tab state
                  // (set the URL hash and dispatch — App reads on mount, but
                  // here we cheat by simulating a click on the Voice tab)
                  const tabs = document.querySelectorAll('.tabs .tab');
                  for (const t of tabs) {
                    if (t.textContent.trim().toLowerCase() === 'voice') {
                      t.click(); break;
                    }
                  }
                }}>
          Open Voice tab →
        </button>
      </div>
    </div>
  );
}

// ─── Terminal + Code-session state in the store ──────────────────────
PCStore.state = {
  ...PCStore.state,
  terminals: [],            // [{id, instanceId, name, cwd, ws_url, groupId}, ...]
  activeTerminalId: null,   // single global active terminal
  ccView: 'navigator',      // 'navigator' | 'chat' | 'terminal'
  codeSessions: [],         // [{id, name, cwd, claudeSessionId, messages: [...]}, ...]
  activeChatId: null,       // active session in chat view
};

// ─── Collapsible "Edited a file, ran 2 commands ›" pill ──────────────
function ActionsPill({ m }) {
  const [open, setOpen] = useState(false);
  const details = Array.isArray(m.details) ? m.details : [];
  return (
    <div className={`cc-actions-pill ${open ? 'open' : ''}`}>
      <button type="button"
              className="cc-actions-summary"
              onClick={() => setOpen((v) => !v)}>
        <span className="ap-caret">›</span>
        <span className="ap-summary">{m.summary || 'Used tools'}</span>
        {details.length > 0 && (
          <span className="ap-count">{details.length}</span>
        )}
      </button>
      {open && details.length > 0 && (
        <div className="ap-details">
          {details.map((d, j) => (
            <div key={j} className="ap-item">{d}</div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── ChatPane — Claude-Code-style session UI with streaming ───────────
// Available models for the composer's model picker. Display label →
// CLI argument passed via `--model`.
const CHAT_MODELS = [
  { id: 'opus',   label: 'Opus 4.7',
    tier: 'Max',  desc: 'Most capable — best for hard reasoning + multi-file edits.' },
  { id: 'sonnet', label: 'Sonnet 4.5',
    tier: 'Pro',  desc: 'Fast + cheaper — great default for everyday coding.' },
  { id: 'haiku',  label: 'Haiku',
    tier: 'Pro',  desc: 'Fastest + cheapest — quick tweaks and small lookups.' },
];

// Compact duration formatter — "9s", "1m 19s", "2h 4m"
function fmtDuration(ms) {
  if (ms == null || ms < 0) return '';
  const sec = Math.round(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  const rs  = sec % 60;
  if (min < 60) return `${min}m ${rs}s`;
  const hr  = Math.floor(min / 60);
  const rm  = min % 60;
  return `${hr}h ${rm}m`;
}

function fmtTokens(n) {
  if (n == null) return '';
  if (n < 1000) return String(n);
  if (n < 100000) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k`;
  return `${Math.round(n / 1000)}k`;
}

function ChatPane({ chat, inst, onBack }) {
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [streamingText, setStreamingText] = useState('');
  const [thinkVerb, setThinkVerb] = useState('Thinking');
  // Default OFF — auto-accepting edits in a freshly opened Recent could
  // overwrite real source files before the user has reviewed the prompt.
  // User toggles it on with the ✓ button when they want bypass-permission.
  const [acceptEdits, setAcceptEdits] = useState(false);
  const [modelId, setModelId] = useState('opus');
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [moreMenuOpen, setMoreMenuOpen] = useState(false);
  const [recording, setRecording] = useState(false);
  const [gitInfo, setGitInfo] = useState(null);
  const bodyRef = useRef(null);
  const taRef = useRef(null);
  const wsRef = useRef(null);
  const streamRef = useRef('');  // accumulator (avoids stale-closure issues)
  const recRef = useRef(null);
  const messages = chat.messages || [];
  const model = CHAT_MODELS.find((m) => m.id === modelId) || CHAT_MODELS[0];

  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, sending, streamingText]);

  // Cycle thinking verbs while waiting for the first token
  useEffect(() => {
    if (!sending || streamingText) return;
    const pick = () => setThinkVerb(
      THINKING_VERBS[Math.floor(Math.random() * THINKING_VERBS.length)]
    );
    pick();
    const id = setInterval(pick, 1300);
    return () => clearInterval(id);
  }, [sending, streamingText]);

  // Open / reuse a chat WebSocket scoped to this code session
  const ensureWs = () => new Promise((resolve, reject) => {
    let ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) return resolve(ws);
    ws = new WebSocket(`ws://127.0.0.1:7654/chat/${chat.id}`);
    wsRef.current = ws;
    ws.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch { return; }
      if (data.type === 'delta') {
        streamRef.current += data.text || '';
        setStreamingText(streamRef.current);
      } else if (data.type === 'done') {
        const final = streamRef.current;
        streamRef.current = '';
        setStreamingText('');
        setSending(false);
        const sid = data.session_id;
        const meta = (data.duration_ms != null || data.output_tokens != null)
          ? { duration_ms: data.duration_ms,
              output_tokens: data.output_tokens }
          : null;
        const cur = PCStore.state.codeSessions || [];
        PCStore.set({
          codeSessions: cur.map((c) => c.id === chat.id ? {
            ...c,
            claudeSessionId: sid || c.claudeSessionId,
            messages: final
              ? [...(c.messages || []),
                  { role: 'assistant', text: final, meta }]
              : (c.messages || []),
          } : c),
        });
        // Persist session id so the next time this chat opens we can --resume
        if (sid && api().update_chat_session_id) {
          try { api().update_chat_session_id(chat.id, sid); } catch (_) {}
        }
      } else if (data.type === 'session') {
        const cur = PCStore.state.codeSessions || [];
        PCStore.set({
          codeSessions: cur.map((c) => c.id === chat.id
            ? { ...c, claudeSessionId: data.session_id || c.claudeSessionId }
            : c),
        });
        if (data.session_id && api().update_chat_session_id) {
          try { api().update_chat_session_id(chat.id, data.session_id); } catch (_) {}
        }
      } else if (data.type === 'actions') {
        // Tool-use summary for the latest assistant turn — render as a
        // collapsible pill below the streaming text.
        const cur = PCStore.state.codeSessions || [];
        PCStore.set({
          codeSessions: cur.map((c) => c.id === chat.id ? {
            ...c,
            messages: [...(c.messages || []), {
              role: 'actions',
              summary: data.summary || 'Used tools',
              details: data.details || [],
            }],
          } : c),
        });
      } else if (data.type === 'error') {
        const cur = PCStore.state.codeSessions || [];
        PCStore.set({
          codeSessions: cur.map((c) => c.id === chat.id ? {
            ...c,
            messages: [...(c.messages || []), { role: 'system', text: data.error || 'error' }],
          } : c),
        });
        streamRef.current = '';
        setStreamingText('');
        setSending(false);
      }
    };
    ws.onerror = (e) => reject(e);
    ws.onopen = () => resolve(ws);
    ws.onclose = () => {
      if (wsRef.current === ws) wsRef.current = null;
    };
  });

  // Cleanup the WS when this pane unmounts
  useEffect(() => () => {
    try { wsRef.current?.close(); } catch (_) {}
    try { recRef.current?.abort?.(); } catch (_) {}
  }, []);

  // Poll git status for the branch / diff badge / PR button
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      if (!api().git_status || !inst.dir) return;
      try {
        const r = await api().git_status(inst.dir);
        if (!cancelled) setGitInfo(r && r.ok ? r : null);
      } catch (_) {
        if (!cancelled) setGitInfo(null);
      }
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [inst.dir]);

  // Close popovers on outside-click / Escape
  useEffect(() => {
    if (!modelMenuOpen && !moreMenuOpen) return;
    const onDown = (e) => {
      if (e.target.closest('.cc-popover, .model-trigger, .more-trigger')) return;
      setModelMenuOpen(false); setMoreMenuOpen(false);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { setModelMenuOpen(false); setMoreMenuOpen(false); }
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [modelMenuOpen, moreMenuOpen]);

  // ───── Composer button handlers ─────
  const onCreatePR = async () => {
    if (!api().git_create_pr) return;
    const r = await api().git_create_pr(inst.dir);
    if (!r?.ok) {
      const cur = PCStore.state.codeSessions || [];
      PCStore.set({
        codeSessions: cur.map((c) => c.id === chat.id ? {
          ...c,
          messages: [...(c.messages || []), {
            role: 'system',
            text: r?.error || 'Create PR failed',
          }],
        } : c),
      });
    }
  };

  const onAttach = async () => {
    if (!api().pick_file) return;
    let path;
    try { path = await api().pick_file(inst.dir); } catch (_) { return; }
    if (!path) return;
    // Quote paths with spaces; Claude's `@`-reference handles either form
    const ref = path.includes(' ') ? `@"${path}"` : `@${path}`;
    setDraft((d) => d ? `${d} ${ref}` : ref);
    setTimeout(() => taRef.current?.focus(), 0);
  };

  const onMic = () => {
    // Stop if already recording (toggle)
    if (recording && recRef.current) {
      try { recRef.current.stop(); } catch (_) {}
      return;
    }
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      const cur = PCStore.state.codeSessions || [];
      PCStore.set({
        codeSessions: cur.map((c) => c.id === chat.id ? {
          ...c,
          messages: [...(c.messages || []), {
            role: 'system',
            text: 'Speech recognition unavailable in this WebView',
          }],
        } : c),
      });
      return;
    }
    const rec = new SR();
    rec.lang = 'en-US';
    rec.continuous = false;
    rec.interimResults = false;
    rec.maxAlternatives = 1;
    rec.onresult = (e) => {
      try {
        const text = (e.results[0][0].transcript || '').trim();
        if (text) setDraft((d) => d ? `${d} ${text}` : text);
      } catch (_) {}
    };
    rec.onend = () => { setRecording(false); recRef.current = null; };
    rec.onerror = () => { setRecording(false); recRef.current = null; };
    recRef.current = rec;
    setRecording(true);
    try { rec.start(); } catch (_) { setRecording(false); }
  };

  const onMoreAction = (action) => {
    setMoreMenuOpen(false);
    if (action === 'terminal') {
      api().launch_claude_instance?.(inst.dir, inst.name);
    } else if (action === 'clear') {
      const cur = PCStore.state.codeSessions || [];
      PCStore.set({
        codeSessions: cur.map((c) => c.id === chat.id
          ? { ...c, messages: [], claudeSessionId: null }
          : c),
      });
      if (api().update_chat_session_id) {
        try { api().update_chat_session_id(chat.id, null); } catch (_) {}
      }
    } else if (action === 'copy_id') {
      const sid = chat.claudeSessionId || '';
      if (sid && navigator.clipboard) {
        navigator.clipboard.writeText(sid).catch(() => {});
      }
    }
  };

  const send = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    streamRef.current = '';
    setStreamingText('');
    setDraft('');
    if (taRef.current) taRef.current.style.height = 'auto';

    const cur = PCStore.state.codeSessions || [];
    PCStore.set({
      codeSessions: cur.map((c) => c.id === chat.id
        ? { ...c, messages: [...(c.messages || []), { role: 'user', text }] }
        : c),
    });

    try {
      const ws = await ensureWs();
      ws.send(JSON.stringify({
        type: 'msg', text,
        cwd: inst.dir,
        prior_session_id: chat.claudeSessionId || null,
        accept_edits: acceptEdits,
        model: modelId,
      }));
    } catch (e) {
      const cur2 = PCStore.state.codeSessions || [];
      PCStore.set({
        codeSessions: cur2.map((c) => c.id === chat.id
          ? { ...c, messages: [...(c.messages || []),
                                { role: 'system', text: 'connection error' }] }
          : c),
      });
      setSending(false);
    }
  };

  const onKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const onInput = (e) => {
    setDraft(e.target.value);
    const ta = e.target;
    ta.style.height = 'auto';
    ta.style.height = Math.min(120, ta.scrollHeight) + 'px';
  };

  const parts = (inst.dir || '').split(/[\\/]/).filter(Boolean);
  const folder = parts[parts.length - 1] || inst.dir;

  return (
    <div className="cc-chat">
      <div className="cc-chat-head">
        <button className="back" onClick={onBack} title="Back">‹</button>
        <span className="path">
          <span className="folder">📁</span>
          <span style={{ color: 'var(--ink-soft)' }}>{folder}</span>
          <span className="sep">/</span>
          <span>{chat.name}</span>
          <span className="caret">▾</span>
        </span>
        <span className="more" title="Session info">⋯</span>
      </div>

      <div className="cc-chat-body" ref={bodyRef}>
        {messages.length === 0 && !sending && (
          <div className="cc-chat-empty">
            Send a message to start this Claude Code session in
            <br/>
            <span style={{ color: 'var(--ink-soft)' }}>{inst.dir}</span>
          </div>
        )}
        {messages.map((m, i) => {
          if (m.role === 'actions') {
            return <ActionsPill key={i} m={m} />;
          }
          return <div key={i} className={`cc-msg ${m.role}`}>{m.text}</div>;
        })}
        {streamingText && (
          <div className="cc-msg assistant streaming">{streamingText}</div>
        )}
      </div>

      {/* Status pill — lives between body and composer.
          Shows the thinking verb while a response is in flight, then
          flips to the duration / token meta of the latest reply. */}
      {(() => {
        let lastMeta = null;
        for (let i = messages.length - 1; i >= 0; i--) {
          if (messages[i].role === 'assistant' && messages[i].meta) {
            lastMeta = messages[i].meta; break;
          }
        }
        const pending = sending && !streamingText;
        if (!pending && !lastMeta) return null;
        return (
          <div className={`cc-chat-status ${pending ? 'pending' : 'meta'}`}>
            {pending ? (
              <>
                <span>{thinkVerb}…</span>
                <span className="dots">
                  <span className="dot"/><span className="dot"/><span className="dot"/>
                </span>
              </>
            ) : (
              <>
                <span className="meta-spark" aria-hidden>✱</span>
                {lastMeta.duration_ms != null && (
                  <span>{fmtDuration(lastMeta.duration_ms)}</span>
                )}
                {lastMeta.duration_ms != null
                 && lastMeta.output_tokens != null && (
                  <span className="meta-dot">·</span>
                )}
                {lastMeta.output_tokens != null && (
                  <span>↓{fmtTokens(lastMeta.output_tokens)} tokens</span>
                )}
              </>
            )}
          </div>
        );
      })()}

      <div className="cc-chat-composer">
        {/* Branch / PR row — live `git` data, only shown for git repos */}
        {gitInfo && (
        <div className="cc-composer-branch">
          <span className="branch-pill" title={`HEAD: ${gitInfo.branch}\nBase: ${gitInfo.base}${gitInfo.dirty ? '\n(uncommitted changes)' : ''}`}>
            <svg className="git-icon" width="13" height="13" viewBox="0 0 16 16"
                 fill="none" stroke="currentColor" strokeWidth="1.6"
                 strokeLinecap="round" strokeLinejoin="round">
              <circle cx="4" cy="3.5"  r="1.4" />
              <circle cx="4" cy="12.5" r="1.4" />
              <circle cx="12" cy="8"   r="1.4" />
              <path d="M4 5 V11" />
              <path d="M4 8 H8 a3 3 0 0 0 3 -3 V4" />
            </svg>
            <span>{gitInfo.branch}</span>
            <span className="branch-arrow">←</span>
            <span>{gitInfo.base}</span>
            {gitInfo.dirty && <span className="dirty-dot" title="Uncommitted changes" />}
          </span>
          {(gitInfo.added > 0 || gitInfo.deleted > 0) && (
            <span className="diff-badge"
                  title={`${gitInfo.files_changed} file${gitInfo.files_changed === 1 ? '' : 's'} changed`}>
              <span className="add">+{gitInfo.added}</span>
              <span className="del">-{gitInfo.deleted}</span>
            </span>
          )}
          <button className="pr-btn"
                  onClick={onCreatePR}
                  disabled={!gitInfo.has_gh || gitInfo.on_base}
                  style={(gitInfo.added === 0 && gitInfo.deleted === 0)
                          ? { marginLeft: 'auto' } : null}
                  title={!gitInfo.has_gh
                    ? 'Install GitHub CLI (gh) to create PRs'
                    : gitInfo.on_base
                      ? 'You\'re on the base branch — switch to a feature branch first'
                      : 'Open PR creation in browser'}>
            <span>Create PR</span>
            <span className="pr-caret">▾</span>
          </button>
        </div>
        )}

        {/* Input row */}
        <div className="cc-composer-input">
          <textarea
            ref={taRef}
            value={draft}
            onChange={onInput}
            onKeyDown={onKey}
            placeholder="Type / for commands"
            disabled={sending}
            rows={1}
          />
          <button className="send"
                  onClick={send}
                  disabled={!draft.trim() || sending}
                  title="Send (Enter)">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none"
                 stroke="currentColor" strokeWidth="1.6"
                 strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 11 V7 a2 2 0 0 1 2 -2 H12" />
              <path d="M9 2 L12 5 L9 8" />
            </svg>
          </button>
        </div>

        {/* Bottom tools row */}
        <div className="cc-composer-tools">
          <div className="tools-left">
            <button className={`tool-btn ${acceptEdits ? 'active' : ''}`}
                    onClick={() => setAcceptEdits(!acceptEdits)}
                    title={acceptEdits
                      ? 'Auto-accept edits is ON — click to require approval'
                      : 'Auto-accept edits is OFF — click to enable'}>
              <span className="tool-icon">{acceptEdits ? '✓' : '○'}</span>
              <span>Accept edits</span>
            </button>
            <button className="tool-btn icon-only"
                    onClick={onAttach}
                    title="Attach a file (inserts @path)">+</button>
            <button className={`tool-btn icon-only ${recording ? 'recording' : ''}`}
                    onClick={onMic}
                    title={recording ? 'Stop recording' : 'Voice → text (fills input)'}>
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none"
                   stroke="currentColor" strokeWidth="1.6"
                   strokeLinecap="round" strokeLinejoin="round">
                <rect x="6" y="2" width="4" height="8" rx="2"/>
                <path d="M3 8 a5 5 0 0 0 10 0"/>
                <path d="M8 13 V15"/>
              </svg>
            </button>
            <div style={{ position: 'relative' }}>
              <button className="tool-btn icon-only more-trigger"
                      onClick={() => setMoreMenuOpen((v) => !v)}
                      title="More options">▾</button>
              {moreMenuOpen && (
                <div className="cc-popover more-popover">
                  <button onClick={() => onMoreAction('terminal')}>
                    <span className="ic">⟩_</span>Open in terminal
                  </button>
                  <button onClick={() => onMoreAction('clear')}>
                    <span className="ic">↺</span>Clear conversation
                  </button>
                  <button onClick={() => onMoreAction('copy_id')}
                          disabled={!chat.claudeSessionId}>
                    <span className="ic">⎘</span>Copy session ID
                  </button>
                </div>
              )}
            </div>
          </div>
          <div className="tools-right" style={{ position: 'relative' }}>
            <button className="model-indicator model-trigger"
                    onClick={() => setModelMenuOpen((v) => !v)}
                    title={`Currently: ${model.label} · ${model.tier}\n${model.desc}\nClick to switch model`}>
              <span>{model.label} · {model.tier}</span>
              <span className={`model-spinner ${sending ? 'on' : ''}`} aria-hidden>◐</span>
            </button>
            {modelMenuOpen && (
              <div className="cc-popover model-popover">
                {CHAT_MODELS.map((m) => (
                  <button key={m.id}
                          className={m.id === modelId ? 'active' : ''}
                          onClick={() => { setModelId(m.id); setModelMenuOpen(false); }}
                          title={m.desc}>
                    <span className="ic">{m.id === modelId ? '✓' : ' '}</span>
                    <span>{m.label}</span>
                    <span className="tier">{m.tier}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function XTerm({ term, visible }) {
  const hostRef = useRef(null);
  const xtermRef = useRef(null);
  const fitRef = useRef(null);
  const wsRef = useRef(null);
  const [status, setStatus] = useState('starting');  // starting | live | error | closed

  useEffect(() => {
    console.log('[xterm] mount', term.id, 'ws=', term.ws_url);
    if (!hostRef.current || xtermRef.current) return;
    const xterm = new window.Terminal({
      fontFamily: '"Cascadia Code", "Consolas", monospace',
      fontSize: 12,
      cursorBlink: true,
      convertEol: true,
      scrollback: 5000,
      theme: {
        background: '#0a0e1a',
        foreground: '#f4f7f6',
        cursor:     '#5eead4',
        selection:  'rgba(94,234,212,0.25)',
      },
      allowProposedApi: true,
    });
    const fit = new window.FitAddon.FitAddon();
    xterm.loadAddon(fit);
    xterm.open(hostRef.current);

    const safeFit = () => {
      try {
        const r = hostRef.current?.getBoundingClientRect();
        if (!r || r.width < 20 || r.height < 20) return;
        fit.fit();
      } catch (_) {}
    };

    // Retry fit on a schedule until container has real dimensions
    [16, 60, 150, 400, 900].forEach((d) => setTimeout(safeFit, d));
    let ro;
    if (window.ResizeObserver) {
      ro = new ResizeObserver(safeFit);
      ro.observe(hostRef.current);
    }

    const ws = new WebSocket(term.ws_url);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      console.log('[xterm] ws open', term.id);
      safeFit();
      setStatus('live');
      try { ws.send('\x1b!resize:' + xterm.cols + ',' + xterm.rows); } catch (_) {}
    };
    ws.onmessage = (ev) => {
      setStatus('live');
      const data = ev.data;
      if (data instanceof ArrayBuffer) xterm.write(new Uint8Array(data));
      else xterm.write(data);
    };
    ws.onerror = (e) => {
      console.error('[xterm] ws error', term.id, e);
      setStatus('error');
      xterm.write('\r\n\x1b[31m[connection error]\x1b[0m\r\n');
    };
    ws.onclose = (e) => {
      console.log('[xterm] ws close', term.id, e.code, e.reason);
      setStatus('closed');
      xterm.write('\r\n\x1b[33m[disconnected]\x1b[0m\r\n');
    };
    xterm.onData((d) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(d);
    });
    xterm.onResize(({ cols, rows }) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send('\x1b!resize:' + cols + ',' + rows);
      }
      api().resize_terminal?.(term.id, cols, rows);
    });

    xtermRef.current = xterm;
    fitRef.current = fit;
    wsRef.current = ws;

    const onWinResize = () => safeFit();
    window.addEventListener('resize', onWinResize);

    return () => {
      window.removeEventListener('resize', onWinResize);
      if (ro) try { ro.disconnect(); } catch (_) {}
      try { ws.close(); } catch (_) {}
      try { xterm.dispose(); } catch (_) {}
      xtermRef.current = null;
    };
  }, [term.id]);

  // Re-fit and focus when this tab becomes visible (display:none → block)
  useEffect(() => {
    if (!visible || !fitRef.current) return;
    [20, 80, 200].forEach((d) => setTimeout(() => {
      try {
        const r = hostRef.current?.getBoundingClientRect();
        if (r && r.width > 20 && r.height > 20) fitRef.current.fit();
      } catch (_) {}
    }, d));
    try { xtermRef.current?.focus(); } catch (_) {}
  }, [visible]);

  return (
    <div className="xterm-wrap"
         style={{ display: visible ? 'flex' : 'none',
                  position: 'relative',
                  flexDirection: 'column' }}>
      {status === 'starting' && (
        <div style={{
          position: 'absolute', inset: 0,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          color: 'var(--mint)', fontSize: 12, letterSpacing: '0.05em',
          background: '#0a0e1a',
          zIndex: 2,
        }}>
          ◦ starting terminal…
        </div>
      )}
      <div ref={hostRef}
           style={{ flex: 1, minHeight: 0, width: '100%' }} />
    </div>
  );
}

// ─── Notes tab ────────────────────────────────────────────────────────
function NotesTab() {
  const { notes, notesSaved } = usePCStore();
  const [text, setText] = useState(notes || '');
  const debounceRef = useRef(null);

  // Hydrate from Python on first mount
  useEffect(() => {
    if (api().load_notes) {
      api().load_notes().then((t) => {
        setText(t || '');
        PCStore.set({ notes: t || '', notesSaved: true });
      });
    }
  }, []);

  const onChange = (e) => {
    const v = e.target.value;
    setText(v);
    PCStore.set({ notesSaved: false });
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      if (api().save_notes) api().save_notes(v);
      PCStore.set({ notes: v, notesSaved: true });
    }, 500);
  };

  const chars = text.length;
  const words = text.trim() ? text.trim().split(/\s+/).length : 0;

  return (
    <Panel title="Notes" icon="✎" action={false}>
      <textarea
        className="notes-area"
        value={text}
        onChange={onChange}
        placeholder="Quick scratch pad — auto-saves as you type."
        autoFocus
      />
      <div className="notes-meta">
        <span>{words} words · {chars} chars</span>
        <span className="saved">{notesSaved ? 'saved' : 'saving…'}</span>
      </div>
    </Panel>
  );
}

// ─── App shell ────────────────────────────────────────────────────────
const TABS = [
  { id: 'voice',   label: 'Voice'   },
  { id: 'tasks',   label: 'Tasks'   },
  { id: 'claude',  label: 'Code'    },
  { id: 'monitor', label: 'Monitor' },
];

function App() {
  const [tab, setTab] = useState(() => {
    const ids = ['voice', 'tasks', 'claude', 'monitor'];
    // Read from URL hash first (set before page load), then optional global
    const m = (window.location.hash || '').match(/tab=([\w-]+)/);
    const raw = ((m && m[1]) || window.PC_INITIAL_TAB || '').toLowerCase();
    if (raw === 'code') return 'claude';
    return ids.includes(raw) ? raw : 'voice';
  });
  const s = usePCStore();
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  const timeStr = now.toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', hour12: false,
  });

  // Poll stats whenever the Monitor tab is open
  useEffect(() => {
    if (tab !== 'monitor') return;
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      try {
        const stats = await api().get_stats?.();
        if (stats && !cancelled) PCStore.setStats(stats);
      } catch (_) {}
    };
    tick();
    const id = setInterval(tick, 1500);
    return () => { cancelled = true; clearInterval(id); };
  }, [tab]);

  // Footer telemetry — small, infrequent
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      try {
        const stats = await api().get_stats?.();
        if (stats && !cancelled) PCStore.setStats(stats);
      } catch (_) {}
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const Body = {
    voice:   <VoiceTab />,
    monitor: <MonitorTab />,
    tasks:   <TasksTab />,
    claude:  <ClaudeTab />,
  }[tab];

  const onMicClick = async () => {
    if (s.listening) await api().stop_listening?.();
    else            await api().start_listening?.();
  };

  return (
    <div className="app">
      <div className="header" style={{ justifyContent: 'center', padding: '14px 14px' }}>
        <div style={{
          fontSize: 30, fontWeight: 600, letterSpacing: '-0.02em',
          color: 'var(--ink)', fontVariantNumeric: 'tabular-nums',
          textShadow: '0 1px 8px rgba(0,0,0,0.35)',
          lineHeight: 1,
        }}>
          {timeStr}
        </div>
      </div>

      <div className="tabs">
        {TABS.map((t) => (
          <div key={t.id}
               className={`tab ${tab === t.id ? 'active' : ''}`}
               onClick={() => setTab(t.id)}>
            {t.label}
          </div>
        ))}
      </div>

      <div className="content">{Body}</div>

      {tab === 'voice' && (
        <div style={{ margin: '0 16px 16px' }}>
          <button className="mic-btn" onClick={onMicClick}>
            <span className="rec-dot"></span>
            {s.listening ? 'Stop listening' : 'Start listening'}
          </button>
        </div>
      )}
    </div>
  );
}

// ─── Full-chat window mode (separate pywebview window) ────────────────
function FullChatWindow() {
  const s = usePCStore();

  // Bootstrap PCStore from the URL hash. We DON'T trust the cached `HASH`
  // constant — pywebview's `load_url` to the same index.html with a new
  // fragment can be treated as in-page navigation by WebView2 (no script
  // re-evaluation), so the module-level HASH stays stale. Re-read
  // window.location.hash here, AND listen for hashchange so we re-bootstrap
  // every time Python navigates the window.
  useEffect(() => {
    const bootstrap = async () => {
      const h = parseHashParams(window.location.hash || '');
      const id = h.id;
      if (!id) return;  // pending state — leave PCStore empty
      // If we already have this chat loaded, keep its messages instead of
      // wiping them on every hashchange.
      const existing = (PCStore.state.codeSessions || []).find(
        (c) => String(c.id) === String(id)
      );
      const chat = existing || {
        id,
        name: h.name || 'session',
        cwd: h.cwd || '',
        claudeSessionId: h.prior || null,
        messages: [],
      };
      PCStore.set({
        codeSessions: [chat],
        activeChatId: id,
        ccView: 'chat',
      });
      document.title = `Claude Code · ${chat.name}`;

      // Seed the chat with the prior conversation. We pull it over the
      // WebSocket bridge (`/history/<session_id>`) instead of the
      // pywebview RPC because freshly-navigated windows have a flaky
      // RPC channel that hangs on get_session_messages.
      if ((chat.messages || []).length === 0 && chat.claudeSessionId) {
        try {
          const msgs = await loadHistoryViaWS(chat.claudeSessionId, 8000);
          if (Array.isArray(msgs) && msgs.length) {
            const cur = PCStore.state.codeSessions || [];
            PCStore.set({
              codeSessions: cur.map((c) => c.id === chat.id
                ? { ...c, messages: msgs }
                : c),
            });
          }
        } catch (_) {}
      }
    };
    bootstrap();
    window.addEventListener('hashchange', bootstrap);
    return () => window.removeEventListener('hashchange', bootstrap);
  }, []);

  const chat = (s.codeSessions || [])[0];
  if (!chat) {
    // Pre-created window in pending state, OR a brief flash before
    // load_url() injects real session data. Render NOTHING so the
    // window stays fully transparent (DWM acrylic shows through).
    return (
      <div style={{
        width: '100vw', height: '100vh',
        background: 'transparent',
      }} />
    );
  }
  const inst = { id: chat.id, name: chat.name, dir: chat.cwd };

  // Close button replaces the dock's "back" — closes the whole window
  // Closing the chat window: ask Python to HIDE it (preserve transparency
  // for the next open). Falls back to window.close() if the API isn't there.
  const onClose = () => {
    try {
      if (api().close_chat_window) {
        api().close_chat_window();
        return;
      }
    } catch (_) {}
    try { window.close(); } catch (_) {}
  };

  return (
    <div className="app" style={{ height: '100vh' }}>
      <div className="content" style={{ padding: 18, gap: 0, height: '100%' }}>
        <ChatPane chat={chat}
                  inst={inst}
                  onBack={onClose} />
      </div>
    </div>
  );
}

// ─── Full-task-editor window (mode=task) ─────────────────────────────
function FullTaskWindow() {
  const [task, setTask] = useState(null);
  const [loading, setLoading] = useState(false);
  // `pending` is true when no task id is in the URL hash (i.e., the
  // pre-created off-screen window or right after load_url before bootstrap
  // resolves). Drive the render off this STATE — not the cached
  // module-level HASH — because WebView2 doesn't re-run the script on
  // same-document hash navigation, so HASH stays stale forever.
  const [pending, setPending] = useState(true);
  const [notFound, setNotFound] = useState(false);

  useEffect(() => {
    const bootstrap = () => {
      const h = parseHashParams(window.location.hash || '');
      const id = h.id;
      if (!id || h.pending) {
        setPending(true);
        setTask(null);
        setNotFound(false);
        setLoading(false);
        return;
      }
      // Build the task object directly from the URL hash (Python
      // serialised it in `open_task_window`) so we never depend on the
      // pywebview JSON-RPC bridge for initial render — it sometimes
      // hangs on a window that's been re-navigated via load_url().
      const t = {
        id,
        text: h.text || '',
        description: h.description || '',
        done: h.done === '1',
      };
      setPending(false);
      setLoading(false);
      setNotFound(false);
      setTask(t);
      document.title = `Task · ${t.text || 'Untitled'}`;
    };
    bootstrap();
    window.addEventListener('hashchange', bootstrap);
    return () => window.removeEventListener('hashchange', bootstrap);
  }, []);

  const onClose = () => {
    try {
      if (api().close_task_window) { api().close_task_window(); return; }
    } catch (_) {}
    try { window.close(); } catch (_) {}
  };

  // Fire-and-forget RPC saves — never await on the bridge from this
  // window, because a hung RPC would block the editor. Optimistic UI
  // updates land first; persistence happens in the background.
  const onChange = (patch) => {
    if (!task) return;
    setTask({ ...task, ...patch });  // optimistic
    try {
      if (api().update_task) {
        const p = api().update_task(task.id, patch);
        if (p && typeof p.catch === 'function') p.catch(() => {});
      }
    } catch (_) {}
  };

  const onDelete = () => {
    if (!task) return;
    try {
      if (api().delete_task) {
        const p = api().delete_task(task.id);
        if (p && typeof p.catch === 'function') p.catch(() => {});
      }
    } catch (_) {}
    onClose();
  };

  // Pending / pre-created state — keep transparent so the off-screen
  // window can't flash anything visible
  if (pending) {
    return <div style={{ width: '100vw', height: '100vh',
                         background: 'transparent' }} />;
  }
  if (loading) {
    return (
      <div className="task-editor-window">
        <div className="td-loading">Loading task…</div>
      </div>
    );
  }
  if (notFound || !task) {
    return (
      <div className="task-editor-window">
        <div className="td-loading">
          Task not found.
          <button className="td-loading-back" onClick={onClose}>Close</button>
        </div>
      </div>
    );
  }
  return (
    <div className="task-editor-window">
      <TaskDetail task={task}
                  onChange={onChange}
                  onBack={onClose}
                  onDelete={onDelete} />
    </div>
  );
}

const ROOT = ReactDOM.createRoot(document.getElementById('root'));
ROOT.render(
  IS_FULL_CHAT ? <FullChatWindow />
  : IS_TASK_EDIT ? <FullTaskWindow />
  : <App />
);
