import { useState, useEffect, useCallback } from 'react'
import Dashboard from './Dashboard'

const REFRESH_MS = 12_000
const DEPTH_REFRESH_MS = 6_000

export default function App() {
  const [data, setData] = useState(null)
  const [depth, setDepth] = useState(null)
  const [depthHistory, setDepthHistory] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [lastUpdate, setLastUpdate] = useState(null)

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch('/api/data')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const json = await res.json()
      setData(json)
      setError(null)
      setLastUpdate(new Date())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  const fetchDepth = useCallback(async () => {
    try {
      const [depthRes, histRes] = await Promise.all([
        fetch('/api/depth'),
        fetch('/api/depth/history'),
      ])
      if (depthRes.ok) setDepth(await depthRes.json())
      if (histRes.ok) {
        const h = await histRes.json()
        setDepthHistory(h.snapshots || [])
      }
    } catch (e) {
      // silent fail for depth
    }
  }, [])

  useEffect(() => {
    fetchData()
    fetchDepth()
    const iv1 = setInterval(fetchData, REFRESH_MS)
    const iv2 = setInterval(fetchDepth, DEPTH_REFRESH_MS)
    return () => { clearInterval(iv1); clearInterval(iv2) }
  }, [fetchData, fetchDepth])

  if (loading && !data) {
    return (
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        minHeight: '100vh', background: '#050a18', color: '#64748b',
        fontFamily: "'IBM Plex Sans', sans-serif",
      }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{
            width: 32, height: 32, border: '3px solid #1e293b',
            borderTop: '3px solid #38bdf8', borderRadius: '50%',
            animation: 'spin 0.8s linear infinite', margin: '0 auto 16px',
          }} />
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
          <div>Conectando con Binance y OKX...</div>
        </div>
      </div>
    )
  }

  return <Dashboard data={data} depth={depth} depthHistory={depthHistory} error={error} lastUpdate={lastUpdate} />
}
