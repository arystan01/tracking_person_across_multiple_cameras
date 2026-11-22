import floorMap from './warehouse/floor-map.jpg'

// Sample assignments A–D to Cam001–004. Coordinates are the bottom-center of
// each camera frame projected onto map.png with the upstream NVIDIA calibration.
const anchors: Record<string, readonly [number, number]> = {
  A: [617, 513], B: [684, 571], C: [588, 682], D: [728, 718],
}

export function WarehouseRoute({ route, zoom }: { route: { camera_id: string }[]; zoom: number }) {
  const visits = route.flatMap((point, index) => anchors[point.camera_id]
    ? [{ anchor: anchors[point.camera_id], index, camera: point.camera_id }] : [])
  const routeLine = visits.map(({ anchor }) => anchor.join(',')).join(' ')
  const repeats = new Map<string, number>()

  return <svg className="simulator-map" style={{ width: zoom * 100 + '%' }} viewBox="0 0 1920 1080"
    role="img" aria-label="Full warehouse floor plan with numbered simulated sightings">
    <image href={floorMap} width={1920} height={1080}/>
    <polyline points={routeLine} fill="none" stroke="#fff" strokeWidth={13} strokeLinejoin="round" strokeLinecap="round"/>
    <polyline points={routeLine} fill="none" stroke="#0075e9" strokeWidth={8} strokeLinejoin="round" strokeLinecap="round"/>
    {Object.entries(anchors).map(([id, [x,y]]) => <g key={id}>
      <title>{'Demo camera ' + id}</title>
      <circle cx={x} cy={y} r={11} fill="#24344a" stroke="#fff" strokeWidth={3}/>
      <text x={x} y={y + 5} textAnchor="middle" fill="white" fontSize={14} fontWeight={700}>{id}</text>
    </g>)}
    {visits.map(({ anchor: [x,y], index, camera }) => {
      const offset = repeats.get(camera) ?? 0
      repeats.set(camera, offset + 1)
      const latest = index === visits.at(-1)?.index
      return <g key={index}>
        <title>{'Sighting ' + (index + 1) + ' · Camera ' + camera}</title>
        <path d={`M ${x} ${y-10} L ${x+offset*39} ${y-33}`} stroke="#0075e9" strokeWidth={3}/>
        <g transform={`translate(${x+offset*39}, ${y-48})`}>
          <rect x={-17} y={-17} width={34} height={34} rx={6} fill={latest ? '#0075e9' : '#fff'} stroke="#0075e9" strokeWidth={3}/>
          <text y={7} textAnchor="middle" fill={latest ? '#fff' : '#005ebf'} fontSize={20} fontWeight={700}>{index + 1}</text>
        </g>
      </g>
    })}
  </svg>
}
