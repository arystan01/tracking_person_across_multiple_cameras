import { useState } from 'react'
import { Clapperboard, Grid2X2, Maximize2, Play } from 'lucide-react'
import { Modal } from './Workspace'
import camera001 from './warehouse/cam-001.jpg'
import camera002 from './warehouse/cam-002.jpg'
import camera003 from './warehouse/cam-003.jpg'
import camera004 from './warehouse/cam-004.jpg'
import clip001 from './warehouse/clip-001.mp4'
import clip002 from './warehouse/clip-002.mp4'
import clip003 from './warehouse/clip-003.mp4'
import clip004 from './warehouse/clip-004.mp4'

const cameras = [
  { id: '001', poster: camera001, clip: clip001 },
  { id: '002', poster: camera002, clip: clip002 },
  { id: '003', poster: camera003, clip: clip003 },
  { id: '004', poster: camera004, clip: clip004 },
]
const dataset = 'https://raw.githubusercontent.com/arystan01/tracking_person_across_multiple_cameras/8b27b7843c342fbe964027c8b77c58928e6fc308/data/nvidia_mtmc/mtmc_4cam/videos/'
const videoUrl = (camera: string) => dataset + 'Warehouse_Synthetic_Cam' + camera + '.mp4'

export function WarehousePlayer({ camera, variant = 'recording' }: { camera: string; variant?: 'recording' | 'clip' }) {
  const [playbackError, setPlaybackError] = useState(false)
  const recording = cameras.find(({ id }) => id === camera)
  if (!recording) return <p>No warehouse recording is assigned to this camera.</p>
  const isClip = variant === 'clip'
  const src = isClip ? recording.clip : videoUrl(recording.id)
  return <>
    <video className="large-frame warehouse-player" controls playsInline preload="metadata" poster={isClip ? undefined : recording.poster}
      onError={() => setPlaybackError(true)}>
      <source src={src} type="video/mp4" onError={() => setPlaybackError(true)}/>
    </video>
    {playbackError && <p className="notice error" role="alert">The sample video could not play. <a href={src} target="_blank" rel="noreferrer">Open the {isClip ? 'clip' : 'recording'}</a></p>}
  </>
}

export function RecordingWall() {
  const [selected, setSelected] = useState<(typeof cameras)[number]>()

  return <section className="panel evidence-wall" aria-labelledby="wall-title">
    <header className="ops-panel-header">
      <span className="ops-panel-icon"><Grid2X2 size={18}/></span>
      <div><h2 id="wall-title">Camera wall</h2><p>Warehouse sample recordings · Independent of this case</p></div>
      <span className="ops-source-chip"><i/> 4 recordings</span>
    </header>
    <div className="recording-grid">
      {cameras.map((camera) => <button type="button" className="recording-tile" key={camera.id}
        onClick={() => setSelected(camera)}
        aria-label={'Play warehouse camera ' + camera.id}>
        <img src={camera.poster} alt="" width={640} height={360}/>
        <span className="recording-mask"/>
        <span className="recording-camera"><Clapperboard size={13}/> CAM {camera.id}</span>
        <span className="recording-expand"><Maximize2 size={15}/></span>
        <span className="recording-play" aria-hidden="true"><Play size={20} fill="currentColor"/></span>
        <span className="recording-caption"><strong>Warehouse {camera.id}</strong></span>
      </button>)}
    </div>
    {selected && <Modal title={'Warehouse camera ' + selected.id} close={() => setSelected(undefined)}>
      <WarehousePlayer key={selected.id} camera={selected.id}/>
    </Modal>}
  </section>
}
