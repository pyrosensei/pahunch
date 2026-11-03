export function PlanProgress({ proposed, confirmed, committed }: { proposed: boolean; confirmed: boolean; committed: boolean }) {
  return <ol className="plan-progress" aria-label="Delivery plan progress">{[
    ['Proposed', proposed], ['Confirmed', confirmed], ['Committed', committed],
  ].map(([name, done], i) => <li key={String(name)} className={done ? 'is-complete' : ''}><span aria-hidden="true">{done ? '✓' : `0${i + 1}`}</span>{name}<span className="sr-only">{done ? ': complete' : ': pending'}</span></li>)}</ol>;
}
