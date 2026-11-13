/** Static counterpart of the delivery route for reduced motion and unavailable WebGL. */
export function ParcelIllustration({ phase = 0, compact = false, premium = false }: { phase?: number; compact?: boolean; premium?: boolean }) {
  const offset = compact ? 130 : ([0, 90, 190, 280][phase] ?? 0);
  return <svg className={`parcel-illustration ${compact ? 'compact' : ''}`} viewBox="0 0 600 470" role="img" aria-label="Parcel illustration">
    {!compact && <>
      <ellipse cx="300" cy="347" rx="250" ry="70" fill="#fffdf6" />
      <path d="M70 337C120 425 390 432 530 315" fill="none" stroke={phase ? '#d8a51b' : '#e2d3a6'} strokeWidth="4" />
      <ellipse cx="70" cy="337" rx="19" ry="9" fill="#ffd447" />
      <ellipse cx="530" cy="315" rx="19" ry="9" fill="#ffd447" />
      {phase === 2 && <path d="M310 397L400 440" fill="none" stroke="#c8cbd0" strokeWidth="3" strokeDasharray="5 7" />}
    </>}
    <g transform={`translate(${offset},0) rotate(-6 160 225)`}>
      <ellipse cx="165" cy="340" rx="83" ry="12" fill="#59450c" opacity=".07" />
      <rect x="85" y="100" width="170" height="222" rx="19" fill="#ffffff" stroke="#e7e4dc" strokeWidth="2" />
      <path d="M85 135H255V176H85Z" fill={premium ? '#e5e8ee' : '#ffd447'} />
      <circle cx="113" cy="155" r="10" fill="#4b3b11" />
      <path d="M110 202H220" stroke="#424952" strokeWidth="4" />
      <path d="M110 220H220M110 238H170" stroke="#d7dbe0" strokeWidth="4" />
      {Array.from({ length: 18 }, (_, index) => <path key={index} d={`M${112 + index * 6} 268v32`} stroke="#343a43" strokeWidth={index % 3 === 0 ? 3 : 1} />)}
    </g>
  </svg>;
}
