// Clawd — Claude's pixel-art mascot, dancing/walking sprite from ayotomcs.me/claude-mascot.
// 8 frames (l001–l008) form a side-step dance loop. Frames 0–3 face right, 4–7 face left.
// Two sparkle clusters (right side at translate(90,-58), left side mirrored at translate(40,-22))
// each have multi-stage particle bursts that we cycle through.

// Each entry is the inner SVG of one frame group. The outer transform is preserved per-frame.
const CLAWD_FRAMES = [
  // l001 — right-facing, legs down
  {
    transform: 'translate(0, 36)',
    body: `<rect x="96" y="65" width="11" height="12" fill="#DD775B"/><rect x="75" y="65" width="11" height="12" fill="#DD775B"/><rect x="43" y="65" width="11" height="12" fill="#DD775B"/><rect x="22" y="65" width="11" height="12" fill="#DD775B"/><rect x="22" width="85" height="65" fill="#DD775B"/><rect x="107" y="36" width="22" height="23" fill="#DD775B"/><rect y="36" width="22" height="23" fill="#DD775B"/><rect x="86" y="25" width="11" height="11" fill="black"/><rect x="32" y="25" width="11" height="11" fill="black"/>`,
  },
  // l002 — right-facing, mid-step (head tilted)
  {
    transform: 'translate(2, 3)',
    body: `<rect x="96" y="80" width="11" height="30" fill="#DD775B"/><rect x="75" y="80" width="11" height="30" fill="#DD775B"/><rect x="43" y="85" width="11" height="25" fill="#DD775B"/><rect x="22" y="85" width="11" height="25" fill="#DD775B"/><rect x="22" y="20" width="42" height="65" fill="#DD775B"/><rect x="64" y="9" width="43" height="71" fill="#DD775B"/><rect x="43" y="14" width="32" height="21" fill="#DD775B"/><rect x="103" width="22" height="31" fill="#DD775B"/><rect y="42" width="22" height="23" fill="#DD775B"/><rect x="86" y="20" width="11" height="11" fill="black"/><rect x="32" y="31" width="11" height="11" fill="black"/>`,
  },
  // l003 — right-facing, hand raised waving
  {
    transform: 'translate(2, -8)',
    body: `<rect x="96" y="83" width="11" height="27" fill="#DD775B"/><rect x="75" y="83" width="11" height="27" fill="#DD775B"/><rect x="43" y="88" width="11" height="25" fill="#DD775B"/><rect x="22" y="88" width="11" height="25" fill="#DD775B"/><rect x="22" y="23" width="42" height="65" fill="#DD775B"/><rect x="64" y="12" width="43" height="71" fill="#DD775B"/><rect x="43" y="17" width="32" height="21" fill="#DD775B"/><rect x="103" y="8" width="8" height="26" fill="#DD775B"/><rect x="108" width="8" height="30" fill="#DD775B"/><rect x="112" width="8" height="27" fill="#DD775B"/><rect x="117" width="8" height="24" fill="#DD775B"/><rect y="59" width="22" height="23" fill="#DD775B"/><rect x="86" y="24" width="11" height="11" fill="black"/><rect x="32" y="34" width="11" height="11" fill="black"/>`,
  },
  // l004 — right-facing, mid-step
  {
    transform: 'translate(2, 3)',
    body: `<rect x="96" y="80" width="11" height="26" fill="#DD775B"/><rect x="75" y="80" width="11" height="26" fill="#DD775B"/><rect x="43" y="85" width="11" height="25" fill="#DD775B"/><rect x="22" y="85" width="11" height="25" fill="#DD775B"/><rect x="22" y="20" width="42" height="65" fill="#DD775B"/><rect x="64" y="9" width="43" height="71" fill="#DD775B"/><rect x="43" y="14" width="32" height="21" fill="#DD775B"/><rect x="103" width="22" height="31" fill="#DD775B"/><rect y="42" width="22" height="23" fill="#DD775B"/><rect x="86" y="20" width="11" height="11" fill="black"/><rect x="32" y="31" width="11" height="11" fill="black"/>`,
  },
  // l005 — left-facing, legs down (mirror of l001)
  {
    transform: 'translate(0, 36)',
    body: `<rect x="96" y="65" width="11" height="12" fill="#DD775B"/><rect x="75" y="65" width="11" height="12" fill="#DD775B"/><rect x="43" y="65" width="11" height="12" fill="#DD775B"/><rect x="22" y="65" width="11" height="12" fill="#DD775B"/><rect x="22" width="85" height="65" fill="#DD775B"/><rect x="107" y="36" width="22" height="23" fill="#DD775B"/><rect y="36" width="22" height="23" fill="#DD775B"/><rect x="86" y="25" width="11" height="11" fill="black"/><rect x="32" y="25" width="11" height="11" fill="black"/>`,
  },
  // l006 — left-facing, mid-step (mirrored)
  {
    transform: 'translate(2, 3)',
    body: `<rect width="11" height="30" transform="matrix(-1 0 0 1 29 80)" fill="#DD775B"/><rect width="11" height="30" transform="matrix(-1 0 0 1 50 80)" fill="#DD775B"/><rect width="11" height="25" transform="matrix(-1 0 0 1 82 85)" fill="#DD775B"/><rect width="11" height="25" transform="matrix(-1 0 0 1 103 85)" fill="#DD775B"/><rect width="42" height="65" transform="matrix(-1 0 0 1 103 20)" fill="#DD775B"/><rect width="43" height="71" transform="matrix(-1 0 0 1 61 9)" fill="#DD775B"/><rect width="32" height="21" transform="matrix(-1 0 0 1 82 14)" fill="#DD775B"/><rect width="22" height="31" transform="matrix(-1 0 0 1 22 0)" fill="#DD775B"/><rect width="22" height="23" transform="matrix(-1 0 0 1 125 42)" fill="#DD775B"/><rect width="11" height="11" transform="matrix(-1 0 0 1 39 20)" fill="black"/><rect width="11" height="11" transform="matrix(-1 0 0 1 93 31)" fill="black"/>`,
  },
  // l007 — left-facing, hand raised (mirrored l003)
  {
    transform: 'translate(2, -8)',
    body: `<rect width="11" height="27" transform="matrix(-1 0 0 1 29 83)" fill="#DD775B"/><rect width="11" height="27" transform="matrix(-1 0 0 1 50 83)" fill="#DD775B"/><rect width="11" height="25" transform="matrix(-1 0 0 1 82 88)" fill="#DD775B"/><rect width="11" height="25" transform="matrix(-1 0 0 1 103 88)" fill="#DD775B"/><rect width="42" height="65" transform="matrix(-1 0 0 1 103 23)" fill="#DD775B"/><rect width="43" height="71" transform="matrix(-1 0 0 1 61 12)" fill="#DD775B"/><rect width="32" height="21" transform="matrix(-1 0 0 1 82 17)" fill="#DD775B"/><rect width="8" height="26" transform="matrix(-1 0 0 1 22 8)" fill="#DD775B"/><rect width="8" height="30" transform="matrix(-1 0 0 1 17 0)" fill="#DD775B"/><rect width="8" height="27" transform="matrix(-1 0 0 1 13 0)" fill="#DD775B"/><rect width="8" height="24" transform="matrix(-1 0 0 1 8 0)" fill="#DD775B"/><rect width="22" height="23" transform="matrix(-1 0 0 1 125 59)" fill="#DD775B"/><rect width="11" height="11" transform="matrix(-1 0 0 1 39 24)" fill="black"/><rect width="11" height="11" transform="matrix(-1 0 0 1 93 34)" fill="black"/>`,
  },
  // l008 — left-facing, mid-step (mirrored l004)
  {
    transform: 'translate(2, 3)',
    body: `<rect width="11" height="26" transform="matrix(-1 0 0 1 29 80)" fill="#DD775B"/><rect width="11" height="26" transform="matrix(-1 0 0 1 50 80)" fill="#DD775B"/><rect width="11" height="25" transform="matrix(-1 0 0 1 82 85)" fill="#DD775B"/><rect width="11" height="25" transform="matrix(-1 0 0 1 103 85)" fill="#DD775B"/><rect width="42" height="65" transform="matrix(-1 0 0 1 103 20)" fill="#DD775B"/><rect width="43" height="71" transform="matrix(-1 0 0 1 61 9)" fill="#DD775B"/><rect width="32" height="21" transform="matrix(-1 0 0 1 82 14)" fill="#DD775B"/><rect width="22" height="31" transform="matrix(-1 0 0 1 22 0)" fill="#DD775B"/><rect width="22" height="23" transform="matrix(-1 0 0 1 125 42)" fill="#DD775B"/><rect width="11" height="11" transform="matrix(-1 0 0 1 39 20)" fill="black"/><rect width="11" height="11" transform="matrix(-1 0 0 1 93 31)" fill="black"/>`,
  },
];

