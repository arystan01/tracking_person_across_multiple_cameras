import { useEffect, useRef, useState } from 'react'

const stages = [
  { mode: 'CATCHING_UP', label: 'Search recordings' },
  { mode: 'LIVE_TRACKING', label: 'Track in coverage' },
  { mode: 'WAITING', label: 'Wait for sightings' },
  { mode: 'PAST_SUMMARY', label: 'Review final route' },
] as const

export function SearchStages({ mode, lifecycle, done }: { mode?: string | null; lifecycle: string; done: boolean }) {
  const previous = useRef(mode)
  const [reacquired, setReacquired] = useState(false)
  useEffect(() => {
    setReacquired(previous.current === 'WAITING' && mode === 'LIVE_TRACKING')
    previous.current = mode
  }, [mode])

  const currentMode = lifecycle === 'FAILED' ? undefined
    : done || ['CLOSED', 'EXPORTING', 'SAVED'].includes(lifecycle) ? 'PAST_SUMMARY'
    : mode ?? (['PREPARING', 'SEARCHING'].includes(lifecycle) ? 'CATCHING_UP' : undefined)

  return <section className="search-stage-panel" aria-label="Search stages">
    <ol className="search-stages">
      {stages.map((stage, index) => <li key={stage.mode} aria-current={stage.mode === currentMode ? 'step' : undefined}>
        <span aria-hidden="true">{index + 1}</span>{stage.label}
      </li>)}
    </ol>
    <div className="search-stage-message" role="status">
      {reacquired && currentMode === 'LIVE_TRACKING' ? 'Camera coverage reacquired.'
        : currentMode === 'WAITING' ? 'The search remains open while routing waits for another sighting.'
        : lifecycle === 'FAILED' ? 'Search failed. Check the reported errors.'
        : null}
    </div>
  </section>
}
