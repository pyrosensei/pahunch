import { Component, lazy, Suspense, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { ArrowDown, ArrowRight, ArrowUpRight, Check, CheckCheck, Package, Pause, Play, ShieldCheck, X } from 'lucide-react';
import { Brand } from '../App';
import { ParcelIllustration } from '../components/ParcelIllustration';
import './Landing.css';

const Scene = lazy(() => import('../components/ParcelScene'));
const chapterNames = ['The delivery journey', '01 / Listen', '02 / Validate', '03 / Commit'];

class SceneBoundary extends Component<{ children: ReactNode; fallback: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? this.props.fallback : this.props.children; }
}

function DeliveryScene({ phase }: { phase: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const [entered, setEntered] = useState(false);
  const [paused, setPaused] = useState(false);
  const [lost, setLost] = useState(false);
  const [tilt, setTilt] = useState(0);
  const [reduced, setReduced] = useState(() => matchMedia('(prefers-reduced-motion: reduce)').matches);
  useEffect(() => {
    const media = matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => setReduced(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    let intersecting = false;
    const update = () => { setVisible(intersecting && !document.hidden); };
    const observer = new IntersectionObserver(([entry]) => {
      intersecting = entry.isIntersecting;
      if (intersecting) setEntered(true);
      update();
    });
    observer.observe(node);
    document.addEventListener('visibilitychange', update);
    return () => { observer.disconnect(); document.removeEventListener('visibilitychange', update); };
  }, []);
  const fallback = <ParcelIllustration phase={phase} />;
  return <div ref={ref} className="delivery-diorama" data-phase={phase} data-rendering={visible && !paused && !reduced && !lost ? 'active' : 'paused'} onPointerMove={event => {
    if (paused || reduced || event.pointerType === 'touch') return;
    const rect = event.currentTarget.getBoundingClientRect();
    setTilt(((event.clientX - rect.left) / rect.width - .5) * .14);
  }} onPointerLeave={() => setTilt(0)}>
    <div className="diorama-heading"><span>SWIFTCART → YOUR DOOR</span><span>FIG. 01</span></div>
    <div className="delivery-canvas" aria-hidden="true">
      {entered && !reduced && !lost ? <SceneBoundary fallback={fallback}><Suspense fallback={fallback}><Scene phase={phase} tilt={paused ? 0 : tilt} active={visible && !paused} onContextLost={() => setLost(true)} /></Suspense></SceneBoundary> : fallback}
    </div>
    <div className="diorama-origin"><span className="route-pin orange-pin" />SwiftCart<small>THE STARTING POINT</small></div>
    <div className="diorama-destination"><span className="route-pin" />Your doorstep<small>A CLEARER HANDOFF</small></div>
    {phase === 1 && <div className="scene-caption"><span className="mono">CUSTOMER PREFERENCE</span><p>“Shaam ko ghar par hoon.”</p><small>I’ll be home in the evening.</small></div>}
    {phase === 2 && <div className="scene-caption policy-caption"><ShieldCheck size={20} /><div><span className="mono">DETERMINISTIC CHECKPOINT</span><p>Every proposed action meets a rule.</p></div></div>}
    {phase === 3 && <div className="illustrative-courier"><div className="courier-label-top"><span>COURIER CARD</span><span>ILLUSTRATIVE</span></div><h3>Hand to customer.</h3><dl><div><dt>Approved window</dt><dd>6–8 pm · example</dd></div><div><dt>Recipient instructions</dt><dd>Customer · call on arrival</dd></div><div><dt>Policy verdict</dt><dd>Permitted</dd></div><div><dt>Confirmation</dt><dd>Explicitly confirmed</dd></div></dl><div className="courier-label-bottom"><CheckCheck size={16} />Plan committed<span className="barcode" /></div></div>}
    <div className="diorama-footer"><span>{chapterNames[phase]}</span><span className="scene-pagination" aria-hidden="true">{chapterNames.map((name, i) => <i className={i <= phase ? 'filled' : ''} key={name} />)}</span><button type="button" onClick={() => setPaused(!paused)} disabled={!!reduced || lost} aria-label={paused ? 'Enable decorative motion' : 'Pause decorative motion'}>{paused || reduced ? <Play size={14} /> : <Pause size={14} />}</button></div>
    <p className="diorama-disclosure">From dispatch desk to doorstep · scroll through the checkpoints</p>
  </div>;
}

export default function Landing() {
  const story = useRef<HTMLDivElement>(null);
  const [phase, setPhase] = useState(0);
  useEffect(() => {
    let frame = 0;
    const update = () => {
      frame = 0;
      if (document.hidden) return;
      let current = 0;
      story.current?.querySelectorAll<HTMLElement>('[data-chapter]').forEach(node => {
        if (node.getBoundingClientRect().top <= innerHeight * .55) current = Number(node.dataset.chapter);
      });
      setPhase(current);
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(update); };
    update();
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    document.addEventListener('visibilitychange', schedule);
    return () => { cancelAnimationFrame(frame); window.removeEventListener('scroll', schedule); window.removeEventListener('resize', schedule); document.removeEventListener('visibilitychange', schedule); };
  }, []);
  return <div className="editorial-landing">
    <header className="landing-header"><Brand /><nav aria-label="Main navigation"><a href="#how-it-works">The delivery story</a><a href="#platform">The platform</a><Link to="/shop">SwiftCart demo <ArrowUpRight size={13} /></Link></nav><Link className="button small secondary" to="/dashboard">Open dashboard <ArrowUpRight size={15} /></Link></header>
    <main id="main">
      <div className="delivery-story" ref={story}>
        <div className="journey-text">
          <section className="editorial-hero" aria-labelledby="hero-title"><span className="eyebrow">A LITTLE CONVERSATION. A BETTER DELIVERY.</span><h1 id="hero-title">Your delivery.<br />Your day.<br /><span className="serif-word">Let’s make it work.</span></h1><p>Tell us what works for you. We’ll check the delivery rules and help your courier arrive with a clear, confirmed plan.</p><div className="editorial-actions"><Link className="button primary" to="/shop">Try a delivery <ArrowUpRight size={18} /></Link><Link className="text-link" to="/dashboard">Open the delivery monitor <ArrowRight size={15} /></Link></div><p className="synthetic-note">A working synthetic demo. No real purchases or phone calls.</p><a className="story-scroll" href="#how-it-works"><ArrowDown size={15} />ONE PARCEL. THREE CHECKPOINTS.</a></section>
          <section id="how-it-works" className="story-chapter" data-chapter="1"><div className="chapter-number">01 <span>TELL US WHAT WORKS</span></div><h2>First, <span className="serif-word">listen.</span></h2><p>A helpful introduction, then it’s your turn. Tell us when you’re available in English, Hindi or Hinglish.</p><blockquote>“Can you come in the evening?”<footer>The reply becomes a proposal, not an assumed agreement.</footer></blockquote><div className="chapter-footnote">NATURAL LANGUAGE <span>→</span> STRUCTURED PROPOSAL</div></section>
          <section className="story-chapter" data-chapter="2"><div className="chapter-number">02 <span>WE CHECK THE DETAILS</span></div><h2>Then, <span className="serif-word">validate.</span></h2><p>We check your request against the delivery window, payment and handoff rules. If it doesn’t fit, we help find an allowed option.</p><div className="policy-example"><span className="mono">ILLUSTRATIVE · HIGH-VALUE ORDER</span><div><X size={17} /><span>“Leave with security”<small>Rejected · direct handoff required</small></span></div><div><Check size={17} /><span>Handoff to approved recipient<small>Permitted alternative · the customer</small></span></div></div><div className="chapter-footnote">MODEL PROPOSAL <span>≠</span> POLICY PERMISSION</div></section>
          <section className="story-chapter" data-chapter="3"><div className="chapter-number">03 <span>ALL SET FOR THE COURIER</span></div><h2>Confirm.<br />Then <span className="serif-word">commit.</span></h2><p>You review and confirm the plan. We check it once more, then save clear instructions for your courier.</p><div className="commit-sequence"><span>Proposed</span><ArrowRight size={14} /><span>Confirmed</span><ArrowRight size={14} /><strong>Committed</strong></div><p className="chapter-aside">A committed plan tells the courier what to do. It doesn’t mean the parcel has been delivered.</p><Link className="text-link" to="/shop">Take a parcel through the journey <ArrowUpRight size={16} /></Link></section>
        </div>
        <aside className="journey-visual" aria-label="Illustrated delivery journey"><DeliveryScene phase={phase} /></aside>
      </div>
      <section id="platform" className="editorial-platform"><div className="platform-copy"><span className="eyebrow">THE OPERATIONS WORKSPACE</span><h2>A conversation.<br /><span className="serif-word">With a paper trail.</span></h2><p>Inspect the proposal, policy verdict and confirmation side by side. Trace the final instructions back to the conversation that shaped them.</p><Link className="text-link" to="/dashboard">Explore the actual dashboard <ArrowUpRight size={17} /></Link></div><div className="dispatch-preview"><div className="dispatch-toolbar"><span className="mono">PAHUNCH / DISPATCH</span><span>ILLUSTRATIVE PREVIEW</span></div><div className="dispatch-body"><div className="dispatch-transcript"><span className="mono">CONVERSATION</span><p><small>CUSTOMER</small>“I’ll be home this evening. Please hand it to me.”</p><p><small>PAHUNCH</small>“Shall I confirm direct handoff in the available evening window?”</p></div><div className="dispatch-plan"><ShieldCheck size={20} /><span className="mono">DELIVERY PLAN</span><h3>Hand to customer</h3><dl><dt>Policy</dt><dd>Permitted</dd><dt>Confirmation</dt><dd>Awaiting customer</dd><dt>Commit</dt><dd>Not yet committed</dd></dl></div></div><div className="dispatch-footer">Conversation → proposal → policy → confirmation → courier card</div></div></section>
      <section className="demo-invitation"><div className="demo-invitation-copy"><span className="eyebrow orange-text">YOUR TURN AT SWIFTCART</span><h2>One small order.<br />The <span className="serif-word">whole journey.</span></h2><p>Choose a synthetic parcel and see how a conversation becomes a delivery plan.</p><div className="editorial-actions"><Link className="button orange" to="/shop">Try a delivery <ArrowUpRight size={17} /></Link><Link className="text-link" to="/dashboard">Open the dashboard <ArrowRight size={15} /></Link></div></div><ol className="demo-checklist">{['Place a synthetic order', 'Mark it out for delivery', 'Start a customer conversation', 'Review the policy-checked proposal', 'Explicitly confirm the plan', 'Save the committed courier card'].map((text, i) => <li key={text}><span>{String(i + 1).padStart(2, '0')}</span>{text}{i === 5 && <Package size={16} />}</li>)}</ol></section>
    </main><footer className="landing-footer"><div><Brand /><p>Thoughtful conversations. Clearer deliveries.</p></div><div className="footer-right"><span>All orders and customer details are synthetic.</span><a href="#main">Back to the top ↑</a></div></footer>
  </div>;
}
