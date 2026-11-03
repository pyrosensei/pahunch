/** Local static art for reduced motion, missing WebGL, context loss and storefront parcels. */
export function ParcelIllustration({ phase = 0, compact = false, premium = false }: { phase?: number; compact?: boolean; premium?: boolean }) {
  return <svg className={`parcel-illustration ${compact ? 'compact' : ''}`} viewBox="0 0 600 470" role="img" aria-label="Parcel illustration">
    {!compact && <g fill="none" strokeWidth="2"><path d="M70 365C30 270 215 320 235 378S520 430 532 292" stroke="#c9d2c8" strokeDasharray="5 7" /><path d={phase > 1 ? 'M70 365C30 270 215 320 235 378S520 430 532 292' : 'M70 365C30 270 215 320 235 378'} stroke="#0f766e" /><circle cx="70" cy="365" r="8" fill="#e86b2f" stroke="#e86b2f" /><circle cx="532" cy="292" r="8" fill="#0f766e" stroke="#0f766e" />{phase === 2 && <path d="M280 405L365 455" stroke="#9eaaa1" strokeDasharray="5 7" />}</g>}
    <ellipse cx="305" cy="351" rx="139" ry="29" fill="#25382e" opacity=".07" />
    <g transform={phase === 3 && !compact ? 'translate(96,-4) scale(.7)' : 'translate(0,0)'}>
      <path d="M155 157L310 98L456 164L300 226Z" fill={premium ? '#829c8b' : '#d9bd91'} />
      <path d="M155 157L300 226V355L155 280Z" fill={premium ? '#49695e' : '#bc9364'} />
      <path d="M300 226L456 164V294L300 355Z" fill={premium ? '#668374' : '#cba779'} />
      <path d="M213 135L360 202L389 191L241 124Z" fill="#0f766e" />
      <path d="M360 202L389 191V320L360 332Z" fill="#135d58" />
      <path d="M180 191L247 224V251L180 217Z" fill="#e86b2f" />
      <path d="M190 202L234 223M190 210L218 224" stroke="#fff7e9" strokeWidth="3" />
      <path d="M314 247L349 234V295L314 309Z" fill="#faf7ef" />
      {[0, 1, 2, 3, 4, 5].map(i => <path key={i} d={`M${320 + i * 4} ${280 - i * 1.5}v16`} stroke="#203e39" strokeWidth={i % 2 ? 1 : 2} />)}
      <path d="M320 255L342 247M320 262L338 255" stroke="#66796b" strokeWidth="2" />
      <path d="M160 160L299 230L450 170" fill="none" stroke="#f6e8ce" strokeOpacity=".4" />
    </g>
    {phase === 1 && !compact && <g fill="none" stroke="#0f766e" opacity=".35"><ellipse cx="305" cy="357" rx="165" ry="43" /><ellipse cx="305" cy="357" rx="190" ry="55" /></g>}
  </svg>;
}
