import { useEffect, useRef, useState, type FormEvent } from 'react'
import { FolderKanban, ImagePlus, Plus } from 'lucide-react'

const formatted = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})

function defaultKnown() {
  const parts = Object.fromEntries(formatted.formatToParts(Date.now() - 15 * 60_000).map(({ type, value }) => [type, value]))
  return { date: [parts.year, parts.month, parts.day].join('-'), time: parts.hour + ':' + parts.minute }
}

function ReferencePreview({ src, rotation, crop }: Photo) {
  const canvas = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const image = new Image()
    image.onload = () => {
      const target = canvas.current
      if (!target || crop[0] >= crop[2] || crop[1] >= crop[3]) return
      drawPhoto(target, image, { src, rotation, crop }, 400)
    }
    image.src = src
    return () => { image.onload = null }
  }, [src, rotation, crop])
  return <canvas ref={canvas} className="reference-preview" role="img" aria-label="Cropped reference photo preview"/>
}

type Photo = { src: string; rotation: number; crop: number[] }

type IntakeFields = { title: string; camera: string; notes: string; known: string; photo?: Photo }

function drawPhoto(target: HTMLCanvasElement, image: HTMLImageElement, { rotation, crop }: Photo, maxSize: number) {
  const sideways = rotation % 180 !== 0
  const w = sideways ? image.naturalHeight : image.naturalWidth
  const h = sideways ? image.naturalWidth : image.naturalHeight
  const cropWidth = w * (crop[2] - crop[0]) / 100
  const cropHeight = h * (crop[3] - crop[1]) / 100
  const scale = Math.min(1, maxSize / Math.max(cropWidth, cropHeight))
  target.width = Math.max(1, Math.round(cropWidth * scale))
  target.height = Math.max(1, Math.round(cropHeight * scale))
  const ctx = target.getContext('2d')
  if (!ctx) throw Error('Unable to prepare this photo.')
  ctx.fillStyle = '#000'
  ctx.fillRect(0, 0, target.width, target.height)
  ctx.scale(scale, scale)
  ctx.translate(w / 2 - w * crop[0] / 100, h / 2 - h * crop[1] / 100)
  ctx.rotate(rotation * Math.PI / 180)
  ctx.drawImage(image, -image.naturalWidth / 2, -image.naturalHeight / 2)
}

export async function preparePhoto(photo: Photo) {
  const image = new Image()
  image.src = photo.src
  await image.decode().catch(() => { throw Error('Choose a readable JPEG or PNG photo.') })
  const canvas = document.createElement('canvas')
  drawPhoto(canvas, image, photo, 2048)
  return new Promise<File>((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(new File([blob], 'reference.jpg', { type: 'image/jpeg' }))
      else reject(Error('Unable to prepare this photo.'))
    }, 'image/jpeg', 0.9)
  })
}