// Sparkle particle stages — each is an array of <rect> strings shown together.
// The right-side sparkle group lives at translate(90,-58); left-side mirrors via scale(-1,1) at translate(40,-22).
const SPARKLE_STAGES_RIGHT = [
  // stage 0 — small chevron
  `<rect x="34" y="38" width="5" height="5" fill="#7CA4D0"/><rect x="29" y="24" width="5" height="5" fill="#DD8361"/><rect x="29" y="5" width="5" height="5" fill="#C87392"/><rect x="10" y="34" width="5" height="5" fill="#7CA4D0"/><rect x="39" y="33" width="5" height="5" fill="#7CA4D0"/><rect x="34" y="19" width="5" height="5" fill="#DD8361"/><rect x="34" width="5" height="5" fill="#C87392"/><rect x="5" y="29" width="5" height="5" fill="#7CA4D0"/>`,
  // stage 1 — diagonal trail
  `<rect x="58" y="45" width="5" height="5" fill="#7CA4D0"/><rect x="5" y="17" width="5" height="5" fill="#7CA4D0"/><rect x="39" y="24" width="10" height="5" fill="#DD8361"/><rect x="25" y="15" width="5" height="5" fill="#C87392"/><rect x="15" y="34" width="10" height="5" fill="#7CA4D0"/><rect x="53" y="40" width="5" height="5" fill="#7CA4D0"/>`,
  // stage 2 — wider burst
  `<rect x="61" y="48" width="5" height="10" fill="#7CA4D0"/><rect y="25" width="10" height="5" fill="#7CA4D0"/><rect x="13" y="43" width="5" height="5" fill="#7CA4D0"/><rect x="23" y="20" width="10" height="5" fill="#C87392"/><rect x="47" y="35" width="5" height="5" fill="#DD8361"/><rect x="61" y="14" width="5" height="5" fill="#C87392"/><rect x="23" y="5" width="5" height="5" fill="#DD8361"/><rect x="18" y="38" width="5" height="5" fill="#7CA4D0"/>`,
  // stage 3 — fading
  `<rect x="76" y="43" width="5" height="5" fill="#7CA4D0"/><rect x="71" y="48" width="5" height="5" fill="#7CA4D0"/><rect y="25" width="5" height="5" fill="#7CA4D0"/><rect x="8" y="38" width="5" height="10" fill="#7CA4D0"/><rect x="76" y="4" width="5" height="10" fill="#C87392"/><rect x="18" y="20" width="5" height="5" fill="#C87392"/><rect x="52" y="25" width="10" height="5" fill="#DD8361"/>`,
  // stage 4 — small remainder
  `<rect x="76" y="45" width="5" height="5" fill="#7CA4D0"/><rect x="71" y="50" width="5" height="5" fill="#7CA4D0"/><rect y="24" width="5" height="5" fill="#7CA4D0"/><rect x="6" y="38" width="5" height="5" fill="#7CA4D0"/><rect x="76" y="11" width="5" height="5" fill="#C87392"/><rect x="21" y="20" width="5" height="5" fill="#C87392"/>`,
  // stage 5 — tiny
  `<rect x="65" y="50" width="5" height="5" fill="#7CA4D0"/><rect y="38" width="5" height="5" fill="#7CA4D0"/><rect x="70" y="11" width="5" height="5" fill="#C87392"/><rect x="17" width="5" height="5" fill="#DD8361"/>`,
  // stage 6 — almost gone
  `<rect x="53" y="11" width="5" height="5" fill="#C87392"/><rect width="5" height="5" fill="#DD8361"/>`,
  // stage 7 — empty / single dot
  `<rect width="5" height="5" fill="#C87392"/>`,
];

