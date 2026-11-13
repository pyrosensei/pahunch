import { useEffect, useMemo, useRef } from 'react';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import { CatmullRomCurve3, Group, Shape, Vector3 } from 'three';

type Props = { phase: number; tilt: number; active: boolean; onContextLost: () => void };

const checkpoints = [
  [-2.7, -1.15, .7], [-1.8, -1.15, 1.7], [0, -1.15, 1.5],
  [2.1, -1.15, .8], [2.5, -1.15, -1.4],
];
const rejected = [[.2, -1.15, 1.5], [1.25, -1.15, 2.1], [2.2, -1.15, 2.2]];
const deliveryPath = new CatmullRomCurve3(checkpoints.map(([x, y, z]) => new Vector3(x, y, z)));

function Route({ points, color }: { points: number[][]; color: string }) {
  const curve = useMemo(() => new CatmullRomCurve3(points.map(([x, y, z]) => new Vector3(x, y, z))), [points]);
  return <mesh>
    <tubeGeometry args={[curve, 64, .035, 8, false]} />
    <meshStandardMaterial color={color} roughness={.7} />
  </mesh>;
}

function ShippingLabel() {
  const outline = useMemo(() => {
    const shape = new Shape();
    shape.moveTo(-.95, -1.3);
    shape.lineTo(.95, -1.3);
    shape.quadraticCurveTo(1.2, -1.3, 1.2, -1.05);
    shape.lineTo(1.2, 1.05);
    shape.quadraticCurveTo(1.2, 1.3, .95, 1.3);
    shape.lineTo(-.95, 1.3);
    shape.quadraticCurveTo(-1.2, 1.3, -1.2, 1.05);
    shape.lineTo(-1.2, -1.05);
    shape.quadraticCurveTo(-1.2, -1.3, -.95, -1.3);
    return shape;
  }, []);
  return <>
    <mesh>
      <extrudeGeometry args={[outline, { depth: .06, bevelEnabled: true, bevelSegments: 3, steps: 1, bevelSize: .025, bevelThickness: .025 }]} />
      <meshStandardMaterial color="#ffffff" roughness={.65} />
    </mesh>
    <mesh position={[0, .73, .1]}>
      <planeGeometry args={[2.4, .55]} />
      <meshStandardMaterial color="#ffd447" />
    </mesh>
    <mesh position={[-.65, .73, .115]}>
      <circleGeometry args={[.14, 32]} />
      <meshBasicMaterial color="#4b3b11" />
    </mesh>
    {[.32, .1, -.12].map((y, index) => <mesh key={y} position={[-.12, y, .1]}>
      <planeGeometry args={[index === 2 ? .8 : 1.5, .055]} />
      <meshBasicMaterial color={index === 0 ? '#424952' : '#d7dbe0'} />
    </mesh>)}
    {Array.from({ length: 17 }, (_, index) => <mesh key={index} position={[-.8 + index * .1, -.77, .1]}>
      <planeGeometry args={[index % 3 === 0 ? .045 : .018, .42]} />
      <meshBasicMaterial color="#343a43" />
    </mesh>)}
    <mesh position={[.76, .31, .1]}>
      <circleGeometry args={[.17, 32]} />
      <meshBasicMaterial color="#ffe9a0" />
    </mesh>
  </>;
}

function DeliveryRoute({ phase, tilt, active, onContextLost }: Props) {
  const label = useRef<Group>(null);
  const { gl, invalidate, size, camera } = useThree();
  useEffect(() => {
    const canvas = gl.domElement;
    const loss = (event: Event) => { event.preventDefault(); onContextLost(); };
    canvas.addEventListener('webglcontextlost', loss);
    return () => canvas.removeEventListener('webglcontextlost', loss);
  }, [gl, onContextLost]);
  useEffect(() => {
    if ('zoom' in camera) {
      camera.zoom = Math.min(size.width / 8.3, size.height / 6.3);
      camera.updateProjectionMatrix();
    }
    if (active) invalidate();
  }, [size, camera, active, invalidate]);
  useEffect(() => { if (active) invalidate(); }, [phase, tilt, active, invalidate]);
  useFrame(({ clock }, delta) => {
    if (!active || !label.current) return;
    const target = deliveryPath.getPoint([.08, .35, .64, .94][phase] ?? .08);
    const factor = 1 - Math.exp(-delta * 5);
    const card = label.current;
    card.position.x += (target.x - card.position.x) * factor;
    card.position.z += (target.z - card.position.z) * factor;
    card.position.y = .15 + Math.sin(clock.elapsedTime * 1.5) * .06;
    card.rotation.y += (.15 + tilt - card.rotation.y) * factor;
    card.rotation.z = -.08 + Math.sin(clock.elapsedTime * .8) * .025;
    invalidate();
  });
  return <>
    <ambientLight intensity={1.8} />
    <directionalLight position={[2, 7, 5]} intensity={2.5} color="#fff7e2" />
    <directionalLight position={[-4, 2, -2]} intensity={1.1} color="#ffffff" />
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -1.28, .25]} scale={[1.3, 1, 1]}>
      <circleGeometry args={[2.75, 80]} />
      <meshStandardMaterial color="#fffdf6" roughness={1} />
    </mesh>
    <Route points={checkpoints} color={phase ? '#d8a51b' : '#e2d3a6'} />
    {phase === 2 && <Route points={rejected} color="#d5d6d9" />}
    {[[-2.7, .7], [2.5, -1.4]].map(([x, z], index) => <group key={x} position={[x, -1.1, z]}>
      <mesh>
        <cylinderGeometry args={[.28, .32, .12, 48]} />
        <meshStandardMaterial color="#ffd447" roughness={.7} />
      </mesh>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, .07, 0]}>
        <ringGeometry args={[.1, .14, 40]} />
        <meshBasicMaterial color={index === 1 && phase === 3 ? '#343a43' : '#fffdf6'} />
      </mesh>
    </group>)}
    <group ref={label} position={[-2.3, .15, 1.1]} scale={.7} rotation={[0, .15, -.08]}>
      <ShippingLabel />
    </group>
  </>;
}

export default function ParcelScene(props: Props) {
  return <Canvas orthographic camera={{ position: [3.5, 3.5, 8], zoom: 70 }}
    dpr={[1, 1.5]} frameloop={props.active ? 'demand' : 'never'}
    gl={{ antialias: true, alpha: true, powerPreference: 'low-power' }}>
    <DeliveryRoute {...props} />
  </Canvas>;
}