export function CaseIntake({ cameras, demo = false, submit }: {
  cameras: Record<string, string>
  demo?: boolean
  submit: (fields: IntakeFields) => Promise<void>
}) {
  const [src, setSrc] = useState('')
  const [fileError, setFileError] = useState('')
  const [rotation, setRotation] = useState(0)
  const [crop, setCrop] = useState([0, 0, 100, 100])
  const [known] = useState(defaultKnown)
  const [busy, setBusy] = useState(false)
  const [reading, setReading] = useState(false)
  const [noPhoto, setNoPhoto] = useState(false)
  const [error, setError] = useState('')
  const generation = useRef(0)

  function readFile(file?: File) {
    const requestId = ++generation.current
    setSrc(''); setFileError(''); setReading(false); setRotation(0); setCrop([0, 0, 100, 100])
    if (!file) return
    setError('')
    if (!['image/jpeg', 'image/png'].includes(file.type) && (file.type || !/\.(jpe?g|png)$/i.test(file.name))) {
      setFileError('Choose a JPEG or PNG photo.'); return
    }
    if (file.size > 5_000_000) { setFileError('Use an image smaller than 5 MB.'); return }
    const reader = new FileReader()
    setReading(true)
    reader.onload = async () => {
      if (requestId !== generation.current) return
      const image = new Image()
      image.src = String(reader.result)
      try {
        await image.decode()
        if (requestId === generation.current) setSrc(image.src)
      } catch {
        if (requestId === generation.current) setFileError('Choose a readable JPEG or PNG photo.')
      } finally {
        if (requestId === generation.current) setReading(false)
      }
    }
    reader.onerror = () => {
      if (requestId !== generation.current) return
      setFileError('Unable to read this photo.'); setReading(false)
    }
    reader.readAsDataURL(file)
  }

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    setError('')
    const skipPhoto = demo && noPhoto
    if (!skipPhoto && (!src || crop[0] >= crop[2] || crop[1] >= crop[3])) {
      setError(src ? 'Set the crop edges so left is before right and top is before bottom.' : 'Choose a JPEG or PNG photo.')
      return
    }
    const fields = new FormData(event.currentTarget)
    const value = (name: string) => String(fields.get(name) ?? '')
    const knownTime = value('date') + 'T' + value('time') + ':00+09:00'
    if (!demo && !(Date.parse(knownTime) < Date.now())) {
      setError('Enter a last-known time in the past (KST).')
      return
    }
    setBusy(true)
    try {
      await submit({
        title: value('title'), camera: value('camera'), notes: value('notes'), known: knownTime,
        photo: skipPhoto ? undefined : { src, rotation, crop },
      })
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Please try again.')
    } finally {
      setBusy(false)
    }
  }

  return <form className="intake-grid ops-intake" onSubmit={create}>
    <section className="panel form intake-card">
      <header className="ops-panel-header"><span className="ops-panel-icon"><ImagePlus size={18}/></span><div><h2>Reference photo</h2><p>Choose a photo from your files</p></div></header>
      <div className="intake-fields">
      <label className="file-field"><input type="file" aria-label="Reference photo" disabled={noPhoto || busy} accept="image/jpeg,image/png,.jpg,.jpeg,.png" onChange={(e) => readFile(e.target.files?.[0])}/></label>
      {!noPhoto && fileError && <p className="notice error" role="alert">{fileError}</p>}
      {!noPhoto && src && <>
        <ReferencePreview src={src} crop={crop} rotation={rotation}/>
        <label>Rotation<select disabled={busy} value={rotation} onChange={(e) => setRotation(Number(e.target.value))}>
          {[0, 90, 180, 270].map((value) => <option key={value} value={value}>{value}°</option>)}
        </select></label>
        <fieldset className="crop" disabled={busy}><legend>Crop (%)</legend>
          {['Left', 'Top', 'Right', 'Bottom'].map((side, index) => <label key={side}>{side}
            <input type="number" min={0} max={100} value={crop[index]} onChange={(event) => {
              const value = Number(event.target.value)
              setCrop((previous) => previous.map((part, current) => current === index ? value : part))
            }}/></label>)}
        </fieldset>
      </>}
      </div>
    </section>
    <section className="panel form intake-card">
      <header className="ops-panel-header"><span className="ops-panel-icon"><FolderKanban size={18}/></span><div><h2>Case information</h2><p>Record the last known sighting</p></div></header>
      <div className="intake-fields">
      {demo && <label>Case title<input name="title" required disabled={busy}/></label>}
      <label>Last-known camera<select name="camera" required disabled={busy}>{Object.entries(cameras).map(([id, name]) => <option key={id} value={id}>{id} · {name}</option>)}</select></label>
      <div className="field-row">
        <label>Last-known date<input type="date" name="date" defaultValue={known.date} required disabled={busy}/></label>
        <label>Last-known time<input type="time" name="time" defaultValue={known.time} required disabled={busy}/></label>
      </div>
      <p className="muted">Asia/Seoul. If the time is approximate, enter an earlier estimate.</p>
      {demo && <label>Notes<textarea name="notes" rows={3} disabled={busy}/></label>}
      {demo && <label className="check-row"><input type="checkbox" disabled={busy} name="no_photo" checked={noPhoto} onChange={(event) => { setNoPhoto(event.target.checked); setError('') }}/>Continue without a photo (demo)</label>}
      {error && <p className="notice error" role="alert">{error}</p>}
      {!Object.keys(cameras).length && <p className="notice error" role="alert">Configure the site cameras to create a connected case.</p>}
      </div>
      <footer className="intake-actions"><button type="submit" className="primary" disabled={busy || (reading && !noPhoto) || !Object.keys(cameras).length}><Plus size={16}/>{busy ? (demo ? 'Creating…' : 'Uploading and creating…') : 'Create case'}</button></footer>
    </section>
  </form>
}