// State → which frames to play and how fast.
// Frames 0–3 face right, 4–7 face left, 2 and 6 are hand-raised "wave" poses.
// Each state uses radically different frames so the difference is unmistakable.
// 0 = right-stand,  1 = right-step,    2 = right-WAVE,   3 = right-step
// 4 = left-stand,   5 = left-step,     6 = left-WAVE,    7 = left-step
const STATE_ANIMS = {
  idle:       { frames: [0],                         fps: 1  },  // CALM — still, no sparkles (default)
  listening:  { frames: [0, 1, 0, 0],                fps: 2  },  // subtle head-tilt every 2s — alive, attentive
  thinking:   { frames: [0, 4],                      fps: 1  },  // slow look left ↔ right
  speaking:   { frames: [1, 5],                      fps: 2  },  // gentle sway right ↔ left (talking, not celebrating)
  happy:      { frames: [2, 6],                      fps: 6  },  // waving both arms alternately
  done:       { frames: [2],                         fps: 1  },  // arm raised, victory pose
  // Standalone SVG poses — rendered from their own files (different viewBoxes)
  finish:     { external: 'assets/clawd-finish.svg'              },  // racing flag up at the line
  exercising: { external: 'assets/clawd-exercising.svg'          },  // dumbbells, headband
};

function ClaudeMascot({ state = 'idle', size = 160 }) {
  const anim = STATE_ANIMS[state] || STATE_ANIMS.idle;
  const isExternal = !!anim.external;

  // Hooks always called in the same order — effects no-op for external states
  const [step, setStep] = React.useState(0);
  const [sparkle, setSparkle] = React.useState(0);

  React.useEffect(() => { setStep(0); }, [state]);

  React.useEffect(() => {
    if (isExternal) return;
    const id = setInterval(() => setStep(s => s + 1), 1000 / anim.fps);
    return () => clearInterval(id);
  }, [anim.fps, isExternal]);

  React.useEffect(() => {
    if (isExternal || state === 'idle') return;
    const speed = (state === 'speaking' || state === 'happy') ? 110 : 220;
    const id = setInterval(() => setSparkle(s => (s + 1) % SPARKLE_STAGES_RIGHT.length), speed);
    return () => clearInterval(id);
  }, [state, isExternal]);

  // External SVG states render their own file at the same display size
  if (isExternal) {
    return (
      <div style={{
        position: 'relative',
        width: size, height: size,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        <img src={anim.external}
             alt={state}
             style={{
               width: size, height: size,
               imageRendering: 'pixelated',
               objectFit: 'contain',
             }} />
      </div>
    );
  }

  const frameIdx = anim.frames[step % anim.frames.length];
  const f = CLAWD_FRAMES[frameIdx];
  const sparkleR = state === 'idle' ? '' : SPARKLE_STAGES_RIGHT[sparkle];
  const sparkleL = state === 'idle' ? '' : SPARKLE_STAGES_RIGHT[(sparkle + 4) % SPARKLE_STAGES_RIGHT.length];

  const inner = `
    <g transform="${f.transform}">${f.body}</g>
    <g transform="translate(90, -58)">${sparkleR}</g>
    <g transform="translate(40, -22) scale(-1, 1)">${sparkleL}</g>
  `;

  return (
    <div style={{
      position: 'relative',
      width: size, height: size,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <div style={{
        position: 'absolute', bottom: 6, left: '50%',
        width: size * 0.55, height: 8,
        transform: 'translateX(-50%)',
        background: 'radial-gradient(ellipse, rgba(0,0,0,0.32) 0%, transparent 70%)',
        filter: 'blur(2px)',
      }}/>
      <svg
        viewBox="0 0 129 113"
        width={size}
        height={size * (113/129)}
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        shapeRendering="crispEdges"
        style={{ overflow: 'visible' }}
        dangerouslySetInnerHTML={{ __html: inner }}
      />
    </div>
  );
}

window.ClaudeMascot = ClaudeMascot;
