// Clawd is defined in clawd.jsx and attached to window.ClaudeMascot.
// We re-bind it here as a local for JSX usage in this file.
const ClaudeMascot = window.ClaudeMascot;

// Thick chunky arrow — pixel-aligned via SVG strokes. dir: 'right'|'left'|'up'|'down'.
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

// Donut ring for monitor cards
function Ring({ value, color = '#5eead4' }) {
  const r = 13;
  const c = 2 * Math.PI * r;
  const off = c - (value / 100) * c;
  return (
    <svg className="ring" viewBox="0 0 32 32">
      <circle cx="16" cy="16" r={r} fill="none" stroke="rgba(255,255,255,0.1)" strokeWidth="3"/>
      <circle cx="16" cy="16" r={r} fill="none" stroke={color} strokeWidth="3" strokeLinecap="round"
        strokeDasharray={c} strokeDashoffset={off}
        transform="rotate(-90 16 16)" />
    </svg>
  );
}

function Panel({ title, icon, dark = false, action = true, children }) {
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

// Voice tab
function VoiceTab() {
  // Cycle through emotive states for demo
  const [state, setState] = React.useState('listening');
  const [draft, setDraft] = React.useState('');
  React.useEffect(() => {
    const seq = ['listening', 'thinking', 'speaking', 'happy'];
    let i = 0;
    const t = setInterval(() => {
      i = (i + 1) % seq.length;
      setState(seq[i]);
    }, 2400);
    return () => clearInterval(t);
  }, []);

  const stateLabel = {
    listening: '● listening',
    thinking: '○ thinking…',
    speaking: '◆ speaking',
    happy: '✨ ready',
  }[state];

  return (
    <Panel title="Claude voice" icon="✱" action={false}>
      <div className="voice-orb-wrap"><ClaudeMascot state={state} /></div>
      <div className="transcript">
        <div className="bubble you">hello</div>
        <div className="bubble claude">Hey! How can I help you today?</div>
      </div>
      <div className="voice-status">
        <span style={{color:'var(--mint)', fontWeight:500}}>{stateLabel}</span>
        <span>·</span>
        <span>en-US</span>
      </div>
      <form
        className="voice-text-fallback"
        onSubmit={(e) => { e.preventDefault(); if (draft.trim()) { setDraft(''); } }}
      >
        <span className="vtf-icon">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M2 4 H14 V11 H6 L3 13 V11 H2 Z"/>
          </svg>
        </span>
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Type instead…"
          className="vtf-input"
        />
        <button type="submit" className="vtf-send" disabled={!draft.trim()} aria-label="Send">
          <ThickArrow dir="up" size={14} />
        </button>
      </form>
    </Panel>
  );
}

// Monitor tab
function MonitorTab() {
  return (
    <>
      <Panel title="System" icon="◐">
        <div className="metric-grid">
          <div className="metric-card">
            <Ring value={53} />
            <div className="ml">CPU</div>
            <div className="mv">53<span className="unit">%</span></div>
            <div className="bar"><div className="bar-fill" style={{width:'53%'}}/></div>
            <div className="sub">8 cores · 2.50 GHz</div>
          </div>
          <div className="metric-card">
            <Ring value={64} />
            <div className="ml">RAM</div>
            <div className="mv">10.1<span className="unit">/ 15.8 GB</span></div>
            <div className="bar"><div className="bar-fill" style={{width:'64%'}}/></div>
            <div className="sub">64% used</div>
          </div>
          <div className="metric-card">
            <Ring value={27} />
            <div className="ml">GPU</div>
            <div className="mv">27<span className="unit">%</span></div>
            <div className="bar"><div className="bar-fill" style={{width:'27%'}}/></div>
            <div className="sub">RTX 2060 · 75°C</div>
          </div>
          <div className="metric-card">
            <Ring value={72} color="#fbbf24" />
            <div className="ml">Disk</div>
            <div className="mv">172<span className="unit">/ 237 GB</span></div>
            <div className="bar"><div className="bar-fill warn" style={{width:'72%'}}/></div>
            <div className="sub">C:\ · 72% used</div>
          </div>
        </div>
      </Panel>

      <Panel title="Network" icon="≋">
        <div className="kv">
          <span className="k"><span className="arr"><ThickArrow dir="down" size={11} /></span> Download</span>
          <span className="v">5 KB/s</span>
        </div>
        <div className="kv">
          <span className="k"><span className="arr"><ThickArrow dir="up" size={11} /></span> Upload</span>
          <span className="v">343 B/s</span>
        </div>
        <div className="kv">
          <span className="k">⌖ Public IP</span>
          <span className="v" style={{color:'var(--ink-mute)'}}>fetching…</span>
        </div>
        <div className="kv">
          <span className="k">⌖ Local IP</span>
          <span className="v">192.168.1.4</span>
        </div>
      </Panel>
    </>
  );
}

function ControlTab() {
  const [result, setResult] = React.useState({ procs: 168, mb: 711 });
  return (
    <Panel title="Quick actions" icon="✦" action={false}>
      <button className="action-primary" onClick={() => setResult({
        procs: Math.floor(Math.random()*200+50),
        mb: Math.floor(Math.random()*900+200)
      })}>
        <span>Clear RAM</span>
        <span className="ap-arr"><ThickArrow dir="right" size={14} /></span>
      </button>
      <div className="action-result">
        Trimmed {result.procs} processes · freed {result.mb} MB
      </div>
      <div className="action-grid">
        <button className="action-btn"><span className="icon">⊟</span>Lock</button>
        <button className="action-btn"><span className="icon">☾</span>Sleep</button>
        <button className="action-btn"><span className="icon">▦</span>Desktop</button>
      </div>
    </Panel>
  );
}

function NotesTab() {
  const [text, setText] = React.useState('');
  const [saved, setSaved] = React.useState(true);
  React.useEffect(() => {
    setSaved(false);
    const t = setTimeout(() => setSaved(true), 500);
    return () => clearTimeout(t);
  }, [text]);
  const chars = text.length;
  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  return (
    <Panel title="Notes" icon="✎" action={false}>
      <textarea
        className="notes-area"
        value={text}
        onChange={e => setText(e.target.value)}
        placeholder="Quick scratch pad — auto-saves as you type."
        autoFocus
      />
      <div className="notes-meta">
        <span>{words} words · {chars} chars</span>
        <span className="saved">{saved ? 'saved' : 'saving…'}</span>
      </div>
    </Panel>
  );
}

Object.assign(window, { Ring, Panel, VoiceTab, MonitorTab, ControlTab, NotesTab });
