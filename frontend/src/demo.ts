import type { CaseSummary, CaseView } from './controls'

export const demoCameras = [
  { id: 'CAM-01', name: 'Main Entrance', location: 'Ground floor', position: '0% 0%' },
  { id: 'CAM-02', name: 'East Corridor', location: 'Level 01', position: '100% 0%' },
  { id: 'CAM-03', name: 'Parking Level', location: 'Basement', position: '0% 100%' },
  { id: 'CAM-04', name: 'Service Exit', location: 'Ground floor', position: '100% 100%' },
]

const at = (time: string) => '2026-11-20T' + time + '+09:00'
export const demoSummary: CaseSummary = {
  case_id: 'TRACE-0248', target_id: 'Campus welfare check', site_id: 'North Campus',
  site_version: 'v1', lifecycle: 'SEARCHING', mode: 'LIVE_TRACKING',
  created_at: at('14:24:00'), updated_at: at('14:32:44'),
}

export const demoView: CaseView = {
  case_id: demoSummary.case_id, lifecycle: demoSummary.lifecycle, mode: demoSummary.mode,
  revision: 1, route_revision: 1, done: false, rendered_route_maps: [], watch: [],
  candidates: [
    { candidate_id: 'Sightline A', tracklet_id: 'demo-a', camera_id: 'CAM-01', appearance_score: .96, review_state: 'HOLD', t_start: at('14:32:08'), t_end: at('14:32:19') },
    { candidate_id: 'Sightline B', tracklet_id: 'demo-b', camera_id: 'CAM-02', appearance_score: .87, review_state: 'HOLD', t_start: at('14:30:42'), t_end: at('14:30:55') },
    { candidate_id: 'Sightline C', tracklet_id: 'demo-c', camera_id: 'CAM-04', appearance_score: .72, review_state: 'HOLD', t_start: at('14:29:15'), t_end: at('14:29:22') },
  ],
  search_progress: { jobs_total: 4, jobs_by_state: { RUNNING: 4 }, processed_camera_seconds: 513 },
  errors: [], unobserved_windows: [],
}
