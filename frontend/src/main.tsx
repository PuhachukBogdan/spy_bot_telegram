import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from '@/App'
import { loadReportData } from '@/data'
import './index.css'

const root = document.getElementById('root')
if (root) {
  try {
    createRoot(root).render(
      <StrictMode>
        <App data={loadReportData()} />
      </StrictMode>,
    )
  } catch (err) {
    // A blank page is the worst failure mode for a report someone opened from a
    // Slack link: it looks like "no problems this week". Say what went wrong.
    root.textContent = `Report failed to render: ${(err as Error).message}`
  }
}
