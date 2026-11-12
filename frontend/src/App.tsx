import { lazy, Suspense, useEffect } from 'react';
import { Link, Route, Routes, useLocation, Navigate } from 'react-router-dom';
import { Package, Headphones } from 'lucide-react';
const Landing = lazy(() => import('./pages/Landing'));
const Shop = lazy(() => import('./pages/Shop'));
const Dashboard = lazy(() => import('./pages/Dashboard'));
const Voice = lazy(() => import('./pages/Voice'));

export function Brand({ shop = false }: { shop?: boolean }) {
  return <Link className={`brand ${shop ? 'brand-shop' : ''}`} to={shop ? '/shop' : '/'} aria-label={shop ? 'SwiftCart shop' : 'Pahunch home'}>
    <span className="brand-mark">{shop ? <Package size={23}/> : 'p.'}</span>{shop ? 'SwiftCart' : 'pahunch'}
  </Link>;
}

export default function App() {
  const location = useLocation();
  const landing = location.pathname === '/';
  const operations = location.pathname === '/dashboard';
  useEffect(() => {
    window.scrollTo(0, 0);
    document.title = operations ? 'Pahunch · Delivery operations' : landing ? 'Pahunch · Delivery coordination' : 'SwiftCart · Your delivery';
  }, [location.pathname, operations, landing]);
  const legacyOrder = landing ? new URLSearchParams(location.search).get('order_id') : null;
  if (legacyOrder) return <Navigate to={`/dashboard${location.search}`} replace/>;
  if (landing) return <><a className="skip-link" href="#main">Skip to content</a><Suspense fallback={<main className="page">Opening Pahunch…</main>}><Landing/></Suspense></>;
  return <div className={operations ? 'operations-app' : 'customer-app'}>
    <a className="skip-link" href="#main">Skip to content</a>
    <header className="experience-header">
      <Brand shop={!operations}/>
      <span className="experience-purpose">{operations ? 'Delivery operations' : 'Thoughtfully chosen. Carefully delivered.'}</span>
      <span className="experience-label">{operations ? <><Headphones size={15}/> Operator workspace</> : 'Customer storefront'}</span>
    </header>
    <Suspense fallback={<main id="main" className="page">Opening your workspace…</main>}>
      <Routes>
        <Route path="/dashboard" element={<Dashboard/>}/>
        <Route path="/shop" element={<Shop/>}/>
        <Route path="/voice" element={<Voice/>}/>
        <Route path="*" element={<main id="main" className="page"><h1>Page unavailable</h1><Link to="/shop">Go to SwiftCart</Link></main>}/>
      </Routes>
    </Suspense>
    <footer className="experience-footer"><span>{operations ? 'Pahunch · Delivery coordination' : 'SwiftCart · A fictional demo store'}</span><span>{operations ? 'Confirmed instructions are recorded for the courier.' : 'No real purchase or payment is collected.'}</span></footer>
  </div>;
}
