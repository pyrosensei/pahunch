import { useEffect, useMemo, useRef } from 'react';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import { CatmullRomCurve3, Group, Vector3 } from 'three';

type Props = { phase: number; tilt: number; active: boolean; onContextLost: () => void };
function Route({ points, color, radius = .022 }: { points: number[][]; color: string; radius?: number }) {
  const curve = useMemo(() => new CatmullRomCurve3(points.map(([x, y, z]) => new Vector3(x, y, z))), [points]);
  return <mesh><tubeGeometry args={[curve, 64, radius, 6, false]} /><meshStandardMaterial color={color} roughness={1} /></mesh>;
}
const route = [[-2.7, -1.15, .7], [-1.8, -1.15, 1.7], [0, -1.15, 1.5], [2.1, -1.15, .8], [2.5, -1.15, -1.4]];
const rejected = [[.2, -1.15, 1.5], [1.25, -1.15, 2.1], [2.2, -1.15, 2.2]];
const deliveryPath = new CatmullRomCurve3(route.map(([x,y,z])=>new Vector3(x,y,z)));
function Diorama({ phase, tilt, active, onContextLost }: Props) {
  const parcel = useRef<Group>(null);
  const { gl, invalidate, size, camera } = useThree();
  useEffect(() => {
    const canvas = gl.domElement;
    const loss = (event: Event) => { event.preventDefault(); onContextLost(); };
    canvas.addEventListener('webglcontextlost', loss);
    return () => canvas.removeEventListener('webglcontextlost', loss);
  }, [gl, onContextLost]);
  useEffect(() => {
    if ('zoom' in camera) { camera.zoom = Math.min(size.width / 7.8, size.height / 6); camera.updateProjectionMatrix(); }
    if (active) invalidate();
  }, [size, camera, active, invalidate]);
  useEffect(() => { if (active) invalidate(); }, [phase, tilt, active, invalidate]);
  useFrame((_, delta) => {
    if (!active || !parcel.current) return;
    const group = parcel.current;
    // The parcel travels between real checkpoints; movement explains the workflow.
    const target = deliveryPath.getPoint([.03,.35,.64,.98][phase] || .03);
    const targetX = target.x;
    const targetY = -.55;
    const targetZ = target.z;
    const targetScale = .64;
    const factor = Math.min(1, delta * 7);
    group.position.x += (targetX - group.position.x) * factor;
    group.position.y += (targetY - group.position.y) * factor;
    group.position.z += (targetZ - group.position.z) * factor;
    group.rotation.y += (-.3 + tilt - group.rotation.y) * factor;
    const nextScale = group.scale.x + (targetScale - group.scale.x) * factor;
    group.scale.setScalar(nextScale);
    if (Math.abs(targetX - group.position.x) + Math.abs(targetY - group.position.y) + Math.abs(targetZ - group.position.z) + Math.abs(-.3 + tilt - group.rotation.y) + Math.abs(targetScale - nextScale) > .001) invalidate();
  });
  return <>
    <ambientLight intensity={1.65} /><directionalLight position={[2, 7, 5]} intensity={2.6} color="#fff2df" /><directionalLight position={[-4, 2, -2]} intensity={1.1} color="#e4eee5" />
    <mesh position={[0,-1.33,.25]}><boxGeometry args={[6.7,.23,4.7]}/><meshStandardMaterial color="#eee9dc" roughness={1}/></mesh>
    <group position={[-2.65,-.61,-.45]}>
      <mesh><boxGeometry args={[1.05,1.2,.85]}/><meshStandardMaterial color="#b9c8bd" roughness={1}/></mesh>
      <mesh position={[0,.56,.025]}><boxGeometry args={[1.16,.12,.96]}/><meshStandardMaterial color="#315e52"/></mesh>
      <mesh position={[0,-.16,.432]}><planeGeometry args={[.58,.73]}/><meshStandardMaterial color="#f6f1e5"/></mesh>
      {[0,1,2,3].map(n=><mesh key={n} position={[0,-.43+n*.15,.44]}><planeGeometry args={[.54,.012]}/><meshBasicMaterial color="#a6b9ad"/></mesh>)}
      <mesh position={[0,.37,.44]}><planeGeometry args={[.6,.18]}/><meshBasicMaterial color="#c85b29"/></mesh>
    </group>
    <group position={[2.45,-.45,-1.9]}>
      <mesh><boxGeometry args={[1.18,1.55,.35]}/><meshStandardMaterial color="#d2bf9c" roughness={1}/></mesh>
      <mesh position={[-.07,-.19,.185]}><planeGeometry args={[.6,1.13]}/><meshStandardMaterial color="#315e52"/></mesh>
      <mesh position={[.11,-.2,.21]}><sphereGeometry args={[.025,10,10]}/><meshStandardMaterial color="#eedab6"/></mesh>
      <mesh position={[-.06,-.83,.28]}><boxGeometry args={[1.4,.12,.9]}/><meshStandardMaterial color="#b9b3a0"/></mesh>
    </group>
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -1.21, 0]} scale={[1, .7, 1]}><circleGeometry args={[2, 64]} /><meshBasicMaterial color="#173d33" transparent opacity={.045} depthWrite={false} /></mesh>
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -1.2, 0]} scale={[1, .7, 1]}><circleGeometry args={[1.6, 64]} /><meshBasicMaterial color="#173d33" transparent opacity={.055} depthWrite={false} /></mesh>
    <Route points={route} color={phase ? '#0f766e' : '#a4b8ab'} />
    {phase === 2 && <><Route points={rejected} color="#b9bcb1" radius={.016} /><mesh position={[.2, -.45, 1.5]}><boxGeometry args={[.08, 1.4, .08]} /><meshStandardMaterial color="#0f766e" /></mesh><mesh position={[.2, .25, 1.5]}><boxGeometry args={[.48, .34, .08]} /><meshStandardMaterial color="#0f766e" /></mesh></>}
    {[[-2.7, .7, '#e86b2f'], [2.5, -1.4, '#0f766e']].map(([x, z, color]) => <group key={color} position={[Number(x), -1.1, Number(z)]}><mesh><cylinderGeometry args={[.12, .16, .13, 24]} /><meshStandardMaterial color={String(color)} roughness={1} /></mesh><mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -.025, 0]}><ringGeometry args={[.22, .235, 40]} /><meshBasicMaterial color={String(color)} /></mesh></group>)}
    {phase === 1 && [1.65, 1.95].map(radius => <mesh key={radius} rotation={[-Math.PI / 2, 0, 0]} position={[0, -.98, 0]}><ringGeometry args={[radius, radius + .012, 64]} /><meshBasicMaterial color="#77a99b" transparent opacity={.55} /></mesh>)}
    <group ref={parcel} position={[-2.55, -.55, .8]} scale={.64} rotation={[0, -.3, 0]}>
      <mesh><boxGeometry args={[2.35, 1.8, 1.9]} /><meshStandardMaterial color="#c9a67b" roughness={.94} /></mesh>
      <mesh position={[.2, .906, 0]} rotation={[-Math.PI / 2, 0, 0]}><planeGeometry args={[.4, 1.91]} /><meshStandardMaterial color="#0f766e" roughness={1} /></mesh>
      <mesh position={[.2, 0, .956]}><planeGeometry args={[.4, 1.81]} /><meshStandardMaterial color="#0f766e" roughness={1} /></mesh>
      <mesh position={[-.65, .26, .96]}><planeGeometry args={[.62, .35]} /><meshStandardMaterial color="#e86b2f" roughness={1} /></mesh>
      {[0, 1].map(n => <mesh key={n} position={[-.65, .31 - n * .1, .966]}><planeGeometry args={[n ? .28 : .43, .025]} /><meshBasicMaterial color="#fff6e5" /></mesh>)}
      <mesh position={[.76, -.3, .961]}><planeGeometry args={[.51, .64]} /><meshStandardMaterial color="#fffaf0" roughness={1} /></mesh>
      {[0, 1, 2, 3, 4, 5, 6].map(n => <mesh key={n} position={[.58 + n * .055, -.45, .967]}><planeGeometry args={[n % 2 ? .014 : .027, .15]} /><meshBasicMaterial color="#274e45" /></mesh>)}
      {[0, 1].map(n => <mesh key={n} position={[.76, -.13 - n * .07, .968]}><planeGeometry args={[n ? .23 : .35, .016]} /><meshBasicMaterial color="#758979" /></mesh>)}
      <mesh position={[-1.181, -.1, 0]} rotation={[0, -Math.PI / 2, 0]}><planeGeometry args={[.85, .7]} /><meshStandardMaterial color="#d7bb94" /></mesh>
    </group>
  </>;
}
export default function ParcelScene(props: Props) {
  return <Canvas orthographic camera={{ position: [6, 4.8, 8], zoom: 70 }} dpr={[1, 1.5]} frameloop={props.active ? 'demand' : 'never'} gl={{ antialias: true, alpha: true, powerPreference: 'low-power' }}><Diorama {...props} /></Canvas>;
}
