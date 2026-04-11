import { useMemo, useState } from 'react'

// ── Helpers ──────────────────────────────────────────────────────────
const fmt = (n, d = 2) => {
  if (n == null || isNaN(n)) return '—'
  return Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })
}
const pct = (n) => {
  if (n == null || isNaN(n)) return '—'
  return (Number(n) * 100).toFixed(4) + '%'
}
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v))

// ── Styles ───────────────────────────────────────────────────────────
const S = {
  card: {
    background: 'linear-gradient(145deg, #0c1224 0%, #111a35 100%)',
    border: '1px solid #1a2544',
    borderRadius: 10,
    padding: '16px 18px',
    transition: 'border-color 0.3s',
  },
  sectionTitle: {
    fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 1.8,
    color: '#4a5980', marginBottom: 12, fontFamily: "'IBM Plex Sans', sans-serif",
  },
  label: { fontSize: 10, color: '#5a6a8a', fontFamily: "'IBM Plex Sans', sans-serif" },
  mono: { fontFamily: "'IBM Plex Mono', monospace" },
  value: { fontSize: 18, fontWeight: 700, color: '#e2e8f0', fontFamily: "'IBM Plex Mono', monospace" },
}

// ── Spark Chart ──────────────────────────────────────────────────────
function Spark({ data, width = 200, height = 52, color = '#38bdf8', showZero = false, valueKey }) {
  if (!data || data.length < 2) {
    return (
      <div style={{ width: '100%', height, background: '#0a1020', borderRadius: 6, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <span style={{ fontSize: 10, color: '#2a3555' }}>Sin datos</span>
      </div>
    )
  }
  const values = data.map(d => {
    if (typeof d === 'number') return d
    if (valueKey && d[valueKey] != null) return Number(d[valueKey])
    if (d.value != null) return Number(d.value)
    if (d.rate != null) return Number(d.rate)
    if (d.ratio != null) return Number(d.ratio)
    return 0
  })
  const min = Math.min(...values), max = Math.max(...values), range = max - min || 1
  const pad = 4
  const pts = values.map((v, i) => [
    pad + (i / (values.length - 1)) * (width - pad * 2),
    pad + (1 - (v - min) / range) * (height - pad * 2),
  ])
  const linePath = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ')
  const areaPath = linePath + ` L${pts[pts.length - 1][0].toFixed(1)},${height} L${pts[0][0].toFixed(1)},${height} Z`
  const zeroY = showZero && min < 0 && max > 0 ? pad + (1 - (0 - min) / range) * (height - pad * 2) : null
  return (
    <svg width={width} height={height} style={{ display: 'block', width: '100%', height }} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
      <rect width={width} height={height} fill="#0a1020" rx={6} />
      <path d={areaPath} fill={color} opacity={0.1} />
      {zeroY != null && <line x1={0} y1={zeroY} x2={width} y2={zeroY} stroke="#2a3555" strokeWidth={0.5} strokeDasharray="4,4" />}
      <path d={linePath} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={pts[pts.length - 1][0]} cy={pts[pts.length - 1][1]} r={3} fill={color} />
      <circle cx={pts[pts.length - 1][0]} cy={pts[pts.length - 1][1]} r={6} fill={color} opacity={0.2} />
    </svg>
  )
}

// ── Dual Bar ─────────────────────────────────────────────────────────
function DualBar({ longPct, shortPct, label }) {
  const l = clamp((longPct || 0.5) * 100, 0, 100), s = clamp((shortPct || 0.5) * 100, 0, 100)
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: '#5a6a8a', marginBottom: 4, ...S.mono }}>
        <span>{label}</span>
        <span style={{ color: '#8a9ac0' }}>L {l.toFixed(1)}% · S {s.toFixed(1)}%</span>
      </div>
      <div style={{ display: 'flex', height: 8, borderRadius: 4, overflow: 'hidden', gap: 2 }}>
        <div style={{ width: `${l}%`, borderRadius: '4px 0 0 4px', transition: 'width 0.6s', background: 'linear-gradient(90deg, #15803d, #22c55e)' }} />
        <div style={{ width: `${s}%`, borderRadius: '0 4px 4px 0', transition: 'width 0.6s', background: 'linear-gradient(90deg, #dc2626, #b91c1c)' }} />
      </div>
    </div>
  )
}

// ── Gauge ────────────────────────────────────────────────────────────
function Gauge({ value, min, max, label, thresholds }) {
  const pctVal = clamp((value - min) / (max - min), 0, 1)
  const startAngle = 135, totalSweep = 270
  const needleAngle = startAngle + pctVal * totalSweep
  const W = 180, H = 110, cx = W / 2, cy = 85, r = 60

  let color = '#64748b'
  if (thresholds) {
    if (value >= thresholds.high) color = '#ef4444'
    else if (value >= thresholds.mid) color = '#f59e0b'
    else if (value <= thresholds.low) color = '#22c55e'
  }

  const toRad = (deg) => (deg - 90) * Math.PI / 180
  const polar = (angle, radius) => ({ x: cx + radius * Math.cos(toRad(angle)), y: cy + radius * Math.sin(toRad(angle)) })
  const arcPath = (start, end, radius) => {
    const s = polar(start, radius), e = polar(end, radius)
    return `M ${s.x} ${s.y} A ${radius} ${radius} 0 ${end - start > 180 ? 1 : 0} 1 ${e.x} ${e.y}`
  }
  const tip = polar(needleAngle, r * 0.78)

  return (
    <div style={{ textAlign: 'center', padding: '4px 0' }}>
      <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`}>
        <path d={arcPath(startAngle, startAngle + totalSweep, r)} fill="none" stroke="#152040" strokeWidth={8} strokeLinecap="round" />
        {pctVal > 0.01 && <path d={arcPath(startAngle, needleAngle, r)} fill="none" stroke={color} strokeWidth={8} strokeLinecap="round" />}
        <line x1={cx} y1={cy} x2={tip.x} y2={tip.y} stroke="#e2e8f0" strokeWidth={2} strokeLinecap="round" />
        <circle cx={cx} cy={cy} r={4} fill="#1a2544" stroke="#e2e8f0" strokeWidth={1.5} />
        <text x={cx} y={cy + 24} textAnchor="middle" fill="#e2e8f0" fontSize={18} fontWeight="700" fontFamily="'IBM Plex Mono', monospace">{fmt(value)}</text>
        <text x={polar(startAngle, r + 14).x} y={polar(startAngle, r + 14).y} textAnchor="middle" fill="#3a4a6a" fontSize={9} fontFamily="'IBM Plex Mono', monospace">{fmt(min, 1)}</text>
        <text x={polar(startAngle + totalSweep, r + 14).x} y={polar(startAngle + totalSweep, r + 14).y} textAnchor="middle" fill="#3a4a6a" fontSize={9} fontFamily="'IBM Plex Mono', monospace">{fmt(max, 1)}</text>
      </svg>
      <div style={{ ...S.label, marginTop: -4 }}>{label}</div>
    </div>
  )
}

// ── Signal Badge ─────────────────────────────────────────────────────
function Signal({ level, text, large = false }) {
  const colors = {
    bullish: { bg: '#062015', border: '#16a34a55', text: '#4ade80', glow: '#22c55e20' },
    bearish: { bg: '#200a0a', border: '#dc262655', text: '#f87171', glow: '#ef444420' },
    neutral: { bg: '#101828', border: '#33415555', text: '#8a9ac0', glow: 'transparent' },
    caution: { bg: '#1a1400', border: '#d9770655', text: '#fbbf24', glow: '#f59e0b20' },
  }
  const c = colors[level] || colors.neutral
  return (
    <div style={{
      display: 'inline-block', padding: large ? '10px 24px' : '4px 12px', borderRadius: 6,
      background: c.bg, border: `1px solid ${c.border}`, color: c.text,
      fontSize: large ? 14 : 11, fontWeight: 600, fontFamily: "'IBM Plex Sans', sans-serif",
      boxShadow: `0 0 20px ${c.glow}`, letterSpacing: 0.3,
    }}>{text}</div>
  )
}

// ── Depth Heatmap ────────────────────────────────────────────────────
function DepthHeatmap({ depth, depthHistory }) {
  if (!depth || !depth.bids || !depth.asks || depth.bids.length === 0) {
    return (
      <div style={{ textAlign: 'center', color: '#4a5980', padding: '30px 0' }}>
        Cargando order book...
      </div>
    )
  }

  const midPrice = depth.midPrice
  const allLevels = [...depth.bids, ...depth.asks]
  const maxQty = Math.max(...allLevels.map(l => l.qty), 1)
  const asks = [...depth.asks].sort((a, b) => b.price - a.price).slice(0, 22)
  const bids = [...depth.bids].sort((a, b) => b.price - a.price).slice(0, 22)

  const renderLevel = (level, side) => {
    const intensity = Math.min(level.qty / maxQty, 1)
    const isWall = side === 'bid'
      ? depth.bidWalls?.some(w => w.price === level.price)
      : depth.askWalls?.some(w => w.price === level.price)
    const barColor = side === 'bid'
      ? `rgba(34, 197, 94, ${0.12 + intensity * 0.55})`
      : `rgba(239, 68, 68, ${0.12 + intensity * 0.55})`

    return (
      <div key={`${side}-${level.price}`} style={{
        display: 'flex', alignItems: 'center', gap: 5, padding: '1.5px 0',
        borderLeft: isWall ? `3px solid ${side === 'bid' ? '#22c55e' : '#ef4444'}` : '3px solid transparent',
        paddingLeft: isWall ? 4 : 0,
      }}>
        <span style={{ ...S.mono, fontSize: 10, color: isWall ? (side === 'bid' ? '#4ade80' : '#f87171') : '#6a7a9a', width: 68, textAlign: 'right', flexShrink: 0 }}>
          ${level.price.toFixed(2)}
        </span>
        <div style={{ flex: 1, height: 10, position: 'relative', borderRadius: 2, background: '#0a1020', overflow: 'hidden' }}>
          <div style={{
            position: 'absolute', top: 0, height: '100%', borderRadius: 2,
            width: `${Math.max(intensity * 100, 2)}%`,
            background: barColor,
            left: side === 'ask' ? 'auto' : 0,
            right: side === 'ask' ? 0 : 'auto',
          }} />
        </div>
        <span style={{ ...S.mono, fontSize: 10, width: 52, textAlign: 'right', flexShrink: 0, color: isWall ? (side === 'bid' ? '#4ade80' : '#f87171') : '#4a5a7a', fontWeight: isWall ? 700 : 400 }}>
          {level.qty >= 1000 ? `${(level.qty / 1000).toFixed(1)}K` : level.qty.toFixed(0)}
        </span>
      </div>
    )
  }

  const imbalanceHistory = depthHistory.map(s => s.bidAskImbalance).filter(v => v != null)
  const imbalance = depth.bidAskImbalance
  const imbalanceColor = imbalance > 0.05 ? '#22c55e' : imbalance < -0.05 ? '#ef4444' : '#8a9ac0'

  return (
    <div>
      {/* Summary row */}
      <div style={{ display: 'flex', gap: 24, marginBottom: 12, flexWrap: 'wrap' }}>
        <div>
          <div style={S.label}>Bid total (±3%)</div>
          <div style={{ ...S.mono, fontSize: 15, fontWeight: 700, color: '#22c55e' }}>{fmt(depth.totalBidQty, 0)} ETH</div>
        </div>
        <div>
          <div style={S.label}>Ask total (±3%)</div>
          <div style={{ ...S.mono, fontSize: 15, fontWeight: 700, color: '#ef4444' }}>{fmt(depth.totalAskQty, 0)} ETH</div>
        </div>
        <div>
          <div style={S.label}>Imbalance bid/ask</div>
          <div style={{ ...S.mono, fontSize: 15, fontWeight: 700, color: imbalanceColor }}>
            {imbalance != null ? `${imbalance > 0 ? '+' : ''}${(imbalance * 100).toFixed(1)}%` : '—'}
            <span style={{ fontSize: 10, fontWeight: 400, marginLeft: 6 }}>
              {imbalance > 0.05 ? 'más soporte' : imbalance < -0.05 ? 'más resistencia' : 'equilibrado'}
            </span>
          </div>
        </div>
        <div>
          <div style={S.label}>Spread</div>
          <div style={{ ...S.mono, fontSize: 15, fontWeight: 600, color: '#8a9ac0' }}>${depth.spread?.toFixed(2) || '—'}</div>
        </div>
      </div>

      {/* Walls */}
      {(depth.bidWalls?.length > 0 || depth.askWalls?.length > 0) && (
        <div style={{ marginBottom: 12, padding: '8px 12px', background: '#080f20', borderRadius: 6, border: '1px solid #1a2544' }}>
          <div style={{ ...S.label, marginBottom: 6 }}>Paredes detectadas — órdenes significativas</div>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            {depth.bidWalls?.slice(0, 4).map(w => (
              <span key={`bw-${w.price}`} style={{ ...S.mono, fontSize: 11, color: '#4ade80', background: '#062015', padding: '2px 8px', borderRadius: 4 }}>
                BID ${w.price.toFixed(0)} · {w.qty.toFixed(0)} ETH
              </span>
            ))}
            {depth.askWalls?.slice(0, 4).map(w => (
              <span key={`aw-${w.price}`} style={{ ...S.mono, fontSize: 11, color: '#f87171', background: '#200a0a', padding: '2px 8px', borderRadius: 4 }}>
                ASK ${w.price.toFixed(0)} · {w.qty.toFixed(0)} ETH
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Heatmap grid */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr auto 1fr', gap: 8, alignItems: 'start' }}>
        <div>
          <div style={{ ...S.label, color: '#ef4444', marginBottom: 4 }}>ASKS — Resistencia</div>
          {asks.map(l => renderLevel(l, 'ask'))}
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '0 8px', paddingTop: 18 }}>
          <div style={{ ...S.mono, fontSize: 11, color: '#e2e8f0', fontWeight: 700, whiteSpace: 'nowrap' }}>${midPrice?.toFixed(2)}</div>
          <div style={{ width: 1, height: 36, background: '#1a2544', margin: '6px 0' }} />
          <div style={{ ...S.label, fontSize: 9 }}>mid</div>
        </div>
        <div>
          <div style={{ ...S.label, color: '#22c55e', marginBottom: 4 }}>BIDS — Soporte</div>
          {bids.map(l => renderLevel(l, 'bid'))}
        </div>
      </div>

      {/* Imbalance history */}
      {imbalanceHistory.length >= 2 && (
        <div style={{ marginTop: 12 }}>
          <div style={S.label}>Imbalance bid/ask histórico (positivo = más soporte)</div>
          <div style={{ marginTop: 4 }}>
            <Spark data={imbalanceHistory} height={40} color="#a78bfa" showZero />
          </div>
        </div>
      )}
    </div>
  )
}

// ── Options Panel (Deribit + Bybit + OKX GEX) ────────────────────────
function OptionsPanel({ options, marketVolume }) {
  const [selectedExpiry, setSelectedExpiry] = useState('all')

  if (!options || options.gammaFlip == null) {
    return <div style={{ color: '#4a5980', fontSize: 12 }}>Cargando datos de opciones (Deribit+Bybit+OKX)...</div>
  }

  const { gammaFlip, flipPosition, totalGex, callWall, putWall,
          callWallDist, putWallDist, maxPain, maxPainDist,
          nearestExpiry, gexByStrike, gexByExpiry, expiryList,
          zoneAnalysis, spotPrice } = options

  const aboveFlip = flipPosition === 'above'
  const flipColor  = aboveFlip ? '#22c55e' : '#ef4444'
  const flipBg     = aboveFlip ? '#062015' : '#200a0a'
  const flipBorder = aboveFlip ? '#16a34a55' : '#dc262655'

  // Active bars: all expirations or selected one
  const activeBars = selectedExpiry === 'all'
    ? (gexByStrike || [])
    : ((gexByExpiry || {})[selectedExpiry] || [])

  const gexMax = Math.max(...activeBars.map(b => Math.abs(b.gex)), 0.001)

  // GEX total for selected expiry
  const gexSelected = selectedExpiry === 'all'
    ? totalGex
    : activeBars.reduce((s, b) => s + b.gex, 0)

  return (
    <div>
      {/* Gamma regime banner */}
      {(() => {
        const flipDist = gammaFlip && spotPrice ? Math.abs((spotPrice - gammaFlip) / spotPrice * 100) : null
        const nearFlip = flipDist != null && flipDist < 2
        const gexSign = totalGex >= 0
        // Zone analysis to determine actual dominance
        const belowZone = zoneAnalysis?.belowFlip
        const aboveZone = zoneAnalysis?.aboveFlip

        let bannerText = ''
        if (nearFlip) {
          bannerText = `Cerca del gamma flip (${flipDist?.toFixed(1)}%) — zona de transición, combustible puede cambiar de dirección`
        } else if (aboveFlip) {
          bannerText = 'Zona call heavy — dealers compran spot si sube → combustible alcista'
        } else {
          bannerText = 'Zona put heavy — dealers venden spot si baja → combustible bajista'
        }
        // Override if total GEX contradicts position
        if (!aboveFlip && gexSign && totalGex > 100) {
          bannerText = `Apenas bajo el flip, pero GEX total +${totalGex?.toFixed(0)}M (calls dominan globalmente) — combustible alcista si recupera $${gammaFlip?.toFixed(0)}`
        } else if (aboveFlip && !gexSign && totalGex < -100) {
          bannerText = `Sobre el flip, pero GEX total ${totalGex?.toFixed(0)}M (puts dominan globalmente) — combustible bajista si pierde $${gammaFlip?.toFixed(0)}`
        }

        return (
          <div style={{ padding: '10px 14px', background: flipBg, border: `1px solid ${flipBorder}`, borderRadius: 7, marginBottom: 14 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
              <div>
                <span style={{ ...S.mono, fontSize: 13, fontWeight: 700, color: flipColor }}>
                  {aboveFlip ? '✓ SOBRE el Gamma Flip' : '⚠ BAJO el Gamma Flip'}
                </span>
                <span style={{ ...S.mono, fontSize: 11, color: '#6a7aa0', marginLeft: 10 }}>
                  Flip en ${gammaFlip?.toFixed(0)}
                </span>
              </div>
              <span style={{ fontSize: 11, color: flipColor }}>
                {bannerText}
              </span>
            </div>
          </div>
        )
      })()}

      {/* Gamma flip direction indicator */}
      {gammaFlip != null && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 0, marginBottom: 14, fontSize: 10, fontFamily: 'monospace' }}>
          <div style={{ flex: 1, textAlign: 'center', padding: '6px 10px', background: '#1a0808', borderRadius: '6px 0 0 6px', border: '1px solid #ef444425' }}>
            <span style={{ color: '#ef4444' }}>🔴 PUTS dominan</span>
            <span style={{ color: '#4a5a7a', marginLeft: 6 }}>← bajo ${gammaFlip?.toFixed(0)}</span>
            <span style={{ color: '#ef444490', marginLeft: 6 }}>dealers venden si baja</span>
          </div>
          <div style={{ padding: '6px 12px', background: '#0d1a30', border: '1px solid #3b82f640', zIndex: 1 }}>
            <span style={{ color: '#3b82f6', fontWeight: 700 }}>FLIP ${gammaFlip?.toFixed(0)}</span>
          </div>
          <div style={{ flex: 1, textAlign: 'center', padding: '6px 10px', background: '#081a0e', borderRadius: '0 6px 6px 0', border: '1px solid #22c55e25' }}>
            <span style={{ color: '#4a5a7a' }}>sobre ${gammaFlip?.toFixed(0)} →</span>
            <span style={{ color: '#22c55e', marginLeft: 6 }}>🟢 CALLS dominan</span>
            <span style={{ color: '#22c55e90', marginLeft: 6 }}>dealers compran si sube</span>
          </div>
        </div>
      )}

      {/* Key levels row */}
      <div style={{ display: 'flex', gap: 16, marginBottom: 14, flexWrap: 'wrap' }}>
        <div>
          <div style={S.label}>Gamma Flip</div>
          <div style={{ ...S.mono, fontSize: 15, fontWeight: 700, color: flipColor }}>${gammaFlip?.toFixed(0)}</div>
          <div style={{ ...S.mono, fontSize: 9, color: '#3a4a6a' }}>
            {gammaFlip && spotPrice ? `${((gammaFlip - spotPrice) / spotPrice * 100).toFixed(1)}% del precio` : ''}
          </div>
        </div>
        <div>
          <div style={S.label}>Call Wall</div>
          <div style={{ ...S.mono, fontSize: 15, fontWeight: 700, color: '#ef4444' }}>
            {callWall ? `$${callWall.toFixed(0)}` : '—'}
          </div>
          <div style={{ ...S.mono, fontSize: 9, color: '#3a4a6a' }}>
            {callWallDist != null ? `+${callWallDist.toFixed(1)}%` : ''}
          </div>
        </div>
        <div>
          <div style={S.label}>Put Wall</div>
          <div style={{ ...S.mono, fontSize: 15, fontWeight: 700, color: '#22c55e' }}>
            {putWall ? `$${putWall.toFixed(0)}` : '—'}
          </div>
          <div style={{ ...S.mono, fontSize: 9, color: '#3a4a6a' }}>
            {putWallDist != null ? `${putWallDist.toFixed(1)}%` : ''}
          </div>
        </div>
        <div>
          <div style={S.label}>Max Pain {nearestExpiry}</div>
          <div style={{ ...S.mono, fontSize: 15, fontWeight: 700, color: '#f59e0b' }}>
            {maxPain ? `$${maxPain.toFixed(0)}` : '—'}
          </div>
          <div style={{ ...S.mono, fontSize: 9, color: '#3a4a6a' }}>
            {maxPainDist != null ? `${maxPainDist > 0 ? '+' : ''}${maxPainDist.toFixed(1)}%` : ''}
          </div>
        </div>
        <div>
          <div style={S.label}>GEX {selectedExpiry === 'all' ? 'Total' : selectedExpiry}</div>
          <div style={{ ...S.mono, fontSize: 15, fontWeight: 700, color: gexSelected >= 0 ? '#22c55e' : '#ef4444' }}>
            {gexSelected >= 0 ? '+' : ''}{gexSelected?.toFixed(1)}M
          </div>
          <div style={{ ...S.mono, fontSize: 9, color: '#3a4a6a' }}>USD</div>
        </div>
        {/* GEX relevance vs combined market volume */}
        {marketVolume?.combined24h && totalGex != null && spotPrice && (() => {
          const vol24h = marketVolume.combined24h
          const hedgePer1Pct = Math.abs(totalGex) * 0.01  // already in M
          const hourlyVol = vol24h / 24 / 1e6  // in M
          const hedgePct = hourlyVol > 0 ? (hedgePer1Pct / hourlyVol * 100) : 0
          const impact = hedgePct >= 10 ? 'ALTO' : hedgePct >= 3 ? 'MODERADO' : 'BAJO'
          const impactColor = hedgePct >= 10 ? '#ef4444' : hedgePct >= 3 ? '#f59e0b' : '#22c55e'
          return (
            <div>
              <div style={S.label}>Impacto GEX</div>
              <div style={{ ...S.mono, fontSize: 15, fontWeight: 700, color: impactColor }}>{impact}</div>
              <div style={{ ...S.mono, fontSize: 9, color: '#3a4a6a' }}>
                {hedgePer1Pct.toFixed(1)}M/1% vs {hourlyVol.toFixed(0)}M/h ({hedgePct.toFixed(1)}%)
              </div>
            </div>
          )
        })()}
      </div>

      {/* GEX impact explanation + volume breakdown */}
      {marketVolume?.combined24h && totalGex != null && spotPrice && (() => {
        const vol24h = marketVolume.combined24h
        const bd = marketVolume.breakdown || {}
        const hedgePer1Pct = Math.abs(totalGex) * 0.01
        const hourlyVol = vol24h / 24 / 1e6
        const hedgePct = hourlyVol > 0 ? (hedgePer1Pct / hourlyVol * 100) : 0
        return (
          <div style={{ padding: '8px 12px', background: '#080e1c', borderRadius: 6, fontSize: 10, color: '#6a8aaa', marginBottom: 14, lineHeight: 1.6 }}>
            <div style={{ marginBottom: 6 }}>
              Por cada <span style={{ color: '#c0d0e0', fontWeight: 700 }}>1% de movimiento</span>, los dealers deben hedgear ~<span style={{ color: '#c0d0e0', fontWeight: 700 }}>${hedgePer1Pct.toFixed(1)}M</span> en spot/futuros.
              {' '}Vol combinado por hora: <span style={{ color: '#c0d0e0', fontWeight: 700 }}>${hourlyVol.toFixed(0)}M</span>.
              {' '}Hedging = <span style={{ color: hedgePct >= 10 ? '#ef4444' : hedgePct >= 3 ? '#f59e0b' : '#22c55e', fontWeight: 700 }}>{hedgePct.toFixed(1)}%</span> del vol/hora
              {hedgePct >= 10
                ? ' → flujo de cobertura MUY significativo, puede mover precio por sí solo'
                : hedgePct >= 3
                ? ' → flujo de cobertura notable, amplifica el movimiento de takers'
                : ' → flujo de cobertura bajo, el precio lo mueven los takers, no los dealers'}
            </div>
            <div style={{ fontSize: 9, color: '#4a5a7a', display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              <span>Vol 24h total: ${(vol24h/1e9).toFixed(2)}B</span>
              <span>Bn Perp: ${(bd.binancePerp/1e9).toFixed(2)}B</span>
              <span>Bn Spot: ${(bd.binanceSpot/1e9).toFixed(2)}B</span>
              <span>Bybit: ${(bd.bybitPerp/1e9).toFixed(2)}B</span>
              <span>OKX: ${(bd.okxPerp/1e9).toFixed(2)}B</span>
              <span>HL: ${(bd.hyperliquid/1e9).toFixed(2)}B</span>
              <span style={{ color: '#6a7aa0' }}>| GEX: Deribit+Bybit+OKX</span>
            </div>
          </div>
        )
      })()}

      {/* Expiry selector */}
      {expiryList && expiryList.length > 0 && (
        <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap', marginBottom: 12 }}>
          <span style={{ ...S.label, alignSelf: 'center', marginRight: 4 }}>Vencimiento:</span>
          {/* All button */}
          <button
            onClick={() => setSelectedExpiry('all')}
            style={{
              ...S.mono, fontSize: 10, padding: '3px 8px', borderRadius: 4, cursor: 'pointer', border: 'none',
              background: selectedExpiry === 'all' ? '#3b82f6' : '#0d1830',
              color: selectedExpiry === 'all' ? '#fff' : '#6a8aaa',
            }}
          >Todos</button>
          {expiryList.map(({ label, dte }) => (
            <button
              key={label}
              onClick={() => setSelectedExpiry(label)}
              style={{
                ...S.mono, fontSize: 10, padding: '3px 8px', borderRadius: 4, cursor: 'pointer', border: 'none',
                background: selectedExpiry === label ? '#3b82f6' : '#0d1830',
                color: selectedExpiry === label ? '#fff' : dte <= 7 ? '#f59e0b' : '#6a8aaa',
              }}
            >
              {label} <span style={{ fontSize: 8, opacity: 0.7 }}>{dte}d</span>
            </button>
          ))}
        </div>
      )}

      {/* Zone analysis — source of gamma */}
      {zoneAnalysis && selectedExpiry === 'all' && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ ...S.label, marginBottom: 8 }}>Fuente del gamma — OI calls vs puts por zona</div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {[
              { key: 'aboveFlip', label: `Sobre el flip (>${gammaFlip?.toFixed(0) || '?'})` },
              { key: 'belowFlip', label: `Bajo el flip (<${gammaFlip?.toFixed(0) || '?'})` },
            ].map(({ key, label }) => {
              const z = zoneAnalysis[key]
              if (!z) return null
              const isPutHeavy = z.dominant === 'puts'
              const domColor   = isPutHeavy ? '#ef4444' : '#22c55e'
              const bgColor    = isPutHeavy ? '#1a0808' : '#081a0e'
              const borderCol  = isPutHeavy ? '#ef444430' : '#22c55e30'
              const totalOi    = (z.callOi || 0) + (z.putOi || 0)
              return (
                <div key={key} style={{ flex: '1 1 220px', background: bgColor, border: `1px solid ${borderCol}`, borderRadius: 7, padding: '10px 12px' }}>
                  <div style={{ ...S.mono, fontSize: 10, color: '#6a7aa0', marginBottom: 6 }}>{label}</div>
                  {/* C/P bar */}
                  <div style={{ display: 'flex', height: 8, borderRadius: 4, overflow: 'hidden', marginBottom: 8 }}>
                    <div style={{ width: `${z.callPct}%`, background: '#22c55e', transition: 'width 0.3s' }} />
                    <div style={{ width: `${z.putPct}%`,  background: '#ef4444', transition: 'width 0.3s' }} />
                  </div>
                  {/* Numbers */}
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                    <div>
                      <span style={{ ...S.mono, fontSize: 9, color: '#22c55e' }}>C </span>
                      <span style={{ ...S.mono, fontSize: 11, color: '#22c55e', fontWeight: 700 }}>{(z.callOi/1000).toFixed(1)}K</span>
                      <span style={{ ...S.mono, fontSize: 9, color: '#4a5a7a', marginLeft: 3 }}>{z.callPct}%</span>
                    </div>
                    <div>
                      <span style={{ ...S.mono, fontSize: 9, color: '#ef4444' }}>P </span>
                      <span style={{ ...S.mono, fontSize: 11, color: '#ef4444', fontWeight: 700 }}>{(z.putOi/1000).toFixed(1)}K</span>
                      <span style={{ ...S.mono, fontSize: 9, color: '#4a5a7a', marginLeft: 3 }}>{z.putPct}%</span>
                    </div>
                    <div style={{ ...S.mono, fontSize: 9, color: z.netGex >= 0 ? '#22c55e' : '#ef4444' }}>
                      GEX {z.netGex >= 0 ? '+' : ''}{z.netGex?.toFixed(0)}M
                    </div>
                  </div>
                  {/* Dominant / effect */}
                  <div style={{ fontSize: 10, color: domColor, fontWeight: 600 }}>
                    {isPutHeavy ? '📛 PUT heavy' : '📗 CALL heavy'}
                    <span style={{ color: '#4a5a7a', fontWeight: 400, marginLeft: 6 }}>
                      {isPutHeavy
                        ? '→ dealers short puts → venden spot si baja → combustible bajista'
                        : '→ dealers short calls → compran spot si sube → combustible alcista'}
                    </span>
                  </div>
                </div>
              )
            })}
          </div>
          {/* Effect summary */}
          <div style={{ marginTop: 8, padding: '8px 12px', background: '#080e1c', borderRadius: 6, fontSize: 11, color: '#8090b0', lineHeight: 1.6 }}>
            {zoneAnalysis.belowFlip?.dominant === 'puts' && zoneAnalysis.aboveFlip?.dominant === 'calls'
              ? '⚡ Estructura clásica: calls sobre el flip + puts bajo el flip. Sobre el flip, dealers compran spot si sube (combustible alcista). Bajo el flip, dealers venden spot si baja (combustible bajista en cascada).'
              : zoneAnalysis.belowFlip?.dominant === 'calls'
              ? '⚠ Inusual: calls dominan también bajo el flip. Combustible alcista amplio en ambas zonas, pero sin colchón bajista si cae.'
              : '📊 Posicionamiento mixto — revisar vencimientos individuales para mayor claridad.'}
          </div>
        </div>
      )}

      {/* GEX bar chart by strike */}
      {activeBars.length > 0 ? (
        <div>
          <div style={{ ...S.label, marginBottom: 6 }}>
            GEX por strike — 🟢 calls dominan (combustible alcista) · 🔴 puts dominan (combustible bajista)
            {selectedExpiry !== 'all' && <span style={{ color: '#3b82f6', marginLeft: 6 }}>· {selectedExpiry}</span>}
          </div>
          <div style={{ maxHeight: 320, overflowY: 'auto' }}>
            {[...activeBars].sort((a, b) => b.strike - a.strike).map(bar => {
              const isCurrent = spotPrice && Math.abs(bar.strike - spotPrice) < 30
              const isFlip    = gammaFlip && Math.abs(bar.strike - gammaFlip) < 30
              const barPct    = Math.abs(bar.gex) / gexMax * 100
              const barColor  = bar.gex >= 0
                ? `rgba(34,197,94,${0.3 + Math.abs(bar.gex)/gexMax*0.7})`
                : `rgba(239,68,68,${0.3 + Math.abs(bar.gex)/gexMax*0.7})`
              // Put/call OI ratio for this bar
              const cOi = bar.callOi || 0
              const pOi = bar.putOi  || 0
              const totalOi = cOi + pOi
              const putDom  = pOi > cOi
              return (
                <div key={bar.strike} style={{
                  display: 'flex', alignItems: 'center', gap: 6, padding: '2px 0',
                  borderLeft: isCurrent ? '3px solid #38bdf8' : isFlip ? `3px solid ${flipColor}` : '3px solid transparent',
                  paddingLeft: (isCurrent || isFlip) ? 4 : 0,
                }}>
                  <span style={{ ...S.mono, fontSize: 10, width: 60, textAlign: 'right', flexShrink: 0, color: isCurrent ? '#38bdf8' : isFlip ? flipColor : '#5a6a7a', fontWeight: (isCurrent || isFlip) ? 700 : 400 }}>
                    ${bar.strike}
                  </span>
                  <div style={{ flex: 1, height: 10, position: 'relative', background: '#0a1020', borderRadius: 2 }}>
                    <div style={{ position: 'absolute', top: 0, height: '100%', borderRadius: 2, width: `${barPct}%`, background: barColor, left: bar.gex >= 0 ? '50%' : 'auto', right: bar.gex < 0 ? '50%' : 'auto' }} />
                    <div style={{ position: 'absolute', left: '50%', top: 0, width: 1, height: '100%', background: '#1a2544' }} />
                  </div>
                  <span style={{ ...S.mono, fontSize: 9, width: 52, textAlign: 'right', flexShrink: 0, color: bar.gex >= 0 ? '#22c55e' : '#ef4444' }}>
                    {bar.gex >= 0 ? '+' : ''}{bar.gex.toFixed(2)}M
                  </span>
                  {/* C/P OI pill */}
                  {totalOi > 0 && (
                    <span style={{ ...S.mono, fontSize: 8, flexShrink: 0, color: putDom ? '#ef444490' : '#22c55e90', width: 54, textAlign: 'left' }}>
                      C{(cOi/1000).toFixed(1)}K P{(pOi/1000).toFixed(1)}K
                    </span>
                  )}
                </div>
              )
            })}
          </div>
          <div style={{ display: 'flex', gap: 16, marginTop: 6, fontSize: 10, color: '#3a4a6a' }}>
            <span style={{ color: '#38bdf8' }}>▌ Precio actual</span>
            <span style={{ color: flipColor }}>▌ Gamma flip</span>
            <span style={{ color: '#6a7aa0' }}>C=Call OI · P=Put OI por strike</span>
          </div>
        </div>
      ) : (
        <div style={{ color: '#4a5980', fontSize: 11 }}>Sin datos GEX para este vencimiento en rango ±15%</div>
      )}

      {/* GEX CURVE — price (Y) vs GEX (X) */}
      {activeBars.length > 2 && (() => {
        const sorted = [...activeBars].sort((a, b) => a.strike - b.strike)
        const W = 700, H = 340, padL = 55, padR = 50, padT = 20, padB = 25
        const chartW = W - padL - padR
        const chartH = H - padT - padB

        const minStrike = sorted[0].strike
        const maxStrike = sorted[sorted.length - 1].strike
        const maxAbsGex = Math.max(...sorted.map(b => Math.abs(b.gex)), 0.001)

        const yScale = (strike) => padT + chartH - ((strike - minStrike) / (maxStrike - minStrike)) * chartH
        const xScale = (gex) => padL + chartW / 2 + (gex / maxAbsGex) * (chartW / 2)
        const zeroX = padL + chartW / 2

        // Build smooth path
        const points = sorted.map(b => ({ x: xScale(b.gex), y: yScale(b.strike), gex: b.gex, strike: b.strike }))

        // Area fill: positive (right of zero) and negative (left of zero)
        const areaPath = points.map((p, i) =>
          `${i === 0 ? 'M' : 'L'} ${p.x} ${p.y}`
        ).join(' ')
        const areaClose = `L ${zeroX} ${points[points.length - 1].y} L ${zeroX} ${points[0].y} Z`

        // Current price Y position
        const priceY = spotPrice ? yScale(spotPrice) : null
        // Gamma flip Y position
        const flipY = gammaFlip ? yScale(gammaFlip) : null

        // Y-axis labels (every ~$100)
        const step = maxStrike - minStrike > 400 ? 100 : 50
        const yLabels = []
        for (let s = Math.ceil(minStrike / step) * step; s <= maxStrike; s += step) {
          yLabels.push(s)
        }

        // X-axis labels
        const xTicks = [-maxAbsGex, -maxAbsGex / 2, 0, maxAbsGex / 2, maxAbsGex]

        return (
          <div style={{ marginTop: 16 }}>
            <div style={{ ...S.label, marginBottom: 8 }}>
              Curva GEX — perfil de gamma por precio
              {selectedExpiry !== 'all' && <span style={{ color: '#3b82f6', marginLeft: 6 }}>· {selectedExpiry}</span>}
            </div>
            <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ background: '#060d1a', borderRadius: 6 }}>
              {/* Grid lines */}
              {yLabels.map(s => (
                <g key={s}>
                  <line x1={padL} y1={yScale(s)} x2={W - padR} y2={yScale(s)} stroke="#0f1a2e" strokeWidth={0.5} />
                  <text x={padL - 4} y={yScale(s) + 3} textAnchor="end" fill="#3a4a6a" fontSize={8} fontFamily="monospace">${s}</text>
                </g>
              ))}
              {/* X-axis ticks */}
              {xTicks.map((v, i) => (
                <g key={i}>
                  <line x1={xScale(v)} y1={padT} x2={xScale(v)} y2={H - padB} stroke={v === 0 ? '#1a2a44' : '#0a1020'} strokeWidth={v === 0 ? 1.5 : 0.5} />
                  <text x={xScale(v)} y={H - padB + 12} textAnchor="middle" fill="#3a4a6a" fontSize={7} fontFamily="monospace">
                    {v === 0 ? '0' : `${v > 0 ? '+' : ''}${v.toFixed(0)}M`}
                  </text>
                </g>
              ))}
              {/* Zero line label */}
              <text x={zeroX} y={padT - 6} textAnchor="middle" fill="#4a5a7a" fontSize={7} fontFamily="monospace">GEX (M USD)</text>

              {/* Area fill — split green/red via clip paths */}
              <defs>
                <clipPath id="clipRight"><rect x={zeroX} y={padT} width={chartW / 2 + padR} height={chartH} /></clipPath>
                <clipPath id="clipLeft"><rect x={padL} y={padT} width={chartW / 2} height={chartH} /></clipPath>
              </defs>
              <path d={`${areaPath} ${areaClose}`} fill="rgba(34,197,94,0.12)" clipPath="url(#clipRight)" />
              <path d={`${areaPath} ${areaClose}`} fill="rgba(239,68,68,0.12)" clipPath="url(#clipLeft)" />

              {/* Curve line — gradient from red to green */}
              {points.map((p, i) => {
                if (i === 0) return null
                const prev = points[i - 1]
                const avgGex = (p.gex + prev.gex) / 2
                const color = avgGex >= 0 ? '#22c55e' : '#ef4444'
                return <line key={i} x1={prev.x} y1={prev.y} x2={p.x} y2={p.y} stroke={color} strokeWidth={2} />
              })}

              {/* Data points */}
              {points.map((p, i) => (
                <circle key={i} cx={p.x} cy={p.y} r={3} fill={p.gex >= 0 ? '#22c55e' : '#ef4444'} stroke="#060d1a" strokeWidth={1} />
              ))}

              {/* Current price line */}
              {priceY != null && priceY >= padT && priceY <= H - padB && (
                <g>
                  <line x1={padL} y1={priceY} x2={W - padR} y2={priceY} stroke="#38bdf8" strokeWidth={1.5} strokeDasharray="4 3" />
                  <rect x={W - padR + 2} y={priceY - 7} width={44} height={14} rx={3} fill="#38bdf8" />
                  <text x={W - padR + 24} y={priceY + 3} textAnchor="middle" fill="#000" fontSize={8} fontWeight="bold" fontFamily="monospace">
                    ${spotPrice?.toFixed(0)}
                  </text>
                </g>
              )}

              {/* Gamma flip line */}
              {flipY != null && flipY >= padT && flipY <= H - padB && (
                <g>
                  <line x1={padL} y1={flipY} x2={W - padR} y2={flipY} stroke={flipColor} strokeWidth={1.5} strokeDasharray="6 3" />
                  <rect x={W - padR + 2} y={flipY - 7} width={44} height={14} rx={3} fill={flipColor} />
                  <text x={W - padR + 24} y={flipY + 3} textAnchor="middle" fill="#000" fontSize={8} fontWeight="bold" fontFamily="monospace">
                    FLIP
                  </text>
                </g>
              )}

              {/* Zone labels */}
              <text x={padL + chartW * 0.75} y={padT + 12} textAnchor="middle" fill="#22c55e40" fontSize={9} fontFamily="monospace">CALLS</text>
              <text x={padL + chartW * 0.25} y={padT + 12} textAnchor="middle" fill="#ef444440" fontSize={9} fontFamily="monospace">PUTS</text>
            </svg>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 4, fontSize: 9, color: '#3a4a6a', fontFamily: 'monospace' }}>
              <span>← Combustible bajista (puts) | Combustible alcista (calls) →</span>
              <span>
                <span style={{ color: '#38bdf8' }}>--- Precio actual</span>
                {' · '}
                <span style={{ color: flipColor }}>--- Gamma flip</span>
              </span>
            </div>
          </div>
        )
      })()}
    </div>
  )
}

// ── Volatility Panel ─────────────────────────────────────────────────
function VolatilityPanel({ volatility }) {
  if (!volatility || volatility.rv24h == null) {
    return <div style={{ color: '#4a5980', fontSize: 12 }}>Cargando volatilidad...</div>
  }
  const { rv4h, rv24h, rv7d, percentile, ratio, history } = volatility

  let regime = '', regimeColor = '#8a9ac0'
  if (percentile != null) {
    if (percentile <= 20)      { regime = 'MUY BAJA';  regimeColor = '#22c55e' }
    else if (percentile <= 40) { regime = 'BAJA';      regimeColor = '#38bdf8' }
    else if (percentile <= 60) { regime = 'MEDIA';     regimeColor = '#8a9ac0' }
    else if (percentile <= 80) { regime = 'ALTA';      regimeColor = '#f59e0b' }
    else                       { regime = 'MUY ALTA';  regimeColor = '#ef4444' }
  }

  let compression = '', compressionColor = '#8a9ac0', compressionLevel = 'neutral'
  if (ratio != null) {
    if (ratio < 0.7)      { compression = 'COMPRIMIDA — Expansión probable'; compressionColor = '#22c55e'; compressionLevel = 'bullish' }
    else if (ratio < 0.9) { compression = 'Ligeramente comprimida'; compressionColor = '#38bdf8'; compressionLevel = 'neutral' }
    else if (ratio > 1.5) { compression = 'EN EXPANSIÓN — Movimiento en curso'; compressionColor = '#ef4444'; compressionLevel = 'caution' }
    else if (ratio > 1.2) { compression = 'Expandiéndose'; compressionColor = '#f59e0b'; compressionLevel = 'caution' }
    else                  { compression = 'Normal'; compressionLevel = 'neutral' }
  }

  return (
    <div>
      <div style={{ display: 'flex', gap: 20, marginBottom: 12, flexWrap: 'wrap' }}>
        {[['RV 4h', rv4h], ['RV 24h', rv24h], ['RV 7d', rv7d]].map(([l, v]) => (
          <div key={l}>
            <div style={S.label}>{l}</div>
            <div style={{ ...S.mono, fontSize: 16, fontWeight: 700, color: '#e2e8f0' }}>{v != null ? v.toFixed(1) + '%' : '—'}</div>
          </div>
        ))}
        <div>
          <div style={S.label}>Ratio 4h/24h</div>
          <div style={{ ...S.mono, fontSize: 16, fontWeight: 700, color: compressionColor }}>{ratio != null ? ratio.toFixed(2) : '—'}</div>
        </div>
      </div>

      {percentile != null && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
            <span style={S.label}>Percentil vs últimos 30 días</span>
            <span style={{ ...S.mono, fontSize: 11, color: regimeColor, fontWeight: 700 }}>{regime} — P{percentile.toFixed(0)}</span>
          </div>
          <div style={{ height: 10, background: '#0a1020', borderRadius: 5, position: 'relative', overflow: 'hidden' }}>
            <div style={{ position: 'absolute', inset: 0, background: 'linear-gradient(90deg, #22c55e20, #f59e0b20, #ef444420)', borderRadius: 5 }} />
            <div style={{
              position: 'absolute', top: -1, width: 4, height: 12, background: regimeColor, borderRadius: 2,
              left: `${clamp(percentile, 2, 98)}%`, transform: 'translateX(-50%)',
              boxShadow: `0 0 6px ${regimeColor}`,
            }} />
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, color: '#3a4a6a', marginTop: 3, ...S.mono }}>
            <span>Baja</span><span>Media</span><span>Alta</span>
          </div>
        </div>
      )}

      {compression && (
        <div style={{ marginBottom: 10 }}>
          <Signal level={compressionLevel} text={compression} />
        </div>
      )}

      {history && history.length > 2 && (
        <div>
          <div style={S.label}>RV 24h histórica — últimos 30d</div>
          <div style={{ marginTop: 4 }}>
            <Spark data={history} color="#a78bfa" height={44} />
          </div>
        </div>
      )}
    </div>
  )
}

// ── Volume Profile Summary ────────────────────────────────────────────
function VolumeProfileSummary({ vp, vpByPeriod, period, setPeriod }) {
  const periods = ['4h', '12h', '24h', '7d', '8d', '30d', '45d']
  const periodLabels = { '4h': '4 horas', '12h': '12 horas', '24h': '24 horas', '7d': '7 días', '8d': '8 días (200h)', '30d': '30 días', '45d': '45 días' }

  // Select the right VP data based on period
  const activeVp = period === '8d' ? vp : (vpByPeriod || {})[period]

  if (!activeVp || !activeVp.poc) {
    return <div style={{ color: '#4a5980', fontSize: 12 }}>Cargando volume profile...</div>
  }

  const posLabels = {
    at_poc:   { text: 'Precio EN el POC — Zona de congestión, difícil salir sin catalizador', level: 'caution' },
    in_va:    { text: 'Precio DENTRO del Value Area — Zona de equilibrio', level: 'neutral' },
    above_va: { text: 'Precio ARRIBA del Value Area — Sobreextensión alcista, posible retorno a VA', level: 'bullish' },
    below_va: { text: 'Precio DEBAJO del Value Area — Sobreextensión bajista, posible retorno a VA', level: 'bearish' },
  }
  const pos = posLabels[activeVp.pricePosition] || { text: '—', level: 'neutral' }

  return (
    <div>
      {/* Period selector */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 12, flexWrap: 'wrap' }}>
        {periods.map(p => (
          <button key={p} onClick={() => setPeriod(p)} style={{
            padding: '3px 10px', borderRadius: 5, border: `1px solid ${period === p ? '#fbbf24' : '#1a2544'}`,
            background: period === p ? '#1a1800' : 'transparent', color: period === p ? '#fbbf24' : '#4a5980',
            cursor: 'pointer', fontSize: 10, fontFamily: "'IBM Plex Mono', monospace", fontWeight: period === p ? 700 : 400,
          }}>{p}</button>
        ))}
        <span style={{ ...S.mono, fontSize: 9, color: '#3a4a6a', alignSelf: 'center', marginLeft: 4 }}>
          {periodLabels[period]}
        </span>
      </div>

      <div style={{ display: 'flex', gap: 16, marginBottom: 12, flexWrap: 'wrap' }}>
        {[
          ['POC (máx vol)', activeVp.poc?.price, '#fbbf24'],
          ['Value Area High', activeVp.vah, '#22c55e'],
          ['Value Area Low', activeVp.val, '#ef4444'],
          ['Precio actual', activeVp.currentPrice, '#e2e8f0'],
        ].map(([l, v, c]) => (
          <div key={l}>
            <div style={S.label}>{l}</div>
            <div style={{ ...S.mono, fontSize: 15, fontWeight: 700, color: c }}>{v != null ? `$${Number(v).toFixed(0)}` : '—'}</div>
          </div>
        ))}
      </div>

      <div style={{ marginBottom: 12 }}>
        <Signal level={pos.level} text={pos.text} />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        <div>
          <div style={{ ...S.label, marginBottom: 5 }}>HVN — Alto volumen (S/R fuertes)</div>
          {(activeVp.hvn || []).slice(0, 5).map(h => (
            <div key={h.price} style={{ display: 'flex', justifyContent: 'space-between', padding: '2px 0' }}>
              <span style={{ ...S.mono, fontSize: 11, color: '#fbbf24' }}>${Number(h.price).toFixed(0)}</span>
              <span style={{ ...S.mono, fontSize: 10, color: '#4a5a7a' }}>{(h.volume / 1000).toFixed(0)}K</span>
            </div>
          ))}
        </div>
        <div>
          <div style={{ ...S.label, marginBottom: 5 }}>LVN — Bajo volumen (paso rápido)</div>
          {(activeVp.lvn || []).length > 0 ? (activeVp.lvn || []).slice(0, 5).map(l => (
            <div key={l.price} style={{ display: 'flex', justifyContent: 'space-between', padding: '2px 0' }}>
              <span style={{ ...S.mono, fontSize: 11, color: '#38bdf8' }}>${Number(l.price).toFixed(0)}</span>
              <span style={{ ...S.mono, fontSize: 10, color: '#4a5a7a' }}>{(l.volume / 1000).toFixed(0)}K</span>
            </div>
          )) : <div style={{ fontSize: 11, color: '#3a4a6a' }}>Sin gaps significativos</div>}
        </div>
      </div>
    </div>
  )
}

// ── Volume Profile ───────────────────────────────────────────────────
function VolumeProfile({ data, dataByPeriod, currentPrice, period, setPeriod, pocInfo, pocByPeriod }) {
  const periods = ['4h', '12h', '24h', '7d', '8d', '30d', '45d']
  const periodLabels = { '4h': '4 horas', '12h': '12 horas', '24h': '24 horas', '7d': '7 días', '8d': '8 días (200h)', '30d': '30 días', '45d': '45 días' }

  const activeData = period === '8d' ? data : (dataByPeriod || {})[period]
  // Canonical POC/VAH/VAL from structured profile (same source as "Niveles Clave" panel)
  const canonicalPoc = period === '8d' ? pocInfo : (pocByPeriod || {})[period]
  const pocPrice = canonicalPoc?.poc?.price
  const vahPrice = canonicalPoc?.vah
  const valPrice = canonicalPoc?.val

  if (!activeData || activeData.length === 0) {
    return <div style={{ color: '#4a5980', fontSize: 11, padding: '20px 0', textAlign: 'center' }}>Calculando volume profile...</div>
  }

  const maxVol = Math.max(...activeData.map(d => d.vol), 1)
  // Top 5 levels ranked by volume within this ±10% dataset (for S/R highlighting)
  const sortedByVol = [...activeData].sort((a, b) => b.vol - a.vol)
  const topLevels = new Set(sortedByVol.slice(0, 5).map(d => d.price))
  // Find the chart-bucket closest to the canonical POC so we can pin it visually
  let pocBucket = null
  if (pocPrice != null && activeData.length) {
    pocBucket = activeData.reduce((best, d) =>
      Math.abs(d.price - pocPrice) < Math.abs(best.price - pocPrice) ? d : best
    , activeData[0]).price
  }
  const sorted = [...activeData].sort((a, b) => b.price - a.price)

  return (
    <div>
      {/* Period selector */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 10, flexWrap: 'wrap' }}>
        {periods.map(p => (
          <button key={p} onClick={() => setPeriod(p)} style={{
            padding: '3px 10px', borderRadius: 5, border: `1px solid ${period === p ? '#fbbf24' : '#1a2544'}`,
            background: period === p ? '#1a1800' : 'transparent', color: period === p ? '#fbbf24' : '#4a5980',
            cursor: 'pointer', fontSize: 10, fontFamily: "'IBM Plex Mono', monospace", fontWeight: period === p ? 700 : 400,
          }}>{p}</button>
        ))}
        <span style={{ ...S.mono, fontSize: 9, color: '#3a4a6a', alignSelf: 'center', marginLeft: 4 }}>
          {periodLabels[period]}
        </span>
      </div>

      <div style={{ ...S.label, marginBottom: 8 }}>
        Volume Profile ±10% — {periodLabels[period]}
        {pocPrice != null && <span style={{ color: '#fbbf24', marginLeft: 8 }}>· POC: ${Number(pocPrice).toFixed(0)}</span>}
        {vahPrice != null && valPrice != null && (
          <span style={{ color: '#5a6a8a', marginLeft: 8 }}>
            · VA: <span style={{ color: '#ef4444' }}>${Number(valPrice).toFixed(0)}</span> – <span style={{ color: '#22c55e' }}>${Number(vahPrice).toFixed(0)}</span>
          </span>
        )}
      </div>
      <div style={{ maxHeight: 480, overflowY: 'auto', paddingRight: 4 }}>
        {sorted.map(level => {
          const isPoc = pocBucket != null && level.price === pocBucket
          const isTop = topLevels.has(level.price)
          const isCurrent = currentPrice && Math.abs(level.price - currentPrice) < 3
          const intensity = level.vol / maxVol
          // Priority: POC > Current > Top
          const barColor = isPoc
            ? `rgba(251, 191, 36, ${0.5 + intensity * 0.5})`
            : isCurrent
              ? '#38bdf8'
              : isTop
                ? `rgba(251, 191, 36, ${0.25 + intensity * 0.4})`
                : `rgba(100, 116, 139, ${0.15 + intensity * 0.5})`

          const borderLeftColor = isPoc ? '#fbbf24' : isCurrent ? '#38bdf8' : isTop ? 'rgba(251, 191, 36, 0.5)' : 'transparent'
          const borderLeftWidth = isPoc ? 4 : 3

          return (
            <div key={level.price} style={{
              display: 'flex', alignItems: 'center', gap: 6, padding: '1px 0',
              borderLeft: `${borderLeftWidth}px solid ${borderLeftColor}`,
              paddingLeft: (isCurrent || isTop || isPoc) ? 4 : 0,
              background: isPoc ? 'rgba(251, 191, 36, 0.05)' : 'transparent',
            }}>
              <span style={{
                ...S.mono, fontSize: 10, width: 62, textAlign: 'right', flexShrink: 0,
                color: isPoc ? '#fbbf24' : isCurrent ? '#38bdf8' : isTop ? '#d4a017' : '#5a6a7a',
                fontWeight: (isCurrent || isTop || isPoc) ? 700 : 400,
              }}>
                ${level.price.toFixed(0)}{isPoc ? ' ◄' : ''}
              </span>
              <div style={{ flex: 1, height: 10, background: '#0a1020', borderRadius: 2, overflow: 'hidden', position: 'relative' }}>
                <div style={{
                  height: '100%', borderRadius: 2,
                  width: `${Math.max(intensity * 100, 1)}%`,
                  background: barColor,
                }} />
              </div>
              <span style={{
                ...S.mono, fontSize: 9, width: 48, textAlign: 'right', flexShrink: 0,
                color: isPoc ? '#fbbf24' : isTop ? '#d4a017' : '#3a4a6a',
                fontWeight: isPoc ? 700 : 400,
              }}>
                {level.vol >= 1000000 ? `${(level.vol / 1000000).toFixed(1)}M` : level.vol >= 1000 ? `${(level.vol / 1000).toFixed(0)}K` : level.vol.toFixed(0)}
              </span>
            </div>
          )
        })}
      </div>
      <div style={{ marginTop: 8, display: 'flex', gap: 16, fontSize: 10, color: '#3a4a6a', flexWrap: 'wrap' }}>
        <span style={{ color: '#fbbf24', fontWeight: 700 }}>◄ POC (máx volumen absoluto)</span>
        <span style={{ color: '#d4a017' }}>▌ Top 5 niveles (S/R clave)</span>
        <span style={{ color: '#38bdf8' }}>▌ Precio actual</span>
      </div>
    </div>
  )
}

// ── CEX Netflows Panel (spot exchange pressure via Dune) ────────────
function CexNetflowsPanel({ cexNetflows }) {
  const cn = cexNetflows || {}
  const aggs = cn.aggregates || {}
  const byEx = cn.byExchange24h || []
  const hourly = cn.hourly || []
  const bias = cn.bias || 'NEUTRAL'
  const lastUpdate = cn.lastUpdate

  const hasData = byEx.length > 0 && Object.keys(aggs).length > 0

  // Bias styling
  const biasMeta = {
    BULLISH:      { label: 'BULLISH',       color: '#22c55e', bg: 'rgba(34,197,94,0.12)',  desc: 'Salida fuerte de ETH de exchanges → presión de retiro / HODL' },
    BULLISH_MILD: { label: 'BULLISH MILD',  color: '#86efac', bg: 'rgba(34,197,94,0.08)',  desc: 'Net withdrawal moderado · ligero sesgo comprador' },
    NEUTRAL:      { label: 'NEUTRAL',       color: '#8a9ac0', bg: 'rgba(138,154,192,0.1)', desc: 'Flujos balanceados · sin presión clara' },
    BEARISH_MILD: { label: 'BEARISH MILD',  color: '#fca5a5', bg: 'rgba(239,68,68,0.08)',  desc: 'Net deposit moderado · ligero sesgo vendedor' },
    BEARISH:      { label: 'BEARISH',       color: '#ef4444', bg: 'rgba(239,68,68,0.12)',  desc: 'Entrada fuerte de ETH a exchanges → presión vendedora' },
  }
  const bm = biasMeta[bias] || biasMeta.NEUTRAL

  // Color helper: negative net = bullish (green), positive = bearish (red)
  const netColor = (v) => {
    if (v == null) return '#8a9ac0'
    if (v < 0) return '#22c55e'
    if (v > 0) return '#ef4444'
    return '#8a9ac0'
  }

  const fmtEth = (v) => {
    if (v == null) return '—'
    const abs = Math.abs(v)
    const sign = v >= 0 ? '+' : '−'
    if (abs >= 1e6) return `${sign}${(abs / 1e6).toFixed(2)}M ETH`
    if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(2)}k ETH`
    return `${sign}${abs.toFixed(0)} ETH`
  }
  const fmtUsd = (v) => {
    if (v == null) return '—'
    const abs = Math.abs(v)
    const sign = v >= 0 ? '+' : '−'
    if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2)}B`
    if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(1)}M`
    if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(0)}k`
    return `${sign}$${abs.toFixed(0)}`
  }

  if (!hasData) {
    return (
      <div>
        <div style={{ ...S.sectionTitle, marginBottom: 10 }}>EXCHANGE NETFLOWS · CEX Spot Pressure (vía Dune)</div>
        <div style={{ padding: '14px 12px', background: '#0a1020', borderRadius: 6, fontSize: 11, color: '#5a6a8a', textAlign: 'center' }}>
          Sin datos de Dune (revisar DUNE_API_KEY en backend/.env)
        </div>
      </div>
    )
  }

  const net24h = aggs['24h']?.netInflowEth
  const usd24h = aggs['24h']?.netInflowUsd
  const windows = [
    { k: '1h',  label: 'Última hora' },
    { k: '6h',  label: 'Últimas 6h' },
    { k: '24h', label: 'Últimas 24h' },
    { k: '7d',  label: 'Últimos 7d' },
  ]

  // Max for top exchange row width scaling
  const maxAbsNet = Math.max(...byEx.map(e => Math.abs(e.netInflowEth || 0)), 1)
  const ageMin = lastUpdate ? Math.floor((Date.now() - lastUpdate) / 60000) : null

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <div style={S.sectionTitle}>EXCHANGE NETFLOWS · CEX Spot Pressure (vía Dune)</div>
        <div style={{ fontSize: 9, color: '#4a5980', ...S.mono }}>
          {cn.exchangeCount || 0} exchanges · update {ageMin != null ? `${ageMin}m` : '—'}
        </div>
      </div>

      {/* Verdict + 24h headline */}
      <div style={{
        padding: '12px 14px',
        marginBottom: 12,
        borderRadius: 8,
        background: bm.bg,
        border: `1px solid ${bm.color}33`,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        flexWrap: 'wrap',
        gap: 12,
      }}>
        <div>
          <div style={{ fontSize: 10, color: '#5a6a8a', marginBottom: 4, letterSpacing: 1 }}>SESGO 24H</div>
          <div style={{ fontSize: 16, fontWeight: 700, color: bm.color, ...S.mono }}>{bm.label}</div>
          <div style={{ fontSize: 10, color: '#8a9ac0', marginTop: 2 }}>{bm.desc}</div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontSize: 10, color: '#5a6a8a', marginBottom: 4, letterSpacing: 1 }}>NET INFLOW 24H</div>
          <div style={{ fontSize: 22, fontWeight: 700, color: netColor(net24h), ...S.mono, lineHeight: 1.1 }}>
            {fmtEth(net24h)}
          </div>
          <div style={{ fontSize: 11, color: netColor(usd24h), ...S.mono, marginTop: 2 }}>
            {fmtUsd(usd24h)}
          </div>
        </div>
      </div>

      {/* Window grid */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
        gap: 6,
        marginBottom: 12,
      }}>
        {windows.map(w => {
          const a = aggs[w.k]
          if (!a) return null
          const netE = a.netInflowEth
          return (
            <div key={w.k} style={{
              padding: '8px 10px',
              borderRadius: 6,
              border: `1px solid ${netColor(netE)}44`,
              background: `${netColor(netE)}08`,
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                <div style={{ fontSize: 10, fontWeight: 700, color: '#e2e8f0', ...S.mono }}>{w.k.toUpperCase()}</div>
                <div style={{ fontSize: 8, color: '#4a5980' }}>{w.label}</div>
              </div>
              <div style={{ fontSize: 13, fontWeight: 700, color: netColor(netE), ...S.mono }}>{fmtEth(netE)}</div>
              <div style={{ fontSize: 9, color: netColor(a.netInflowUsd), ...S.mono, marginTop: 1 }}>{fmtUsd(a.netInflowUsd)}</div>
              <div style={{ borderTop: '1px solid #1a2544', marginTop: 5, paddingTop: 4, display: 'flex', justifyContent: 'space-between', fontSize: 9 }}>
                <span style={{ color: '#5a6a8a' }}>in <span style={{ color: '#fca5a5', ...S.mono }}>{(a.inflowEth / 1e3).toFixed(1)}k</span></span>
                <span style={{ color: '#5a6a8a' }}>out <span style={{ color: '#86efac', ...S.mono }}>{(a.outflowEth / 1e3).toFixed(1)}k</span></span>
              </div>
            </div>
          )
        })}
      </div>

      {/* 7-day hourly sparkline */}
      {hourly.length >= 2 && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
            <div style={{ fontSize: 9, color: '#5a6a8a', letterSpacing: 1 }}>NET INFLOW HORARIO · ÚLTIMOS 7D</div>
            <div style={{ fontSize: 9, color: '#5a6a8a', ...S.mono }}>{hourly.length}h</div>
          </div>
          <Spark
            data={hourly}
            valueKey="netInflowEth"
            height={60}
            width={800}
            showZero
            color={netColor(net24h)}
          />
        </div>
      )}

      {/* Per-exchange ranking */}
      <div>
        <div style={{ fontSize: 9, color: '#5a6a8a', letterSpacing: 1, marginBottom: 6 }}>RANKING POR EXCHANGE · 24H</div>
        <div style={{ display: 'grid', gap: 4 }}>
          {byEx.map(e => {
            const widthPct = Math.min((Math.abs(e.netInflowEth || 0) / maxAbsNet) * 100, 100)
            const c = netColor(e.netInflowEth)
            return (
              <div key={e.cex} style={{
                display: 'grid',
                gridTemplateColumns: '90px 1fr 110px 90px',
                alignItems: 'center',
                gap: 8,
                padding: '4px 6px',
                borderRadius: 4,
                background: '#0a1020',
              }}>
                <div style={{ fontSize: 11, color: '#c8d6e5', fontWeight: 600, ...S.mono }}>{e.cex}</div>
                <div style={{ height: 6, background: '#111a35', borderRadius: 3, overflow: 'hidden', position: 'relative' }}>
                  <div style={{
                    position: 'absolute', top: 0, left: 0,
                    width: `${widthPct}%`, height: '100%',
                    background: c, opacity: 0.6, borderRadius: 3, transition: 'width 0.6s',
                  }} />
                </div>
                <div style={{ fontSize: 11, fontWeight: 700, color: c, ...S.mono, textAlign: 'right' }}>{fmtEth(e.netInflowEth)}</div>
                <div style={{ fontSize: 10, color: c, ...S.mono, textAlign: 'right' }}>{fmtUsd(e.netInflowUsd)}</div>
              </div>
            )
          })}
        </div>
      </div>

      <div style={{ marginTop: 10, padding: '6px 8px', background: '#0a1020', borderRadius: 5, fontSize: 9, color: '#5a6a8a', lineHeight: 1.5 }}>
        <b style={{ color: '#8a9ac0' }}>Convención</b>: net inflow &gt; 0 → ETH entrando a CEX → presión vendedora (BEARISH).
        Net inflow &lt; 0 → ETH saliendo a self-custody → HODL / acumulación (BULLISH).
        Datos via Dune cex.flows · 9 CEX clasificados · refresh cada 30 min.
      </div>
    </div>
  )
}

// ── Money Quality Panel (plata nueva vs short covering) ─────────────
function MoneyQualityPanel({ moneyQuality }) {
  const mq = moneyQuality || {}
  const byWindow = mq.byWindow || {}
  // 4 intraday windows + 3 multi-day windows (horizons for slow stochs on higher TFs)
  const windows = ['1h', '4h', '12h', '24h', '3d', '7d', '14d']
  const verdict = mq.verdict || '—'
  const score = mq.score
  const fundingCtx = mq.fundingContext

  const verdictColor = (v) => {
    if (!v) return '#6a7aa0'
    if (v.startsWith('ALCISTA')) return '#22c55e'
    if (v.startsWith('Alcista')) return '#86efac'
    if (v.startsWith('BAJISTA')) return '#ef4444'
    if (v.startsWith('Bajista')) return '#fca5a5'
    return '#8a9ac0'
  }
  const dirColor = (d) => d === 'bullish' ? '#22c55e' : d === 'bearish' ? '#ef4444' : '#8a9ac0'
  const qualityBadge = (q) => {
    if (q === 'high')   return { text: 'ALTA',  color: '#22c55e', bg: 'rgba(34,197,94,0.12)' }
    if (q === 'medium') return { text: 'MEDIA', color: '#f59e0b', bg: 'rgba(245,158,11,0.12)' }
    return { text: 'BAJA', color: '#ef4444', bg: 'rgba(239,68,68,0.12)' }
  }

  const fmtPct = (v) => v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
  const fmtRatio = (v) => v == null ? '—' : v.toFixed(2)
  const fmtDelta = (v) => {
    if (v == null) return '—'
    const abs = Math.abs(v)
    if (abs >= 1e6) return `${v >= 0 ? '+' : ''}${(v / 1e6).toFixed(2)}M`
    if (abs >= 1e3) return `${v >= 0 ? '+' : ''}${(v / 1e3).toFixed(1)}k`
    return `${v >= 0 ? '+' : ''}${v.toFixed(0)}`
  }

  return (
    <div>
      <div style={{ ...S.sectionTitle, marginBottom: 10 }}>CALIDAD DEL MOVIMIENTO · Plata Nueva vs Covering</div>

      <div style={{
        padding: '10px 12px',
        marginBottom: 12,
        borderRadius: 8,
        background: 'rgba(15, 23, 42, 0.6)',
        border: `1px solid ${verdictColor(verdict)}33`,
      }}>
        <div style={{ fontSize: 10, color: '#5a6a8a', marginBottom: 4, letterSpacing: 1 }}>VEREDICTO</div>
        <div style={{ fontSize: 14, fontWeight: 700, color: verdictColor(verdict), ...S.mono, marginBottom: 4 }}>
          {verdict} {score != null && <span style={{ fontSize: 11, color: '#6a7aa0', fontWeight: 400 }}>· score {score >= 0 ? '+' : ''}{score}</span>}
        </div>
        {fundingCtx && (
          <div style={{ fontSize: 10, color: '#8a9ac0', marginTop: 3 }}>{fundingCtx}</div>
        )}
      </div>

      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))',
        gap: 6,
      }}>
        {windows.map(w => {
          const info = byWindow[w]
          if (!info) {
            return (
              <div key={w} style={{
                padding: '8px 6px', borderRadius: 6, border: '1px solid #1a2544',
                background: '#0a1020', textAlign: 'center', opacity: 0.4,
              }}>
                <div style={{ fontSize: 10, fontWeight: 700, color: '#4a5980' }}>{w}</div>
                <div style={{ fontSize: 9, color: '#4a5980', marginTop: 4 }}>sin datos</div>
              </div>
            )
          }
          const qb = qualityBadge(info.quality)
          return (
            <div key={w} style={{
              padding: '8px 8px',
              borderRadius: 6,
              border: `1px solid ${dirColor(info.direction)}44`,
              background: `${dirColor(info.direction)}08`,
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                <div style={{ fontSize: 11, fontWeight: 700, color: '#e2e8f0', ...S.mono }}>{w}</div>
                <div style={{
                  fontSize: 8, fontWeight: 700, padding: '1px 5px', borderRadius: 3,
                  color: qb.color, background: qb.bg, letterSpacing: 0.5,
                }}>{qb.text}</div>
              </div>

              <div style={{ fontSize: 10, color: dirColor(info.direction), fontWeight: 600, marginBottom: 6, lineHeight: 1.2 }}>
                {info.label}
              </div>

              <div style={{ borderTop: '1px solid #1a2544', paddingTop: 5, display: 'grid', gap: 2 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9 }}>
                  <span style={{ color: '#5a6a8a' }}>ΔPx</span>
                  <span style={{ ...S.mono, color: info.priceChgPct >= 0 ? '#86efac' : '#fca5a5' }}>{fmtPct(info.priceChgPct)}</span>
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9 }}>
                  <span style={{ color: '#5a6a8a' }}>ΔOI</span>
                  <span style={{ ...S.mono, color: info.oiDeltaPct >= 0 ? '#86efac' : '#fca5a5' }}>{fmtPct(info.oiDeltaPct)}</span>
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9 }}>
                  <span style={{ color: '#5a6a8a' }}>OI Δ</span>
                  <span style={{ ...S.mono, color: '#a8b5d1' }}>{fmtDelta(info.oiDelta)}</span>
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9 }}>
                  <span style={{ color: '#5a6a8a' }}>Ratio</span>
                  <span style={{ ...S.mono, color: '#e2e8f0' }}>{fmtRatio(info.ratio)}</span>
                </div>
                {info.deltaVsVol != null && (
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9 }}>
                    <span style={{ color: '#5a6a8a' }}>Δ/Vol</span>
                    <span style={{ ...S.mono, color: Math.abs(info.deltaVsVol) > 15 ? '#f59e0b' : '#a8b5d1' }}>
                      {info.deltaVsVol >= 0 ? '+' : ''}{info.deltaVsVol.toFixed(1)}%
                    </span>
                  </div>
                )}
              </div>
            </div>
          )
        })}
      </div>

      <div style={{ marginTop: 10, padding: '6px 8px', background: '#0a1020', borderRadius: 5, fontSize: 9, color: '#5a6a8a', lineHeight: 1.5 }}>
        <b style={{ color: '#8a9ac0' }}>Ratio</b> = |ΔPrecio%| / |ΔOI%|. &lt;1 acumulación real · 1-2 balanceado · 2-5 covering dominante · &gt;5 squeeze puro.
        <br /><b style={{ color: '#8a9ac0' }}>Δ/Vol</b> = delta taker / volumen total. &gt;15% = agresión extrema.
      </div>
    </div>
  )
}

// ── Setup Detector (stoch alignment + threshold breach + MQ filter) ────
// Map each stoch TF to TWO MQ windows:
//   fast = horizon del Fast stoch (100 × TF_duration)
//   slow = horizon del Slow stoch (400 × TF_duration)
// The fast window tells us about recent capital pressure on the entry; the
// slow window tells us if the range (lookback) was built on stable capital.
// Ambos filtros se combinan en analyzeSetup para producir la calidad final.
const STOCH_TO_MQ_WINDOWS = {
  // TF → { fast: window-for-fast-horizon, slow: window-for-slow-horizon }
  // 1m:  fast≈100min≈1.7h, slow≈400min≈6.7h   → fast=1h, slow=12h
  '1m':  { fast: '1h',  slow: '12h' },
  // 5m:  fast≈8h,         slow≈33h             → fast=12h, slow=24h
  '5m':  { fast: '12h', slow: '24h' },
  // 15m: fast≈25h,        slow≈100h≈4d         → fast=24h, slow='3d'
  '15m': { fast: '24h', slow: '3d'  },
  // 1h:  fast≈100h≈4d,    slow≈400h≈17d        → fast=3d,  slow=14d
  '1h':  { fast: '3d',  slow: '14d' },
  // 4h:  fast≈400h≈17d,   slow≈1600h≈67d       → fast=14d (clamp), slow=14d
  '4h':  { fast: '14d', slow: '14d' },
}
// Legacy single-window map kept for any stale references / fallbacks
const STOCH_TO_MQ_WINDOW = {
  '1m': '1h', '5m': '12h', '15m': '24h', '1h': '3d', '4h': '14d'
}

/**
 * Grade a single MQ window against a setup direction.
 *  - direction: 'short' | 'long'
 *  - mqInfo:    the MQ window info ({ ratio, direction, label, quality })
 * Returns:
 *  { verdict, note, tier }
 *    verdict: 'BLOCK' | 'NEUTRAL' | 'UPGRADE'
 *    tier:    -1 (block) | 0 (neutral) | +1 (covering) | +2 (squeeze)
 */
function gradeMqForDirection(direction, mqInfo) {
  if (!mqInfo) return { verdict: 'NEUTRAL', note: null, tier: 0 }
  const r = mqInfo.ratio
  const mqDir = mqInfo.direction
  if (r == null) return { verdict: 'NEUTRAL', note: 'Sin ratio', tier: 0 }

  // BLOCK: ratio <1 en misma dirección del movimiento = tendencia real confirmada
  if (direction === 'short' && r < 1 && mqDir === 'bullish') {
    return {
      verdict: 'BLOCK',
      note: `Acumulación real (r ${r.toFixed(2)}) — alcista real`,
      tier: -1,
    }
  }
  if (direction === 'long' && r < 1 && mqDir === 'bearish') {
    return {
      verdict: 'BLOCK',
      note: `Distribución real (r ${r.toFixed(2)}) — bajista real`,
      tier: -1,
    }
  }
  // UPGRADE: ratio ≥2 contrario = covering/liquidation = rally/drop débil
  if (direction === 'short' && r >= 2 && mqDir === 'bullish') {
    const tier = r >= 5 ? 2 : 1
    return {
      verdict: 'UPGRADE',
      note: r >= 5
        ? `Squeeze puro (r ${r.toFixed(2)})`
        : `Covering dominante (r ${r.toFixed(2)})`,
      tier,
    }
  }
  if (direction === 'long' && r >= 2 && mqDir === 'bearish') {
    const tier = r >= 5 ? 2 : 1
    return {
      verdict: 'UPGRADE',
      note: r >= 5
        ? `Long squeeze puro (r ${r.toFixed(2)})`
        : `Liquidation dominante (r ${r.toFixed(2)})`,
      tier,
    }
  }
  // Balanceado
  if (r >= 1 && r < 2) {
    return {
      verdict: 'NEUTRAL',
      note: `Balanceado (r ${r.toFixed(2)})`,
      tier: 0,
    }
  }
  return { verdict: 'NEUTRAL', note: `r ${r.toFixed(2)}`, tier: 0 }
}

/**
 * Map cut-anchored MQ quality to setup grade and tier.
 *   block        → BLOCKED
 *   neutral      → A
 *   upgrade-mid  → A+
 *   upgrade-high → A++
 */
function cutQualityToGrade(cutQuality) {
  switch (cutQuality) {
    case 'block':        return { grade: 'BLOCKED', tier: -1 }
    case 'upgrade-high': return { grade: 'A++',     tier: 2 }
    case 'upgrade-mid':  return { grade: 'A+',      tier: 1 }
    case 'neutral':      return { grade: 'A',       tier: 0 }
    default:             return { grade: null,      tier: 0 }
  }
}

/**
 * Analyze stochastic setup per TF according to user's strategy.
 *
 * PRIMARY FILTER: cut-anchored MQ.
 *   Measures the OI/Price evolution from the moment Fast %K entered the OB/OS
 *   zone up to now. This is the IMPULSE filter — it answers "is the move that
 *   put the stoch into the extreme being powered by NEW MONEY (trend → BLOCK)
 *   or by COVERING (squeeze → UPGRADE)?". This is what determines the quality
 *   grade (BLOCKED / A / A+ / A++).
 *
 * SECONDARY (informative): dual regime MQ (fast horizon + slow horizon).
 *   Tells us about the multi-day capital base under the rolling stoch range.
 *   Shown as context but does NOT gate the trade.
 *
 * Stoch alignment rules:
 *  - Slow %K + %D both in same extreme zone (OB ≥80 or OS ≤20)
 *  - Fast %K + %D both in same zone (persistence ≥2 bars)
 *  - Trigger: Fast %K crosses the threshold (exits the zone)
 */
function analyzeSetup(stochData, mqFastInfo, mqSlowInfo, cutInfo) {
  if (!stochData || !stochData.slow || !stochData.fast) return null
  const slow = stochData.slow
  const fast = stochData.fast
  if (slow.k == null || slow.d == null || fast.k == null || fast.d == null) return null

  const slowOB = slow.k >= 80 && slow.d >= 80
  const slowOS = slow.k <= 20 && slow.d <= 20
  const fastOB = fast.k >= 80 && fast.d >= 80
  const fastOS = fast.k <= 20 && fast.d <= 20
  const slowCerca80 = slow.k >= 75                // strict confirmation band
  const slowCerca20 = slow.k <= 25

  const fkh = (fast.kHistory || []).filter(v => v != null)
  const kPrev = fkh.length >= 2 ? fkh[fkh.length - 2] : null
  const kPrev2 = fkh.length >= 3 ? fkh[fkh.length - 3] : null
  const kNow = fast.k

  // Persistence: fast %K was in zone for ≥2 consecutive previous bars
  const persistOB = kPrev != null && kPrev2 != null && kPrev >= 80 && kPrev2 >= 80
  const persistOS = kPrev != null && kPrev2 != null && kPrev <= 20 && kPrev2 <= 20

  // Cross detection on %K (threshold breach) — trigger condition
  const justCrossedDown80 = kPrev != null && kPrev >= 80 && kNow < 80
  const justCrossedDown75 = kPrev != null && kPrev >= 75 && kNow < 75
  const justCrossedUp20   = kPrev != null && kPrev <= 20 && kNow > 20
  const justCrossedUp25   = kPrev != null && kPrev <= 25 && kNow > 25

  // Determine setup state and direction
  let state = 'INACTIVE'
  let direction = null
  let trigger = null  // 'standard' (80/20) | 'strict' (75/25) | null

  // SHORT setup: slow OB + fast OB (armed) + possible trigger
  if (slowOB && fastOB) {
    direction = 'short'
    state = 'ARMED'
    if (justCrossedDown80) { state = 'TRIGGERED'; trigger = 'standard' }
    else if (justCrossedDown75) { state = 'TRIGGERED'; trigger = 'strict' }
  }
  else if (slowOB && persistOB && kPrev >= 80 && kNow < 80) {
    direction = 'short'
    state = 'TRIGGERED'
    trigger = kNow < 75 ? 'strict' : 'standard'
  }
  else if ((slowOB || slowCerca80) && kPrev != null && kPrev < 80 && kNow < 70 && persistOB) {
    direction = 'short'
    state = 'LATE'
  }
  else if (slowOS && fastOS) {
    direction = 'long'
    state = 'ARMED'
    if (justCrossedUp20) { state = 'TRIGGERED'; trigger = 'standard' }
    else if (justCrossedUp25) { state = 'TRIGGERED'; trigger = 'strict' }
  }
  else if (slowOS && persistOS && kPrev <= 20 && kNow > 20) {
    direction = 'long'
    state = 'TRIGGERED'
    trigger = kNow > 25 ? 'strict' : 'standard'
  }
  else if ((slowOS || slowCerca20) && kPrev != null && kPrev > 20 && kNow > 30 && persistOS) {
    direction = 'long'
    state = 'LATE'
  }

  // ── Quality grading ──────────────────────────────────────────────
  // PRIMARY: cut-anchored MQ (the impulse from the moment fast %K entered zone)
  // SECONDARY (informative only): dual regime MQ (fast horizon + slow horizon)
  let quality = null          // 'A' | 'A+' | 'A++' | 'BLOCKED' | null
  let blockedReason = null
  let qualityNote = null
  let mqFastGrade = null
  let mqSlowGrade = null

  if (direction && (state === 'ARMED' || state === 'TRIGGERED')) {
    // Compute regime grades for context (not gating)
    mqFastGrade = gradeMqForDirection(direction, mqFastInfo)
    mqSlowGrade = gradeMqForDirection(direction, mqSlowInfo)

    // PRIMARY: cut-anchored quality determines the grade
    if (cutInfo && cutInfo.direction === direction && cutInfo.quality) {
      const { grade } = cutQualityToGrade(cutInfo.quality)
      quality = grade
      if (grade === 'BLOCKED') {
        const ratioStr = cutInfo.ratio != null ? cutInfo.ratio.toFixed(2) : '—'
        blockedReason = `${cutInfo.label} (r ${ratioStr}, ${cutInfo.anchorBars} bars desde el corte)`
      } else {
        const ratioStr = cutInfo.ratio != null ? cutInfo.ratio.toFixed(2) : '—'
        qualityNote = `desde el corte (${cutInfo.anchorBars} bars): ${cutInfo.label} · r ${ratioStr}`
      }
    } else if (cutInfo && cutInfo.direction === direction && cutInfo.quality === null) {
      // Cut info present but OI lookup failed — fall back to default A
      quality = 'A'
      qualityNote = `desde el corte (${cutInfo.anchorBars} bars): ${cutInfo.label}`
    } else {
      // No cut data — use default A so we don't silently block valid setups
      quality = 'A'
      qualityNote = 'sin filtro de corte (sin datos)'
    }
  }

  return {
    state,
    direction,
    trigger,
    quality,
    slowK: slow.k, slowD: slow.d, fastK: fast.k, fastD: fast.d,
    fastKPrev: kPrev,
    slowOB, slowOS, fastOB, fastOS,
    persistOB, persistOS,
    // PRIMARY filter (cut-anchored)
    cut: cutInfo && cutInfo.direction === direction ? {
      anchorBars:     cutInfo.anchorBars,
      anchorIsCapped: cutInfo.anchorIsCapped,
      priceChgPct:    cutInfo.priceChgPct,
      oiDeltaPct:     cutInfo.oiDeltaPct,
      ratio:          cutInfo.ratio,
      label:          cutInfo.label,
      quality:        cutInfo.quality,
      oiSource:       cutInfo.oiSource,
    } : null,
    // SECONDARY filter (regime, informative)
    mqFast: mqFastInfo ? {
      ratio: mqFastInfo.ratio ?? null,
      label: mqFastInfo.label ?? null,
      direction: mqFastInfo.direction ?? null,
      quality: mqFastInfo.quality ?? null,
      verdict: mqFastGrade?.verdict ?? null,
      note: mqFastGrade?.note ?? null,
    } : null,
    mqSlow: mqSlowInfo ? {
      ratio: mqSlowInfo.ratio ?? null,
      label: mqSlowInfo.label ?? null,
      direction: mqSlowInfo.direction ?? null,
      quality: mqSlowInfo.quality ?? null,
      verdict: mqSlowGrade?.verdict ?? null,
      note: mqSlowGrade?.note ?? null,
    } : null,
    blockedReason,
    qualityNote,
  }
}

function SetupPanel({ stochastics, moneyQuality, cutAnchoredMq, stochTf, setStochTf }) {
  const timeframes = ['1m', '5m', '15m', '1h', '4h']
  const mqByWindow = (moneyQuality || {}).byWindow || {}
  const cutByTf = cutAnchoredMq || {}

  // Analyze all TFs: primary = cut-anchored MQ; secondary = dual regime MQ
  const analysisByTf = {}
  timeframes.forEach(tf => {
    const windows = STOCH_TO_MQ_WINDOWS[tf] || { fast: '4h', slow: '24h' }
    const mqFast = mqByWindow[windows.fast]
    const mqSlow = mqByWindow[windows.slow]
    const cutInfo = cutByTf[tf]
    analysisByTf[tf] = analyzeSetup((stochastics || {})[tf], mqFast, mqSlow, cutInfo)
  })

  const current = analysisByTf[stochTf]
  const currentWindows = STOCH_TO_MQ_WINDOWS[stochTf] || { fast: '4h', slow: '24h' }

  const stateLabel = {
    'INACTIVE':  'Sin setup',
    'ARMED':     'ARMADO — esperando trigger',
    'TRIGGERED': 'DISPARADO — entrada ahora',
    'LATE':      'TARDE — cruce ya pasó',
  }

  const dirColor = (d) => d === 'short' ? '#ef4444' : d === 'long' ? '#22c55e' : '#6a7aa0'
  const dirLabel = (d) => d === 'short' ? 'SHORT' : d === 'long' ? 'LONG' : '—'

  const stateColor = (st) => {
    if (st === 'TRIGGERED') return '#22c55e'
    if (st === 'ARMED') return '#f59e0b'
    if (st === 'LATE') return '#6a7aa0'
    return '#4a5980'
  }

  const qualityBadge = (q) => {
    if (q === 'A++')     return { text: 'A++', color: '#22c55e', bg: 'rgba(34,197,94,0.15)' }
    if (q === 'A+')      return { text: 'A+',  color: '#86efac', bg: 'rgba(34,197,94,0.10)' }
    if (q === 'A')       return { text: 'A',   color: '#fbbf24', bg: 'rgba(251,191,36,0.10)' }
    if (q === 'BLOCKED') return { text: '✗ BLOQUEADO', color: '#ef4444', bg: 'rgba(239,68,68,0.12)' }
    return { text: '—', color: '#4a5980', bg: 'transparent' }
  }

  // Action text based on state
  const getActionText = (a) => {
    if (!a || a.state === 'INACTIVE') return 'Sin acción — esperar setup'
    if (a.quality === 'BLOCKED') return `NO OPERAR — ${a.blockedReason}`
    if (a.state === 'ARMED') {
      if (a.direction === 'short') return `Esperar Fast %K cruce <80 (trigger 75 estricto) · ahora en ${a.fastK.toFixed(1)}`
      return `Esperar Fast %K cruce >20 (trigger 25 estricto) · ahora en ${a.fastK.toFixed(1)}`
    }
    if (a.state === 'TRIGGERED') {
      const trig = a.trigger === 'strict' ? 'estricto (75/25)' : 'estándar (80/20)'
      if (a.direction === 'short') return `🔴 ENTRAR SHORT AHORA — trigger ${trig} · Fast %K=${a.fastK.toFixed(1)}`
      return `🟢 ENTRAR LONG AHORA — trigger ${trig} · Fast %K=${a.fastK.toFixed(1)}`
    }
    if (a.state === 'LATE') return `Setup ${dirLabel(a.direction)} activo hace varias velas — esperar próximo ciclo`
    return '—'
  }

  const pillForTf = (tf) => {
    const a = analysisByTf[tf]
    if (!a || a.state === 'INACTIVE') {
      return { color: '#4a5980', bg: '#0a1020', text: '○' }
    }
    if (a.quality === 'BLOCKED') {
      return { color: '#ef4444', bg: 'rgba(239,68,68,0.10)', text: '✗' }
    }
    if (a.state === 'TRIGGERED') {
      return { color: dirColor(a.direction), bg: `${dirColor(a.direction)}22`, text: a.direction === 'short' ? '▼' : '▲' }
    }
    if (a.state === 'ARMED') {
      return { color: '#f59e0b', bg: 'rgba(245,158,11,0.10)', text: '●' }
    }
    if (a.state === 'LATE') {
      return { color: '#6a7aa0', bg: '#0a1020', text: '~' }
    }
    return { color: '#4a5980', bg: '#0a1020', text: '○' }
  }

  // Aligned TFs (triggered or armed in same direction as current)
  const aligned = timeframes.filter(tf => {
    const a = analysisByTf[tf]
    return a && current && a.direction === current.direction && (a.state === 'ARMED' || a.state === 'TRIGGERED') && a.quality !== 'BLOCKED'
  })

  const qb = qualityBadge(current?.quality)
  const activeStateColor = current ? stateColor(current.state) : '#4a5980'

  return (
    <div>
      <div style={{ ...S.sectionTitle, marginBottom: 10, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span>SETUP DEL MOMENTO · Stoch Alignment + MQ Filter</span>
        <span style={{
          fontSize: 10, fontWeight: 700, padding: '2px 8px', borderRadius: 4,
          color: qb.color, background: qb.bg, letterSpacing: 0.5,
        }}>{qb.text}</span>
      </div>

      {/* TF selector row */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        <span style={{ ...S.label, marginRight: 4 }}>TF</span>
        {timeframes.map(tf => {
          const p = pillForTf(tf)
          const isSelected = tf === stochTf
          return (
            <button key={tf} onClick={() => setStochTf && setStochTf(tf)} style={{
              padding: '3px 9px', borderRadius: 5,
              border: `1px solid ${isSelected ? '#38bdf8' : '#1a2544'}`,
              background: isSelected ? '#081624' : p.bg,
              color: isSelected ? '#38bdf8' : p.color,
              fontSize: 11, fontWeight: 700, cursor: 'pointer',
              display: 'flex', gap: 5, alignItems: 'center',
              ...S.mono,
            }}>
              <span>{tf}</span>
              <span style={{ fontSize: 10 }}>{p.text}</span>
            </button>
          )
        })}
        <span style={{ ...S.label, marginLeft: 8, color: '#6a7aa0', fontSize: 9 }}>
          ● ARMED · ▼ SHORT · ▲ LONG · ✗ BLOQUEADO · ~ TARDE · ○ inactivo
        </span>
      </div>

      {current ? (
        <div style={{
          padding: 14, borderRadius: 8,
          border: `1px solid ${activeStateColor}66`,
          background: `${activeStateColor}0c`,
          marginBottom: 10,
        }}>
          {/* Top: direction + state */}
          <div style={{ display: 'flex', gap: 14, marginBottom: 10, alignItems: 'center' }}>
            <div>
              <div style={S.label}>DIRECCIÓN</div>
              <div style={{
                fontSize: 16, fontWeight: 700, color: dirColor(current.direction), ...S.mono,
              }}>
                {current.direction ? (current.direction === 'short' ? '🔴 SHORT' : '🟢 LONG') : '—'}
              </div>
            </div>
            <div style={{ borderLeft: '1px solid #1a2544', paddingLeft: 14 }}>
              <div style={S.label}>ESTADO</div>
              <div style={{ fontSize: 13, fontWeight: 700, color: activeStateColor, ...S.mono }}>
                {stateLabel[current.state] || '—'}
              </div>
            </div>
          </div>

          {/* Middle: stoch details */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 10 }}>
            <div style={{ padding: 8, background: '#0a1020', borderRadius: 5 }}>
              <div style={{ ...S.label, fontSize: 9, marginBottom: 3 }}>SLOW (400,40,10)</div>
              <div style={{ ...S.mono, fontSize: 12, color: '#e2e8f0' }}>
                K <b style={{ color: current.slowOB ? '#ef4444' : current.slowOS ? '#22c55e' : '#8a9ac0' }}>{current.slowK.toFixed(1)}</b>
                {' · '}
                D <b style={{ color: current.slowOB ? '#ef4444' : current.slowOS ? '#22c55e' : '#8a9ac0' }}>{current.slowD.toFixed(1)}</b>
              </div>
              <div style={{ fontSize: 9, color: '#5a6a8a', marginTop: 2 }}>
                {current.slowOB ? '✓ OB (régimen alcista agotado)' : current.slowOS ? '✓ OS (régimen bajista agotado)' : 'Neutral — sin régimen extremo'}
              </div>
            </div>
            <div style={{ padding: 8, background: '#0a1020', borderRadius: 5 }}>
              <div style={{ ...S.label, fontSize: 9, marginBottom: 3 }}>FAST (100,10,4)</div>
              <div style={{ ...S.mono, fontSize: 12, color: '#e2e8f0' }}>
                K <b style={{ color: current.fastOB ? '#ef4444' : current.fastOS ? '#22c55e' : '#8a9ac0' }}>{current.fastK.toFixed(1)}</b>
                {' · '}
                D <b style={{ color: current.fastOB ? '#ef4444' : current.fastOS ? '#22c55e' : '#8a9ac0' }}>{current.fastD.toFixed(1)}</b>
                {current.fastKPrev != null && (
                  <span style={{ fontSize: 9, color: '#5a6a8a' }}> (prev {current.fastKPrev.toFixed(1)})</span>
                )}
              </div>
              <div style={{ fontSize: 9, color: '#5a6a8a', marginTop: 2 }}>
                {current.fastOB && current.persistOB ? '✓ OB persistente (≥2 velas)' :
                 current.fastOS && current.persistOS ? '✓ OS persistente (≥2 velas)' :
                 current.fastOB ? 'OB reciente (persistencia insuficiente)' :
                 current.fastOS ? 'OS reciente (persistencia insuficiente)' : 'Neutral'}
              </div>
            </div>
          </div>

          {/* PRIMARY: cut-anchored MQ — measures impulse FROM the moment Fast %K entered the zone */}
          {current.cut && (() => {
            const cut = current.cut
            const cutQ = cut.quality
            const cutColor = cutQ === 'block' ? '#ef4444'
                           : cutQ === 'upgrade-high' ? '#22c55e'
                           : cutQ === 'upgrade-mid'  ? '#86efac'
                           : '#fbbf24'
            const cutBg    = cutQ === 'block' ? 'rgba(239,68,68,0.10)'
                           : cutQ === 'upgrade-high' ? 'rgba(34,197,94,0.10)'
                           : cutQ === 'upgrade-mid'  ? 'rgba(34,197,94,0.06)'
                           : 'rgba(251,191,36,0.06)'
            const cutTag   = cutQ === 'block' ? '✗ BLOQUEA'
                           : cutQ === 'upgrade-high' ? '✓✓ A++'
                           : cutQ === 'upgrade-mid'  ? '✓ A+'
                           : '◦ A'
            const fmtPct   = (v) => v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%'
            const ratioStr = cut.ratio != null ? cut.ratio.toFixed(2) : '—'
            const tfMin    = { '1m':1, '5m':5, '15m':15, '1h':60, '4h':240 }[stochTf] || 1
            const ageMin   = cut.anchorBars != null ? cut.anchorBars * tfMin : null
            const ageStr   = ageMin == null ? '—'
                           : ageMin >= 1440 ? `${(ageMin/1440).toFixed(1)}d`
                           : ageMin >= 60   ? `${(ageMin/60).toFixed(1)}h`
                           : `${ageMin}m`
            return (
              <div style={{
                marginBottom: 10,
                padding: '8px 11px',
                background: cutBg,
                borderRadius: 6,
                border: `1px solid ${cutColor}55`,
                borderLeft: `4px solid ${cutColor}`,
              }}>
                <div style={{
                  display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                  fontSize: 10, color: '#8a9ac0', marginBottom: 5, letterSpacing: 0.4, fontWeight: 600,
                }}>
                  <span>★ MQ DESDE EL CORTE · PRIMARY FILTER · OI {cut.oiSource || '—'}</span>
                  <span style={{ color: cutColor, fontWeight: 700, letterSpacing: 0.6 }}>{cutTag}</span>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8, marginBottom: 4 }}>
                  <div>
                    <div style={{ fontSize: 9, color: '#5a6a8a' }}>ANCHOR</div>
                    <div style={{ fontSize: 11, color: '#e2e8f0', ...S.mono }}>
                      {cut.anchorBars} bars · {ageStr}
                      {cut.anchorIsCapped && <span style={{ color: '#fbbf24', fontSize: 9 }}> ⚠cap</span>}
                    </div>
                  </div>
                  <div>
                    <div style={{ fontSize: 9, color: '#5a6a8a' }}>ΔPRECIO / ΔOI</div>
                    <div style={{ fontSize: 11, color: '#e2e8f0', ...S.mono }}>
                      {fmtPct(cut.priceChgPct)} / {fmtPct(cut.oiDeltaPct)}
                    </div>
                  </div>
                  <div>
                    <div style={{ fontSize: 9, color: '#5a6a8a' }}>RATIO |ΔP|/|ΔOI|</div>
                    <div style={{ fontSize: 11, color: cutColor, ...S.mono, fontWeight: 700 }}>
                      {ratioStr}
                    </div>
                  </div>
                </div>
                <div style={{ fontSize: 11, color: '#e2e8f0', ...S.mono, fontWeight: 600 }}>
                  → {cut.label || '—'}
                </div>
              </div>
            )
          })()}

          {/* SECONDARY (informative): dual MQ regime — fast horizon + slow horizon */}
          {(current.mqFast || current.mqSlow) && (
            <div style={{ marginBottom: 10, opacity: 0.85 }}>
              <div style={{ fontSize: 9, color: '#5a6a8a', marginBottom: 4, letterSpacing: 0.4 }}>
                CONTEXTO · FILTRO MQ DUAL (régimen) · fast={currentWindows.fast} · slow={currentWindows.slow}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
                {[
                  { key: 'fast', label: 'FAST MQ', info: current.mqFast, window: currentWindows.fast },
                  { key: 'slow', label: 'SLOW MQ', info: current.mqSlow, window: currentWindows.slow },
                ].map(({ key, label, info, window }) => {
                  if (!info) {
                    return (
                      <div key={key} style={{
                        padding: '6px 10px', background: '#0a1020', borderRadius: 5,
                        borderLeft: '3px solid #1a2544',
                      }}>
                        <div style={{ fontSize: 9, color: '#4a5980' }}>{label} · {window}</div>
                        <div style={{ fontSize: 10, color: '#4a5980', ...S.mono }}>sin datos</div>
                      </div>
                    )
                  }
                  const verdictColor = info.verdict === 'BLOCK' ? '#ef4444'
                                    : info.verdict === 'UPGRADE' ? '#22c55e'
                                    : '#8a9ac0'
                  const bgColor = info.verdict === 'BLOCK' ? 'rgba(239,68,68,0.08)'
                                : info.verdict === 'UPGRADE' ? 'rgba(34,197,94,0.06)'
                                : '#0a1020'
                  const tag = info.verdict === 'BLOCK' ? '✗ BLOQUEA'
                            : info.verdict === 'UPGRADE' ? '✓ UPGRADE'
                            : '◦ NEUTRAL'
                  return (
                    <div key={key} style={{
                      padding: '6px 10px', background: bgColor, borderRadius: 5,
                      borderLeft: `3px solid ${verdictColor}`,
                    }}>
                      <div style={{
                        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                        fontSize: 9, color: '#5a6a8a', marginBottom: 2,
                      }}>
                        <span>{label} · {window}</span>
                        <span style={{ color: verdictColor, fontWeight: 700, letterSpacing: 0.4 }}>{tag}</span>
                      </div>
                      <div style={{ fontSize: 10, color: '#e2e8f0', ...S.mono }}>
                        {info.label || '—'}
                      </div>
                      <div style={{ fontSize: 9, color: '#6a7aa0', ...S.mono, marginTop: 1 }}>
                        ratio {info.ratio != null ? info.ratio.toFixed(2) : '—'}
                        {info.direction && info.direction !== 'neutral' ? ` · ${info.direction}` : ''}
                      </div>
                    </div>
                  )
                })}
              </div>
              {(current.qualityNote || current.blockedReason) && (
                <div style={{
                  fontSize: 9,
                  color: current.quality === 'BLOCKED' ? '#fca5a5' : '#86efac',
                  marginTop: 4, paddingLeft: 4,
                }}>
                  → {current.blockedReason || current.qualityNote}
                </div>
              )}
            </div>
          )}

          {/* Action box */}
          <div style={{
            padding: '10px 12px',
            background: activeStateColor === '#22c55e' ? 'rgba(34,197,94,0.08)' :
                        activeStateColor === '#f59e0b' ? 'rgba(245,158,11,0.06)' : '#0a1020',
            borderRadius: 5,
            border: `1px dashed ${activeStateColor}55`,
          }}>
            <div style={{ fontSize: 9, color: '#5a6a8a', marginBottom: 3 }}>ACCIÓN</div>
            <div style={{ fontSize: 12, fontWeight: 600, color: current.quality === 'BLOCKED' ? '#fca5a5' : '#e2e8f0', ...S.mono }}>
              {getActionText(current)}
            </div>
            {aligned.length >= 2 && current.quality !== 'BLOCKED' && current.state !== 'INACTIVE' && (
              <div style={{ fontSize: 10, color: '#86efac', marginTop: 4, fontWeight: 600 }}>
                ⚡ Multi-TF alineado en {aligned.length} timeframes: {aligned.join(', ')}
              </div>
            )}
          </div>
        </div>
      ) : (
        <div style={{
          padding: 14, textAlign: 'center', color: '#4a5980', fontSize: 11,
          background: '#0a1020', borderRadius: 6, marginBottom: 10,
        }}>
          Sin datos de estocásticos para {stochTf}
        </div>
      )}

      <div style={{ padding: '6px 8px', background: '#0a1020', borderRadius: 5, fontSize: 9, color: '#5a6a8a', lineHeight: 1.5 }}>
        <b style={{ color: '#8a9ac0' }}>Regla</b>: Slow (400,40,10) y Fast (100,10,4) ambos en mismo extremo (OB≥80 / OS≤20). <b>Trigger</b>: Fast %K cruza el umbral (sale de la zona).
        <br />
        <b style={{ color: '#fbbf24' }}>★ Filtro PRIMARY (cut-anchored)</b>: mide |ΔP|/|ΔOI| desde el bar exacto en que Fast %K (100,10) entró a la zona OB/OS hasta ahora. <i>OI subiendo + ratio &lt;1 en dirección</i> = acumulación/distribución real → <b>BLOQUEADO</b>. <i>OI cayendo o ratio ≥5 contrario</i> = squeeze/capitulación → <b>A++</b>. <i>Ratio 2-5 contrario</i> = covering/liquidation → <b>A+</b>. Resto → A.
        <br />
        <b style={{ color: '#5a6a8a' }}>Contexto (régimen)</b>: filtro MQ dual sobre 100×TF + 400×TF — informativo, no gatea la operación.
      </div>
    </div>
  )
}

// ── Stochastic Oscillator Panel ───────────────────────────────────────
function StochasticPanel({ stochastics, timeframe, setTimeframe }) {
  const timeframes = ['1m', '5m', '15m', '1h', '4h']
  const tfLabels = { '1m': '1min', '5m': '5min', '15m': '15min', '1h': '1 hora', '4h': '4 horas' }

  const current = (stochastics || {})[timeframe] || {}
  const slow = current.slow
  const fast = current.fast

  const zone = (k, d) => {
    if (k == null || d == null) return { label: '—', color: '#4a5980', bgColor: 'transparent' }
    if (k >= 80 && d >= 80) return { label: 'OVERBOUGHT', color: '#ef4444', bgColor: 'rgba(239, 68, 68, 0.08)' }
    if (k <= 20 && d <= 20) return { label: 'OVERSOLD', color: '#22c55e', bgColor: 'rgba(34, 197, 94, 0.08)' }
    if (k >= 70) return { label: 'CERCA OB', color: '#f59e0b', bgColor: 'rgba(245, 158, 11, 0.06)' }
    if (k <= 30) return { label: 'CERCA OS', color: '#38bdf8', bgColor: 'rgba(56, 189, 248, 0.06)' }
    return { label: 'NEUTRAL', color: '#6a7aa0', bgColor: 'transparent' }
  }

  const cross = (k, d, kPrev, dPrev) => {
    if (k == null || d == null || kPrev == null || dPrev == null) return null
    if (kPrev <= dPrev && k > d) return { dir: 'up', label: '▲ Cross alcista' }
    if (kPrev >= dPrev && k < d) return { dir: 'down', label: '▼ Cross bajista' }
    return null
  }

  const renderSpark = (kHist, dHist) => {
    const W = 280, H = 80, PAD = 4
    const hist = (kHist || []).filter(v => v != null)
    const dhist = (dHist || []).filter(v => v != null)
    if (hist.length < 2) {
      return <div style={{ color: '#3a4a6a', fontSize: 10, textAlign: 'center', padding: 20 }}>Sin historia suficiente</div>
    }
    const n = Math.max(hist.length, dhist.length)
    const xStep = (W - PAD * 2) / (n - 1)
    const yScale = v => PAD + (H - PAD * 2) * (1 - v / 100)
    const kPath = hist.map((v, i) => `${i === 0 ? 'M' : 'L'} ${(PAD + i * xStep).toFixed(1)} ${yScale(v).toFixed(1)}`).join(' ')
    const dPath = dhist.map((v, i) => `${i === 0 ? 'M' : 'L'} ${(PAD + i * xStep).toFixed(1)} ${yScale(v).toFixed(1)}`).join(' ')
    const y80 = yScale(80)
    const y20 = yScale(20)
    const y50 = yScale(50)
    return (
      <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        {/* OB zone */}
        <rect x={PAD} y={PAD} width={W - PAD * 2} height={y80 - PAD} fill="rgba(239, 68, 68, 0.08)" />
        {/* OS zone */}
        <rect x={PAD} y={y20} width={W - PAD * 2} height={H - PAD - y20} fill="rgba(34, 197, 94, 0.08)" />
        {/* grid lines */}
        <line x1={PAD} y1={y80} x2={W - PAD} y2={y80} stroke="#ef4444" strokeWidth="0.5" strokeDasharray="2 3" opacity="0.5" />
        <line x1={PAD} y1={y50} x2={W - PAD} y2={y50} stroke="#2a3550" strokeWidth="0.5" strokeDasharray="2 3" />
        <line x1={PAD} y1={y20} x2={W - PAD} y2={y20} stroke="#22c55e" strokeWidth="0.5" strokeDasharray="2 3" opacity="0.5" />
        {/* %D (orange/red) */}
        <path d={dPath} stroke="#f59e0b" strokeWidth="1.2" fill="none" opacity="0.85" />
        {/* %K (blue) */}
        <path d={kPath} stroke="#38bdf8" strokeWidth="1.5" fill="none" />
        {/* Labels */}
        <text x={W - PAD - 2} y={y80 - 2} fontSize="8" fill="#ef4444" textAnchor="end" opacity="0.7">80</text>
        <text x={W - PAD - 2} y={y20 + 9} fontSize="8" fill="#22c55e" textAnchor="end" opacity="0.7">20</text>
      </svg>
    )
  }

  const renderStoch = (st, label, sublabel) => {
    if (!st || st.k == null || st.d == null) {
      return (
        <div style={{ flex: 1, background: '#0a1020', borderRadius: 6, padding: 12, border: '1px solid #1a2544' }}>
          <div style={{ ...S.label, marginBottom: 4 }}>{label}</div>
          <div style={{ fontSize: 10, color: '#4a5980', marginBottom: 8 }}>{sublabel}</div>
          <div style={{ color: '#4a5980', fontSize: 11, padding: 20, textAlign: 'center' }}>Calculando...</div>
        </div>
      )
    }
    const z = zone(st.k, st.d)
    // detect recent cross using last 2 history points
    const kh = st.kHistory || []
    const dh = st.dHistory || []
    const kPrev = kh.length >= 2 ? kh[kh.length - 2] : null
    const dPrev = dh.length >= 2 ? dh[dh.length - 2] : null
    const cr = cross(st.k, st.d, kPrev, dPrev)

    return (
      <div style={{ flex: 1, background: z.bgColor || '#0a1020', borderRadius: 6, padding: 12, border: `1px solid ${z.color === '#6a7aa0' ? '#1a2544' : z.color}40` }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
          <div style={S.label}>{label}</div>
          <div style={{ fontSize: 9, color: z.color, fontWeight: 700, background: '#0a1020', padding: '2px 6px', borderRadius: 3, letterSpacing: 0.5 }}>{z.label}</div>
        </div>
        <div style={{ fontSize: 10, color: '#4a5980', marginBottom: 8 }}>{sublabel}</div>
        <div style={{ display: 'flex', gap: 16, marginBottom: 8 }}>
          <div>
            <div style={{ ...S.label, color: '#38bdf8' }}>%K</div>
            <div style={{ ...S.mono, fontSize: 18, fontWeight: 700, color: z.color }}>{st.k.toFixed(1)}</div>
          </div>
          <div>
            <div style={{ ...S.label, color: '#f59e0b' }}>%D</div>
            <div style={{ ...S.mono, fontSize: 18, fontWeight: 700, color: '#f59e0b' }}>{st.d.toFixed(1)}</div>
          </div>
          {cr && (
            <div style={{ alignSelf: 'flex-end' }}>
              <div style={{ fontSize: 10, color: cr.dir === 'up' ? '#22c55e' : '#ef4444', fontWeight: 700 }}>{cr.label}</div>
            </div>
          )}
        </div>
        {renderSpark(st.kHistory, st.dHistory)}
      </div>
    )
  }

  // Combined read
  let signal = null
  if (slow && fast && slow.k != null && fast.k != null) {
    const slowOB = slow.k >= 80 && slow.d >= 80
    const slowOS = slow.k <= 20 && slow.d <= 20
    const kh = fast.kHistory || []; const dh = fast.dHistory || []
    const kPrev = kh.length >= 2 ? kh[kh.length - 2] : null
    const dPrev = dh.length >= 2 ? dh[dh.length - 2] : null
    const fastCrossUp = kPrev != null && dPrev != null && kPrev <= dPrev && fast.k > fast.d && fast.k < 30
    const fastCrossDn = kPrev != null && dPrev != null && kPrev >= dPrev && fast.k < fast.d && fast.k > 70
    if (slowOS && fastCrossUp) signal = { level: 'bullish', text: `Slow OS + Fast cross alcista en ${timeframe} — setup de rebote fuerte` }
    else if (slowOB && fastCrossDn) signal = { level: 'bearish', text: `Slow OB + Fast cross bajista en ${timeframe} — setup de techo fuerte` }
    else if (slowOS) signal = { level: 'bullish', text: `Slow Stoch ${timeframe} en OS — régimen sobrevendido, esperar confirmación fast` }
    else if (slowOB) signal = { level: 'bearish', text: `Slow Stoch ${timeframe} en OB — régimen sobrecomprado, esperar confirmación fast` }
    else if (fastCrossUp) signal = { level: 'caution', text: `Fast cross alcista en OS (${timeframe}) — rebote sin confirmación de régimen` }
    else if (fastCrossDn) signal = { level: 'caution', text: `Fast cross bajista en OB (${timeframe}) — techo sin confirmación de régimen` }
    else signal = { level: 'neutral', text: `Sin setup claro de estocásticos en ${timeframe}` }
  }

  return (
    <div>
      {/* Timeframe selector */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        <span style={{ ...S.label, marginRight: 6 }}>TF</span>
        {timeframes.map(tf => (
          <button key={tf} onClick={() => setTimeframe(tf)} style={{
            padding: '3px 10px', borderRadius: 5, border: `1px solid ${timeframe === tf ? '#38bdf8' : '#1a2544'}`,
            background: timeframe === tf ? '#081624' : 'transparent', color: timeframe === tf ? '#38bdf8' : '#4a5980',
            cursor: 'pointer', fontSize: 10, fontFamily: "'IBM Plex Mono', monospace", fontWeight: timeframe === tf ? 700 : 400,
          }}>{tf}</button>
        ))}
        <span style={{ ...S.mono, fontSize: 9, color: '#3a4a6a', marginLeft: 4 }}>
          {tfLabels[timeframe]} — alimenta el score de la señal
        </span>
      </div>

      {signal && (
        <div style={{ marginBottom: 10 }}>
          <Signal level={signal.level} text={signal.text} />
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 10 }}>
        {renderStoch(slow, 'SLOW STOCH', '400,40,10 — régimen de fondo')}
        {renderStoch(fast, 'FAST STOCH', '100,10,4 — timing de entrada')}
      </div>

      <div style={{ marginTop: 10, fontSize: 10, color: '#4a5980', lineHeight: 1.6 }}>
        <span style={{ color: '#38bdf8' }}>%K</span> (azul) cruzando <span style={{ color: '#f59e0b' }}>%D</span> (naranja) al alza en zona OS = señal de compra ·
        Al revés en zona OB = señal de venta · Lectura combinada: slow define régimen, fast da el timing.
      </div>
    </div>
  )
}

// ── Long/Short Ratio Panel (multi-period) ─────────────────────────────
function LongShortPanel({ longShort, signal }) {
  const [period, setPeriod] = useState('1h')
  const periods = ['5m', '15m', '1h', '4h', '1d']
  const periodLabels = { '5m': '5min', '15m': '15min', '1h': '1 hora', '4h': '4 horas', '1d': '1 día' }

  const byPeriod = longShort?.byPeriod || {}
  const topByPeriod = longShort?.topByPeriod || {}

  // Get latest values from selected period
  const retailSeries = byPeriod[period] || []
  const topSeries = topByPeriod[period] || []
  const latestRetail = retailSeries.length > 0 ? retailSeries[retailSeries.length - 1] : null
  const latestTop = topSeries.length > 0 ? topSeries[topSeries.length - 1] : null

  const retailRatio = latestRetail?.ratio
  const retailLongPct = latestRetail?.longPct ?? null  // already decimal (0.59)
  const retailShortPct = latestRetail?.shortPct ?? null
  const topRatio = latestTop?.ratio
  const topLongPct = latestTop?.longPct ?? null
  const topShortPct = latestTop?.shortPct ?? null

  const divergence = (retailRatio != null && topRatio != null) ? +(retailRatio - topRatio).toFixed(2) : null

  const fmt = (n) => n != null ? n.toFixed(2) : '—'

  return (
    <div>
      {/* Period selector */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 12, flexWrap: 'wrap' }}>
        {periods.map(p => (
          <button key={p} onClick={() => setPeriod(p)} style={{
            padding: '3px 10px', borderRadius: 5, border: `1px solid ${period === p ? '#a78bfa' : '#1a2544'}`,
            background: period === p ? '#1a1040' : 'transparent', color: period === p ? '#a78bfa' : '#4a5980',
            cursor: 'pointer', fontSize: 10, fontFamily: "'IBM Plex Mono', monospace", fontWeight: period === p ? 700 : 400,
          }}>{p}</button>
        ))}
        <span style={{ ...S.mono, fontSize: 9, color: '#3a4a6a', alignSelf: 'center', marginLeft: 4 }}>
          resolución por vela
        </span>
      </div>

      {/* Bars */}
      {retailLongPct != null && (
        <DualBar longPct={retailLongPct} shortPct={retailShortPct} label={`Retail (Global) — ${periodLabels[period]}`} />
      )}
      {topLongPct != null && (
        <DualBar longPct={topLongPct} shortPct={topShortPct} label={`Top Traders (Smart Money) — ${periodLabels[period]}`} />
      )}

      {/* Chart: use selected period's history */}
      {retailSeries.length > 0 && (
        <div style={{ marginTop: 8, marginBottom: 8 }}>
          <div style={S.label}>L/S Ratio historial ({period})</div>
          <div style={{ marginTop: 4 }}>
            <Spark data={retailSeries} color="#a78bfa" valueKey="ratio" />
          </div>
        </div>
      )}

      {/* Numbers */}
      <div style={{ display: 'flex', gap: 16, marginBottom: 10 }}>
        <div>
          <div style={S.label}>Retail</div>
          <div style={{ ...S.mono, fontSize: 16, fontWeight: 700 }}>{fmt(retailRatio)}</div>
        </div>
        <div>
          <div style={S.label}>Top Traders</div>
          <div style={{ ...S.mono, fontSize: 16, fontWeight: 700 }}>{fmt(topRatio)}</div>
        </div>
        <div>
          <div style={S.label}>Divergencia</div>
          <div style={{ ...S.mono, fontSize: 16, fontWeight: 700, color: divergence != null && Math.abs(divergence) > 0.3 ? '#f59e0b' : '#5a6a8a' }}>
            {divergence != null ? (divergence > 0 ? '+' : '') + divergence.toFixed(2) : '—'}
          </div>
        </div>
      </div>
      {signal && <Signal level={signal.level} text={signal.text} />}
    </div>
  )
}

// ── Taker Flow (cumulative delta by period) ───────────────────────────
function TakerFlow({ flow }) {
  const [period, setPeriod] = useState('4h')
  const periods = ['1h', '4h', '12h', '24h']

  const perp = flow?.perp?.[period] || {}
  const spot = flow?.spot?.[period] || {}

  const renderBar = (buy, sell, color) => {
    const total = (buy || 0) + (sell || 0)
    if (!total) return null
    const buyPct = (buy / total) * 100
    const sellPct = (sell / total) * 100
    return (
      <div style={{ display: 'flex', height: 8, borderRadius: 4, overflow: 'hidden', gap: 1 }}>
        <div style={{ width: `${buyPct}%`, background: '#16a34a', borderRadius: '4px 0 0 4px', transition: 'width 0.4s' }} />
        <div style={{ width: `${sellPct}%`, background: '#dc2626', borderRadius: '0 4px 4px 0', transition: 'width 0.4s' }} />
      </div>
    )
  }

  const fmt1 = (n) => {
    if (n == null) return '—'
    const abs = Math.abs(n)
    if (abs >= 1000000) return `${(n / 1000000).toFixed(2)}M`
    if (abs >= 1000) return `${(n / 1000).toFixed(1)}K`
    return n.toFixed(0)
  }

  const renderSide = (data, label, color) => {
    if (!data || data.buy == null) return <div style={{ color: '#4a5980', fontSize: 11 }}>Sin datos</div>
    const { buy, sell, delta, ratio, totalVol, priceChgPct, deltaVsVol } = data
    const deltaPositive = delta >= 0
    const deltaColor = deltaPositive ? '#22c55e' : '#ef4444'
    const ratioColor = ratio > 1.1 ? '#22c55e' : ratio < 0.9 ? '#ef4444' : '#8a9ac0'
    return (
      <div>
        <div style={{ display: 'flex', gap: 12, marginBottom: 6, flexWrap: 'wrap' }}>
          <div>
            <div style={S.label}>Buy vol</div>
            <div style={{ ...S.mono, fontSize: 14, fontWeight: 700, color: '#22c55e' }}>{fmt1(buy)} ETH</div>
          </div>
          <div>
            <div style={S.label}>Sell vol</div>
            <div style={{ ...S.mono, fontSize: 14, fontWeight: 700, color: '#ef4444' }}>{fmt1(sell)} ETH</div>
          </div>
          <div>
            <div style={S.label}>Delta neto</div>
            <div style={{ ...S.mono, fontSize: 14, fontWeight: 700, color: deltaColor }}>
              {deltaPositive ? '+' : ''}{fmt1(delta)} ETH
            </div>
          </div>
          <div>
            <div style={S.label}>Ratio</div>
            <div style={{ ...S.mono, fontSize: 14, fontWeight: 700, color: ratioColor }}>
              {ratio != null ? ratio.toFixed(3) : '—'}
            </div>
          </div>
          {totalVol != null && (
            <div>
              <div style={S.label}>Vol total</div>
              <div style={{ ...S.mono, fontSize: 14, fontWeight: 700, color: '#8a9ac0' }}>{fmt1(totalVol)} ETH</div>
            </div>
          )}
          {deltaVsVol != null && (
            <div>
              <div style={S.label}>Delta/Vol</div>
              <div style={{ ...S.mono, fontSize: 14, fontWeight: 700, color: Math.abs(deltaVsVol) > 10 ? (deltaVsVol > 0 ? '#22c55e' : '#ef4444') : '#8a9ac0' }}>
                {deltaVsVol > 0 ? '+' : ''}{deltaVsVol}%
              </div>
            </div>
          )}
        </div>
        {renderBar(buy, sell, color)}
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, color: '#3a4a6a', marginTop: 2, ...S.mono }}>
          <span>BUY {buy > 0 ? ((buy / (buy + sell)) * 100).toFixed(1) : 0}%</span>
          <span>SELL {sell > 0 ? ((sell / (buy + sell)) * 100).toFixed(1) : 0}%</span>
        </div>
      </div>
    )
  }

  return (
    <div>
      {/* Period selector */}
      <div style={{ display: 'flex', gap: 6, marginBottom: 14 }}>
        {periods.map(p => (
          <button key={p} onClick={() => setPeriod(p)} style={{
            padding: '4px 12px', borderRadius: 5, border: `1px solid ${period === p ? '#38bdf8' : '#1a2544'}`,
            background: period === p ? '#0c2a3a' : 'transparent', color: period === p ? '#38bdf8' : '#4a5980',
            cursor: 'pointer', fontSize: 11, fontFamily: "'IBM Plex Mono', monospace", fontWeight: period === p ? 700 : 400,
            transition: 'all 0.2s',
          }}>{p}</button>
        ))}
        <span style={{ ...S.mono, fontSize: 10, color: '#3a4a6a', alignSelf: 'center', marginLeft: 4 }}>
          ventana de análisis
        </span>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        <div>
          <div style={{ ...S.label, color: '#a78bfa', marginBottom: 8 }}>PERP — Futuros</div>
          {renderSide(perp, 'perp', '#a78bfa')}
        </div>
        <div style={{ borderLeft: '1px solid #1a2544', paddingLeft: 16 }}>
          <div style={{ ...S.label, color: '#fbbf24', marginBottom: 8 }}>SPOT — Contado</div>
          {renderSide(spot, 'spot', '#fbbf24')}
        </div>
      </div>

      {/* Cross-market conclusion */}
      {perp.delta != null && spot.delta != null && (
        <div style={{ marginTop: 12, padding: '8px 12px', background: '#080f20', borderRadius: 6, border: '1px solid #1a2544', fontSize: 11 }}>
          {perp.delta > 0 && spot.delta > 0 && (
            <span style={{ color: '#4ade80' }}>✓ Flujo comprador en perp y spot — presión alcista real en las últimas {period}</span>
          )}
          {perp.delta < 0 && spot.delta < 0 && (
            <span style={{ color: '#f87171' }}>✓ Flujo vendedor en perp y spot — presión bajista real en las últimas {period}</span>
          )}
          {perp.delta > 0 && spot.delta < 0 && (
            <span style={{ color: '#fbbf24' }}>⚠ Perp compra pero spot vende — especuladores long sin respaldo real. Riesgo de trampa</span>
          )}
          {perp.delta < 0 && spot.delta > 0 && (
            <span style={{ color: '#fbbf24' }}>⚠ Perp vende pero spot absorbe — shorts contra compradores reales. Posible squeeze</span>
          )}
        </div>
      )}

      {/* Divergence analysis: delta vs price */}
      {(() => {
        const combinedDelta = (perp.delta || 0) + (spot.delta || 0)
        const combinedVol = (perp.totalVol || 0) + (spot.totalVol || 0)
        const pChg = perp.priceChgPct  // perp price tracks better
        const deltaIntensity = combinedVol > 0 ? Math.abs(combinedDelta / combinedVol * 100) : 0
        if (pChg == null || combinedVol === 0) return null

        const buyDominant = combinedDelta > 0
        const priceUp = pChg > 0.15
        const priceDown = pChg < -0.15
        const priceFlat = !priceUp && !priceDown
        const strongDelta = deltaIntensity > 5  // delta > 5% of vol = significant

        let divergence = null
        let divColor = '#8a9ac0'
        let divIcon = '—'

        if (buyDominant && strongDelta && (priceFlat || priceDown)) {
          divergence = `Divergencia bajista: delta comprador fuerte (${deltaIntensity.toFixed(1)}% del vol) pero precio ${priceDown ? 'bajó' : 'no subió'} (${pChg > 0 ? '+' : ''}${pChg.toFixed(2)}%) — hay ventas pasivas absorbiendo. Posible distribución/acumulación de shorts`
          divColor = '#f59e0b'
          divIcon = '⚠'
        } else if (!buyDominant && strongDelta && (priceFlat || priceUp)) {
          divergence = `Divergencia alcista: delta vendedor fuerte (${deltaIntensity.toFixed(1)}% del vol) pero precio ${priceUp ? 'subió' : 'no bajó'} (${pChg > 0 ? '+' : ''}${pChg.toFixed(2)}%) — hay compras pasivas absorbiendo. Posible acumulación`
          divColor = '#f59e0b'
          divIcon = '⚠'
        } else if (buyDominant && strongDelta && priceUp) {
          divergence = `Flujo confirma precio: compras agresivas (${deltaIntensity.toFixed(1)}% del vol) + precio sube ${pChg > 0 ? '+' : ''}${pChg.toFixed(2)}% — movimiento respaldado`
          divColor = '#4ade80'
          divIcon = '✓'
        } else if (!buyDominant && strongDelta && priceDown) {
          divergence = `Flujo confirma precio: ventas agresivas (${deltaIntensity.toFixed(1)}% del vol) + precio baja ${pChg.toFixed(2)}% — movimiento respaldado`
          divColor = '#f87171'
          divIcon = '✓'
        } else if (!strongDelta) {
          divergence = `Sin presión clara: delta solo ${deltaIntensity.toFixed(1)}% del volumen total — flujo equilibrado`
          divColor = '#4a5980'
          divIcon = '○'
        }

        if (!divergence) return null
        return (
          <div style={{ marginTop: 8, padding: '8px 12px', background: '#0a1428', borderRadius: 6, border: `1px solid ${divColor}33`, fontSize: 11 }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
              <span style={{ fontSize: 13 }}>{divIcon}</span>
              <div>
                <div style={{ color: divColor, fontWeight: 600, marginBottom: 4, ...S.mono }}>{divergence}</div>
                <div style={{ color: '#3a4a6a', fontSize: 10, ...S.mono }}>
                  Precio: ${perp.priceOpen?.toFixed(2)} → ${perp.priceClose?.toFixed(2)} ({pChg > 0 ? '+' : ''}{pChg.toFixed(2)}%) · Delta combinado (perp+spot): {combinedDelta > 0 ? '+' : ''}{fmt1(combinedDelta)} ETH de {fmt1(combinedVol)} ETH totales
                </div>
              </div>
            </div>
          </div>
        )
      })()}
    </div>
  )
}

// ── Liquidation Map ──────────────────────────────────────────────────
function LiquidationMap({ liqMap }) {
  if (!liqMap || !liqMap.clusters || liqMap.clusters.length === 0) {
    return <div style={{ color: '#4a5980', fontSize: 12 }}>Cargando mapa de liquidaciones...</div>
  }

  const { clusters, totalOiUsd, oiByExchange, spotPrice } = liqMap
  const maxOi = Math.max(...clusters.flatMap(c => [c.longOiUsd, c.shortOiUsd]), 1)

  // Build visual bars: longs liquidate below, shorts liquidate above
  const longBars = clusters.map(c => ({ price: c.longLiqPrice, oi: c.longOiUsd, pct: c.longPctFromSpot, lev: c.leverage, side: 'long' }))
    .filter(b => b.pct > -50).sort((a, b) => b.price - a.price)
  const shortBars = clusters.map(c => ({ price: c.shortLiqPrice, oi: c.shortOiUsd, pct: c.shortPctFromSpot, lev: c.leverage, side: 'short' }))
    .filter(b => b.pct < 50).sort((a, b) => b.price - a.price)

  const allBars = [...shortBars, ...longBars].sort((a, b) => b.price - a.price)

  // Cumulative OI below spot (long liqs) and above spot (short liqs)
  const longLiqTotal = clusters.reduce((s, c) => s + c.longOiUsd, 0)
  const shortLiqTotal = clusters.reduce((s, c) => s + c.shortOiUsd, 0)

  // Nearest dense cluster
  const nearestLong = longBars.filter(b => Math.abs(b.pct) < 5).sort((a, b) => b.oi - a.oi)[0]
  const nearestShort = shortBars.filter(b => Math.abs(b.pct) < 5).sort((a, b) => b.oi - a.oi)[0]

  const fmt$ = (n) => {
    if (n >= 1e9) return `$${(n/1e9).toFixed(2)}B`
    if (n >= 1e6) return `$${(n/1e6).toFixed(0)}M`
    if (n >= 1e3) return `$${(n/1e3).toFixed(0)}K`
    return `$${n}`
  }

  return (
    <div>
      {/* Summary */}
      <div style={{ display: 'flex', gap: 16, marginBottom: 12, flexWrap: 'wrap' }}>
        <div>
          <div style={S.label}>OI Total (todos los exchanges)</div>
          <div style={{ ...S.mono, fontSize: 16, fontWeight: 700, color: '#e2e8f0' }}>{fmt$(totalOiUsd)}</div>
        </div>
        <div>
          <div style={S.label}>Liq Longs (abajo)</div>
          <div style={{ ...S.mono, fontSize: 14, fontWeight: 700, color: '#ef4444' }}>{fmt$(longLiqTotal)}</div>
        </div>
        <div>
          <div style={S.label}>Liq Shorts (arriba)</div>
          <div style={{ ...S.mono, fontSize: 14, fontWeight: 700, color: '#22c55e' }}>{fmt$(shortLiqTotal)}</div>
        </div>
        <div>
          <div style={S.label}>Spot Price</div>
          <div style={{ ...S.mono, fontSize: 14, fontWeight: 700, color: '#38bdf8' }}>${spotPrice?.toFixed(2)}</div>
        </div>
      </div>

      {/* OI by exchange */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 12, fontSize: 10, color: '#4a5a7a', ...S.mono }}>
        {Object.entries(oiByExchange || {}).map(([ex, val]) => (
          <span key={ex}>{ex}: {fmt$(val)}</span>
        ))}
      </div>

      {/* Nearest danger zones */}
      {(nearestLong || nearestShort) && (
        <div style={{ marginBottom: 12, padding: '8px 12px', background: '#0a0815', borderRadius: 6, border: '1px solid #dc262633', fontSize: 11 }}>
          {nearestLong && (
            <div style={{ color: '#f87171', marginBottom: 4 }}>
              Zona de liq longs cercana: ${nearestLong.price.toFixed(0)} ({nearestLong.pct.toFixed(1)}%) — {nearestLong.lev}x — {fmt$(nearestLong.oi)}
            </div>
          )}
          {nearestShort && (
            <div style={{ color: '#4ade80' }}>
              Zona de liq shorts cercana: ${nearestShort.price.toFixed(0)} (+{nearestShort.pct.toFixed(1)}%) — {nearestShort.lev}x — {fmt$(nearestShort.oi)}
            </div>
          )}
        </div>
      )}

      {/* Visual map */}
      <div style={{ maxHeight: 400, overflowY: 'auto' }}>
        {allBars.map((bar, i) => {
          const isSpot = spotPrice && Math.abs(bar.price - spotPrice) / spotPrice < 0.005
          const barPct = (bar.oi / maxOi) * 100
          const color = bar.side === 'long' ? '#ef4444' : '#22c55e'
          const bgColor = bar.side === 'long' ? 'rgba(239,68,68,' : 'rgba(34,197,94,'
          return (
            <div key={`${bar.side}-${bar.lev}`}>
              {/* Spot price marker */}
              {i > 0 && allBars[i-1].price > (spotPrice || 0) && bar.price <= (spotPrice || 0) && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '4px 0', borderTop: '2px solid #38bdf8', borderBottom: '2px solid #38bdf8', margin: '4px 0' }}>
                  <span style={{ ...S.mono, fontSize: 11, fontWeight: 700, color: '#38bdf8', width: 75, textAlign: 'right' }}>
                    SPOT ${spotPrice?.toFixed(0)}
                  </span>
                  <div style={{ flex: 1, height: 2, background: '#38bdf8' }} />
                </div>
              )}
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '2px 0' }}>
                <span style={{ ...S.mono, fontSize: 10, width: 75, textAlign: 'right', flexShrink: 0, color: isSpot ? '#38bdf8' : '#5a6a8a' }}>
                  ${bar.price.toFixed(0)}
                </span>
                <span style={{ ...S.mono, fontSize: 9, width: 40, textAlign: 'right', color: '#4a5a7a', flexShrink: 0 }}>
                  {bar.lev}x
                </span>
                <div style={{ flex: 1, height: 12, background: '#0a1020', borderRadius: 3, overflow: 'hidden', position: 'relative' }}>
                  <div style={{
                    height: '100%', borderRadius: 3,
                    width: `${Math.max(barPct, 2)}%`,
                    background: `${bgColor}${(0.3 + barPct/100*0.7).toFixed(2)})`,
                  }} />
                </div>
                <span style={{ ...S.mono, fontSize: 9, width: 55, textAlign: 'right', flexShrink: 0, color }}>
                  {fmt$(bar.oi)}
                </span>
                <span style={{ ...S.mono, fontSize: 8, width: 40, textAlign: 'right', flexShrink: 0, color: '#3a4a6a' }}>
                  {bar.pct > 0 ? '+' : ''}{bar.pct.toFixed(1)}%
                </span>
              </div>
            </div>
          )
        })}
      </div>
      <div style={{ display: 'flex', gap: 16, marginTop: 8, fontSize: 10, color: '#3a4a6a' }}>
        <span style={{ color: '#ef4444' }}>LONG liqs (si baja)</span>
        <span style={{ color: '#22c55e' }}>SHORT liqs (si sube)</span>
        <span style={{ color: '#38bdf8' }}>Precio actual</span>
      </div>
      <div style={{ marginTop: 8, fontSize: 10, color: '#3a4a6a', fontStyle: 'italic' }}>
        Estimación basada en distribución de leverage sobre OI total. No refleja posiciones reales individuales.
      </div>
    </div>
  )
}

// ── IV Term Structure ────────────────────────────────────────────────
function IvTermStructure({ data: ivData }) {
  if (!ivData || ivData.length < 2) {
    return <div style={{ color: '#4a5980', fontSize: 12 }}>Sin datos de term structure...</div>
  }

  const W = 600, H = 200, padL = 45, padR = 20, padT = 15, padB = 30
  const chartW = W - padL - padR
  const chartH = H - padT - padB

  const maxDte = Math.max(...ivData.map(d => d.dte), 1)
  const minIv = Math.min(...ivData.map(d => d.iv))
  const maxIv = Math.max(...ivData.map(d => d.iv))
  const ivRange = maxIv - minIv || 1

  const xScale = (dte) => padL + (dte / maxDte) * chartW
  const yScale = (iv) => padT + chartH - ((iv - minIv + ivRange * 0.05) / (ivRange * 1.1)) * chartH

  const points = ivData.map(d => ({ x: xScale(d.dte), y: yScale(d.iv), dte: d.dte, iv: d.iv }))
  const linePath = points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ')

  // Contango (upward slope) vs backwardation
  const slope = ivData.length >= 2 ? ivData[ivData.length - 1].iv - ivData[0].iv : 0
  const structure = slope > 3 ? 'Contango — mercado espera más vol a futuro' : slope < -3 ? 'Backwardation — miedo en corto plazo' : 'Flat — sin expectativa clara'
  const structColor = slope > 3 ? '#22c55e' : slope < -3 ? '#ef4444' : '#8a9ac0'

  return (
    <div>
      <div style={{ marginBottom: 8, fontSize: 11, color: structColor, fontWeight: 600, ...S.mono }}>
        {structure} (pendiente {slope > 0 ? '+' : ''}{slope.toFixed(1)}%)
      </div>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ background: '#060d1a', borderRadius: 6 }}>
        {/* Grid */}
        {[0, 7, 14, 30, 60, 90, 120, 150, 180].filter(d => d <= maxDte).map(d => (
          <g key={d}>
            <line x1={xScale(d)} y1={padT} x2={xScale(d)} y2={H - padB} stroke="#0f1a2e" strokeWidth={0.5} />
            <text x={xScale(d)} y={H - padB + 12} textAnchor="middle" fill="#3a4a6a" fontSize={8} fontFamily="monospace">{d}d</text>
          </g>
        ))}
        {/* IV axis */}
        {Array.from({ length: 5 }, (_, i) => minIv + (ivRange / 4) * i).map((iv, i) => (
          <g key={i}>
            <line x1={padL} y1={yScale(iv)} x2={W - padR} y2={yScale(iv)} stroke="#0f1a2e" strokeWidth={0.5} />
            <text x={padL - 4} y={yScale(iv) + 3} textAnchor="end" fill="#3a4a6a" fontSize={8} fontFamily="monospace">{iv.toFixed(0)}%</text>
          </g>
        ))}
        {/* Line */}
        <path d={linePath} fill="none" stroke="#a78bfa" strokeWidth={2} strokeLinejoin="round" />
        {/* Points */}
        {points.map((p, i) => (
          <g key={i}>
            <circle cx={p.x} cy={p.y} r={4} fill="#a78bfa" stroke="#060d1a" strokeWidth={1.5} />
            <text x={p.x} y={p.y - 8} textAnchor="middle" fill="#c4b5fd" fontSize={7} fontFamily="monospace">{p.iv.toFixed(0)}%</text>
          </g>
        ))}
        <text x={W / 2} y={H - 2} textAnchor="middle" fill="#3a4a6a" fontSize={8}>Días hasta vencimiento (DTE)</text>
      </svg>
    </div>
  )
}

// ── Key Levels Panel (consolidated) ──────────────────────────────────
function KeyLevelsPanel({ data }) {
  if (!data) return null
  const opts = data.options || {}
  const vp = data.volumeProfile || {}
  const depth = data._depth  // passed separately
  const price = data.binance?.price

  if (!price) return null

  const levels = []

  // Options levels
  if (opts.gammaFlip) levels.push({ price: opts.gammaFlip, label: 'Gamma Flip', source: 'options', color: '#3b82f6', importance: 3 })
  if (opts.callWall) levels.push({ price: opts.callWall, label: 'Call Wall', source: 'options', color: '#ef4444', importance: 2 })
  if (opts.putWall) levels.push({ price: opts.putWall, label: 'Put Wall', source: 'options', color: '#22c55e', importance: 2 })
  if (opts.maxPain) levels.push({ price: opts.maxPain, label: `Max Pain (${opts.nearestExpiry || ''})`, source: 'options', color: '#f59e0b', importance: 2 })

  // Volume Profile levels
  if (vp.poc?.price) levels.push({ price: vp.poc.price, label: 'POC', source: 'volume', color: '#fbbf24', importance: 3 })
  if (vp.vah) levels.push({ price: vp.vah, label: 'VAH', source: 'volume', color: '#22c55e80', importance: 1 })
  if (vp.val) levels.push({ price: vp.val, label: 'VAL', source: 'volume', color: '#ef444480', importance: 1 })
  ;(vp.hvn || []).slice(0, 3).forEach(h => {
    levels.push({ price: h.price, label: 'HVN', source: 'volume', color: '#fbbf2480', importance: 1 })
  })

  // Depth walls
  if (depth?.bidWalls) {
    depth.bidWalls.slice(0, 3).forEach(w => {
      levels.push({ price: w.price, label: `Bid Wall (${w.qty.toFixed(0)} ETH)`, source: 'orderbook', color: '#22c55e', importance: 1 })
    })
  }
  if (depth?.askWalls) {
    depth.askWalls.slice(0, 3).forEach(w => {
      levels.push({ price: w.price, label: `Ask Wall (${w.qty.toFixed(0)} ETH)`, source: 'orderbook', color: '#ef4444', importance: 1 })
    })
  }

  if (levels.length === 0) return null

  // Check for convergence: levels within $5 of each other from different sources
  const sorted = [...levels].sort((a, b) => b.price - a.price)
  const convergences = []
  for (let i = 0; i < sorted.length; i++) {
    for (let j = i + 1; j < sorted.length; j++) {
      if (sorted[i].source !== sorted[j].source && Math.abs(sorted[i].price - sorted[j].price) < 10) {
        convergences.push({
          price: (sorted[i].price + sorted[j].price) / 2,
          labels: [sorted[i].label, sorted[j].label],
          sources: [sorted[i].source, sorted[j].source],
        })
      }
    }
  }

  const sourceColors = { options: '#a78bfa', volume: '#fbbf24', orderbook: '#38bdf8' }

  return (
    <div>
      {/* Convergence alerts */}
      {convergences.length > 0 && (
        <div style={{ marginBottom: 12, padding: '8px 12px', background: '#1a1008', borderRadius: 6, border: '1px solid #d9770633' }}>
          <div style={{ ...S.label, color: '#f59e0b', marginBottom: 6 }}>Convergencia de niveles detectada</div>
          {convergences.slice(0, 4).map((c, i) => (
            <div key={i} style={{ fontSize: 11, color: '#fbbf24', ...S.mono, marginBottom: 2 }}>
              ${c.price.toFixed(0)} — {c.labels.join(' + ')} ({c.sources.join(' + ')})
            </div>
          ))}
        </div>
      )}

      {/* Price ladder */}
      <div style={{ maxHeight: 320, overflowY: 'auto' }}>
        {sorted.map((level, i) => {
          const distPct = ((level.price - price) / price * 100)
          const isAbove = level.price > price
          const isNear = Math.abs(distPct) < 1
          return (
            <div key={`${level.label}-${level.price}-${i}`} style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '3px 0',
              borderLeft: `3px solid ${level.color}`, paddingLeft: 8,
              background: isNear ? '#ffffff08' : 'transparent',
            }}>
              <span style={{ ...S.mono, fontSize: 11, fontWeight: 700, color: level.color, width: 70, textAlign: 'right', flexShrink: 0 }}>
                ${Number(level.price).toFixed(0)}
              </span>
              <span style={{ ...S.mono, fontSize: 10, color: '#6a7aa0', width: 50, textAlign: 'right', flexShrink: 0 }}>
                {distPct > 0 ? '+' : ''}{distPct.toFixed(1)}%
              </span>
              <span style={{ fontSize: 10, color: sourceColors[level.source] || '#8a9ac0', padding: '1px 6px', background: '#0a1020', borderRadius: 3, flexShrink: 0 }}>
                {level.source}
              </span>
              <span style={{ fontSize: 10, color: '#8a9ac0', flex: 1 }}>{level.label}</span>
              <span style={{ display: 'flex', gap: 2 }}>
                {[...Array(level.importance)].map((_, j) => (
                  <div key={j} style={{ width: 4, height: 4, borderRadius: '50%', background: level.color }} />
                ))}
              </span>
            </div>
          )
        })}
      </div>
      <div style={{ marginTop: 8, display: 'flex', gap: 12, fontSize: 9, color: '#3a4a6a' }}>
        <span style={{ color: '#a78bfa' }}>opciones</span>
        <span style={{ color: '#fbbf24' }}>volumen</span>
        <span style={{ color: '#38bdf8' }}>orderbook</span>
        <span style={{ color: '#f59e0b' }}>convergencia = nivel muy fuerte</span>
      </div>
    </div>
  )
}

// ── Signals Logic ────────────────────────────────────────────────────
function computeSignals(data, period = '1h', stochTf = '1h') {
  if (!data) return { funding: null, oi: null, taker: null, divergence: null, marketState: null }

  const bn   = data.binance       || {}
  const vol  = data.volatility    || {}
  const vp   = data.volumeProfile || {}
  const opts = data.options       || {}

  const fundingRate = bn.funding?.rate
  const oiChange    = bn.openInterest?.change48h

  // Period-aware: pull L/S from byPeriod if available
  const lsPeriodMap = { '5m': '5m', '15m': '15m', '1h': '1h', '4h': '4h', '12h': '4h', '1d': '1d', '15d': '1d' }
  const lsPeriod = lsPeriodMap[period] || '1h'
  const retailSeries = bn.longShort?.byPeriod?.[lsPeriod] || []
  const topSeries = bn.longShort?.topByPeriod?.[lsPeriod] || bn.longShort?.topByPeriod?.['1h'] || []
  const latestRetail = retailSeries.length > 0 ? retailSeries[retailSeries.length - 1] : null
  const latestTop = topSeries.length > 0 ? topSeries[topSeries.length - 1] : null

  const retailRatio = latestRetail?.ratio ?? bn.longShort?.globalRatio
  const topRatio    = latestTop?.ratio ?? bn.longShort?.topTradersRatio
  const divergence_val = (retailRatio != null && topRatio != null) ? retailRatio - topRatio : bn.longShort?.divergence

  // Period-aware: pull taker from flow if available
  const flowPeriodMap = { '5m': '1h', '15m': '1h', '1h': '1h', '4h': '4h', '12h': '12h', '1d': '24h', '15d': '24h' }
  const flowPeriod = flowPeriodMap[period] || '1h'
  const perpFlow = bn.takerBuySell?.flow?.perp?.[flowPeriod] || {}
  const takerRatio  = perpFlow.ratio ?? bn.takerBuySell?.ratio

  // ── Funding ──
  let funding
  if (fundingRate == null) {
    funding = { level: 'neutral', text: 'SIN DATOS' }
  } else if (fundingRate > 0.0003) {
    funding = { level: 'bearish', text: `Funding +${(fundingRate * 100).toFixed(4)}% — Longs cargados, combustible para caída` }
  } else if (fundingRate > 0.0001) {
    funding = { level: 'caution', text: `Funding +${(fundingRate * 100).toFixed(4)}% — Sesgo long moderado` }
  } else if (fundingRate < -0.0003) {
    funding = { level: 'bullish', text: `Funding ${(fundingRate * 100).toFixed(4)}% — Shorts cargados, combustible para suba` }
  } else if (fundingRate < -0.0001) {
    funding = { level: 'caution', text: `Funding ${(fundingRate * 100).toFixed(4)}% — Sesgo short moderado` }
  } else {
    funding = { level: 'neutral', text: `Funding ${(fundingRate * 100).toFixed(4)}% — Neutral` }
  }

  // ── OI ──
  let oi
  if (oiChange == null) {
    oi = { level: 'neutral', text: 'SIN DATOS' }
  } else if (oiChange > 10) {
    oi = { level: 'caution', text: `OI +${oiChange.toFixed(1)}% 48h — Apalancamiento creciendo` }
  } else if (oiChange < -10) {
    oi = { level: 'caution', text: `OI ${oiChange.toFixed(1)}% 48h — Desapalancamiento` }
  } else {
    oi = { level: 'neutral', text: `OI ${oiChange > 0 ? '+' : ''}${oiChange.toFixed(1)}% 48h — Estable` }
  }

  // ── Taker (perp + spot) ──
  const spotRatio = bn.takerBuySell?.spotRatio
  let taker
  if (takerRatio == null) {
    taker = { level: 'neutral', text: 'SIN DATOS' }
  } else {
    const perpBull = takerRatio > 1.15, perpBear = takerRatio < 0.85
    const spotBull = spotRatio != null && spotRatio > 1.05
    const spotBear = spotRatio != null && spotRatio < 0.95
    const spotStr  = spotRatio != null ? ` · Spot ${spotRatio.toFixed(3)}` : ''
    if (perpBull && spotBull) {
      taker = { level: 'bullish', text: `Perp ${takerRatio.toFixed(2)} + Spot ${spotRatio.toFixed(3)} — Flujo comprador confirmado en ambos mercados` }
    } else if (perpBear && spotBear) {
      taker = { level: 'bearish', text: `Perp ${takerRatio.toFixed(2)} + Spot ${spotRatio.toFixed(3)} — Flujo vendedor confirmado en ambos mercados` }
    } else if ((perpBull && spotBear) || (perpBear && spotBull)) {
      taker = { level: 'caution', text: `Divergencia: Perp ${takerRatio.toFixed(2)}${spotStr} — Especulación no confirma mercado real` }
    } else if (perpBull) {
      taker = { level: 'bullish', text: `Perp ${takerRatio.toFixed(2)}${spotStr} — Compra agresiva en futuros` }
    } else if (perpBear) {
      taker = { level: 'bearish', text: `Perp ${takerRatio.toFixed(2)}${spotStr} — Venta agresiva en futuros` }
    } else {
      taker = { level: 'neutral', text: `Perp ${takerRatio.toFixed(2)}${spotStr} — Equilibrado` }
    }
  }

  // ── Divergencia ──
  let divergence
  if (retailRatio == null || topRatio == null) {
    divergence = { level: 'neutral', text: 'SIN DATOS' }
  } else if (retailRatio > 1.4 && topRatio < 1.15) {
    divergence = { level: 'bearish', text: `Retail long (${retailRatio.toFixed(2)}) vs Smart money neutra (${topRatio.toFixed(2)}) — Cuidado` }
  } else if (retailRatio < 0.7 && topRatio > 0.85) {
    divergence = { level: 'bullish', text: `Retail short (${retailRatio.toFixed(2)}) vs Smart money neutra (${topRatio.toFixed(2)}) — Oportunidad` }
  } else if (divergence_val != null && Math.abs(divergence_val) > 0.5) {
    divergence = { level: 'caution', text: `Divergencia ${divergence_val > 0 ? 'retail más long' : 'retail más short'} (${divergence_val.toFixed(2)})` }
  } else {
    divergence = { level: 'neutral', text: `Retail (${(retailRatio || 0).toFixed(2)}) y Smart money (${(topRatio || 0).toFixed(2)}) alineados` }
  }

  // ── Market State: combina TODOS los indicadores con PESOS POR PERÍODO ──
  // Cada período tiene pesos distintos: qué importa para un scalp de 5m no es lo mismo que para un swing de 15d
  //
  // Peso por factor y período:
  //   scalp (5m,15m): taker=ALTO, L/S=MEDIO, funding/OI/opciones/ethbtc/ivrv=BAJO, VP=MEDIO
  //   intra (1h,4h):  taker=ALTO, funding=MEDIO, OI=MEDIO, L/S=ALTO, opciones=MEDIO, VP=MEDIO
  //   swing (12h,1d): funding=ALTO, OI=ALTO, taker=MEDIO, L/S=ALTO, opciones=ALTO, ethbtc=MEDIO, VP=ALTO
  //   macro (15d):    funding=ALTO, OI=ALTO, taker=BAJO, opciones=ALTO, ethbtc=ALTO, ivrv=ALTO, VP=ALTO
  const W = {
    '5m':  { funding: 0, oi: 0, oiPrice: 0, taker: 2, ls: 1, vol: 0, ivRv: 0, ethBtc: 0, gamma: 0, vp: 1, stoch: 1, mq: 1, setup: 3 },
    '15m': { funding: 0, oi: 0, oiPrice: 0, taker: 2, ls: 1, vol: 0, ivRv: 0, ethBtc: 0, gamma: 0, vp: 1, stoch: 1, mq: 1, setup: 3 },
    '1h':  { funding: 1, oi: 1, oiPrice: 1, taker: 1, ls: 1, vol: 0, ivRv: 0, ethBtc: 0, gamma: 1, vp: 1, stoch: 1, mq: 2, setup: 3 },
    '4h':  { funding: 1, oi: 1, oiPrice: 1, taker: 1, ls: 1, vol: 1, ivRv: 0, ethBtc: 1, gamma: 1, vp: 1, stoch: 2, mq: 2, setup: 4 },
    '12h': { funding: 2, oi: 1, oiPrice: 1, taker: 1, ls: 1, vol: 1, ivRv: 1, ethBtc: 1, gamma: 1, vp: 1, stoch: 2, mq: 2, setup: 3 },
    '1d':  { funding: 2, oi: 2, oiPrice: 1, taker: 1, ls: 1, vol: 1, ivRv: 1, ethBtc: 1, gamma: 2, vp: 1, stoch: 2, mq: 1, setup: 2 },
    '15d': { funding: 2, oi: 2, oiPrice: 1, taker: 0, ls: 1, vol: 1, ivRv: 2, ethBtc: 2, gamma: 2, vp: 1, stoch: 2, mq: 0, setup: 0 },
  }
  const w = W[period] || W['1h']

  const states = []
  let score = 0

  // ── 1. Funding (peso: w.funding) ──
  if (fundingRate != null && w.funding > 0) {
    if (fundingRate > 0.0003)       { states.push(`Longs cargados (×${w.funding})`);   score -= w.funding }
    else if (fundingRate > 0.0001)  { states.push(`Sesgo long (×${w.funding})`);       score -= Math.ceil(w.funding / 2) }
    else if (fundingRate < -0.0003) { states.push(`Shorts cargados (×${w.funding})`);  score += w.funding }
    else if (fundingRate < -0.0001) { states.push(`Sesgo short (×${w.funding})`);      score += Math.ceil(w.funding / 2) }
  } else if (fundingRate != null && w.funding === 0) {
    // Show but don't score
    if (fundingRate > 0.0003)       states.push('Longs cargados (—)')
    else if (fundingRate < -0.0003) states.push('Shorts cargados (—)')
  }

  // ── 2. OI + Price combo (peso: w.oi, w.oiPrice) ──
  const priceChg24h = bn.priceChange24h
  if (oiChange != null && w.oi > 0) {
    if (oiChange > 8 && fundingRate > 0.0001)       { states.push(`OI creciendo con longs (×${w.oi})`);  score -= w.oi }
    else if (oiChange > 8 && fundingRate < -0.0001)  { states.push(`OI creciendo con shorts (×${w.oi})`); score += w.oi }
    else if (oiChange < -8) states.push('Desapalancándose')
  } else if (oiChange != null && w.oi === 0) {
    if (oiChange > 8) states.push('OI creciendo (—)')
    else if (oiChange < -8) states.push('Desapalancándose (—)')
  }
  if (oiChange != null && priceChg24h != null && w.oiPrice > 0) {
    if (oiChange > 5 && priceChg24h > 2)       { states.push(`OI↑ + Precio↑ → nuevos longs (×${w.oiPrice})`); score += w.oiPrice }
    else if (oiChange > 5 && priceChg24h < -2)  { states.push(`OI↑ + Precio↓ → nuevos shorts (×${w.oiPrice})`); score -= w.oiPrice }
    else if (oiChange < -5 && priceChg24h < -2) states.push('OI↓ + Precio↓ → longs liquidándose')
    else if (oiChange < -5 && priceChg24h > 2)  states.push('OI↓ + Precio↑ → shorts liquidándose')
  }

  // ── 3. Taker flow (peso: w.taker) ──
  if (takerRatio != null && w.taker > 0) {
    if (takerRatio > 1.15)      { states.push(`Flujo comprador (×${w.taker})`); score += w.taker }
    else if (takerRatio > 1.05) { states.push(`Sesgo comprador (×${Math.ceil(w.taker/2)})`); score += Math.ceil(w.taker / 2) }
    else if (takerRatio < 0.85) { states.push(`Flujo vendedor (×${w.taker})`);  score -= w.taker }
    else if (takerRatio < 0.95) { states.push(`Sesgo vendedor (×${Math.ceil(w.taker/2)})`);  score -= Math.ceil(w.taker / 2) }
  } else if (takerRatio != null && w.taker === 0) {
    if (takerRatio > 1.15) states.push('Flujo comprador (—)')
    else if (takerRatio < 0.85) states.push('Flujo vendedor (—)')
  }

  // ── 4. Divergencia retail/smart money (peso: w.ls) ──
  if (retailRatio != null && topRatio != null && w.ls > 0) {
    if (retailRatio > 1.4 && topRatio < 1.15)       { states.push(`Retail long / Smart money neutra (×${w.ls})`); score -= w.ls }
    else if (retailRatio < 0.7 && topRatio > 0.85)   { states.push(`Retail short / Smart money neutra (×${w.ls})`); score += w.ls }
  }

  // ── 5. Volatilidad (peso: w.vol) — amplifica convicción, no da dirección ──
  const volPct   = vol.percentile
  const volRatio = vol.ratio
  if (volPct != null) {
    if (volPct <= 20)      states.push(`Vol P${volPct.toFixed(0)} muy baja — expansión probable`)
    else if (volPct >= 80) states.push(`Vol P${volPct.toFixed(0)} muy alta — movimiento en curso`)
  }
  if (volRatio != null) {
    if (volRatio < 0.7)      states.push('Vol comprimida')
    else if (volRatio > 1.5) states.push('Vol expandiéndose')
  }
  // Vol comprimida amplifica el score existente (convicción mayor)
  if (w.vol > 0 && volRatio != null && volRatio < 0.7 && Math.abs(score) >= 1) {
    const amp = Math.sign(score) * w.vol
    score += amp
    states.push(`Vol comprimida amplifica score ${amp > 0 ? '+' : ''}${amp}`)
  }

  // ── 6. IV vs RV (peso: w.ivRv) ──
  const ivRv = data.ivRvSpread
  if (ivRv && ivRv.spread != null) {
    if (ivRv.spread > 15) {
      states.push(`IV >> RV (+${ivRv.spread.toFixed(0)}%) — mercado espera movimiento`)
      // IV premium alto amplifica dirección existente
      if (w.ivRv > 0 && Math.abs(score) >= 1) {
        const amp = Math.sign(score) * w.ivRv
        score += amp
        states.push(`IV premium amplifica score ${amp > 0 ? '+' : ''}${amp}`)
      }
    } else if (ivRv.spread < -10) {
      states.push(`IV << RV (${ivRv.spread.toFixed(0)}%) — opciones baratas`)
    }
  }

  // ── 7. ETH/BTC (peso: w.ethBtc) ──
  const ethBtc = data.ethBtc
  if (ethBtc?.change24h != null && w.ethBtc > 0) {
    if (ethBtc.change24h > 2)       { states.push(`ETH/BTC +${ethBtc.change24h.toFixed(1)}% outperforming (×${w.ethBtc})`); score += w.ethBtc }
    else if (ethBtc.change24h < -2)  { states.push(`ETH/BTC ${ethBtc.change24h.toFixed(1)}% underperforming (×${w.ethBtc})`); score -= w.ethBtc }
  } else if (ethBtc?.change24h != null && w.ethBtc === 0) {
    if (ethBtc.change24h > 2) states.push(`ETH/BTC +${ethBtc.change24h.toFixed(1)}% (—)`)
    else if (ethBtc.change24h < -2) states.push(`ETH/BTC ${ethBtc.change24h.toFixed(1)}% (—)`)
  }

  // ── 8. Opciones — gamma regime (peso: w.gamma) ──
  const aboveFlip = opts.flipPosition === 'above'
  const belowFlip = opts.flipPosition === 'below'
  if (opts.gammaFlip != null) {
    if (aboveFlip) {
      states.push(`Sobre gamma flip $${Number(opts.gammaFlip).toFixed(0)} — combustible alcista`)
      if (w.gamma > 0) score += w.gamma
    } else {
      states.push(`Bajo gamma flip $${Number(opts.gammaFlip).toFixed(0)} — combustible bajista`)
      if (w.gamma > 0) score -= w.gamma
    }
  }
  if (opts.callWall != null && opts.callWallDist != null && opts.callWallDist < 3)
    states.push(`Call wall $${Number(opts.callWall).toFixed(0)} (${opts.callWallDist.toFixed(1)}% arriba)`)
  if (opts.putWall != null && opts.putWallDist != null && Math.abs(opts.putWallDist) < 3)
    states.push(`Put wall $${Number(opts.putWall).toFixed(0)} (${Math.abs(opts.putWallDist).toFixed(1)}% abajo)`)
  if (opts.maxPain != null && opts.maxPainDist != null && Math.abs(opts.maxPainDist) < 4)
    states.push(`Max pain $${Number(opts.maxPain).toFixed(0)} (${opts.nearestExpiry}) — imán de precio`)

  // ── 9. Volume Profile (peso: w.vp, period-aware) ──
  const vpPeriodMap = { '5m': '4h', '15m': '12h', '1h': '24h', '4h': '7d', '12h': '7d', '1d': '30d', '15d': '45d' }
  const vpPeriodKey = vpPeriodMap[period] || null
  const activeVp = vpPeriodKey ? (data.volumeProfileByPeriod?.[vpPeriodKey] || vp) : vp
  if (activeVp.pricePosition) {
    const pocStr = activeVp.poc?.price != null ? ` $${Number(activeVp.poc.price).toFixed(0)}` : ''
    const vpLabel = vpPeriodKey || '8d'
    if (activeVp.pricePosition === 'at_poc') {
      states.push(`En POC${pocStr} (${vpLabel}) — congestión`)
      // POC = neutral, no score
    } else if (activeVp.pricePosition === 'above_va') {
      states.push(`Arriba del VA (${vpLabel}) — sobreextensión alcista`)
      if (w.vp > 0) score -= w.vp  // mean reversion bias
    } else if (activeVp.pricePosition === 'below_va') {
      states.push(`Debajo del VA (${vpLabel}) — sobreextensión bajista`)
      if (w.vp > 0) score += w.vp  // mean reversion bias
    } else if (activeVp.pricePosition === 'in_va') {
      states.push(`Dentro del VA (${vpLabel})`)
    }
  }

  // ── 10. Stochastics (peso: w.stoch, TF elegido por usuario) ──
  // Slow stoch (400,40,10) = sesgo de régimen; Fast stoch (100,10,4) = timing
  // OB (>80) = agotamiento alcista → bearish bias
  // OS (<20) = agotamiento bajista → bullish bias
  // Cross %K sobre %D en zona extrema = confirmación de vuelta
  const stochByTf = data.stochastics || {}
  const stochCurrent = stochByTf[stochTf] || null
  if (stochCurrent && w.stoch > 0) {
    const slow = stochCurrent.slow
    const fast = stochCurrent.fast
    // Slow stoch = régimen
    if (slow && slow.k != null && slow.d != null) {
      if (slow.k >= 80 && slow.d >= 80) {
        states.push(`Slow Stoch ${stochTf} OB (${slow.k.toFixed(0)}/${slow.d.toFixed(0)}) (×${w.stoch})`)
        score -= w.stoch
      } else if (slow.k <= 20 && slow.d <= 20) {
        states.push(`Slow Stoch ${stochTf} OS (${slow.k.toFixed(0)}/${slow.d.toFixed(0)}) (×${w.stoch})`)
        score += w.stoch
      } else if (slow.k >= 70) {
        states.push(`Slow Stoch ${stochTf} cerca OB (${slow.k.toFixed(0)})`)
      } else if (slow.k <= 30) {
        states.push(`Slow Stoch ${stochTf} cerca OS (${slow.k.toFixed(0)})`)
      }
    }
    // Fast stoch = timing confirmation
    if (fast && fast.k != null && fast.d != null && slow && slow.k != null) {
      // Bullish cross in OS zone = timing de rebote
      if (fast.k > fast.d && fast.k < 30 && slow.k < 40) {
        states.push(`Fast Stoch cruzando al alza en OS — timing rebote (×${Math.ceil(w.stoch / 2)})`)
        score += Math.ceil(w.stoch / 2)
      }
      // Bearish cross in OB zone = timing de techo
      else if (fast.k < fast.d && fast.k > 70 && slow.k > 60) {
        states.push(`Fast Stoch cruzando a la baja en OB — timing techo (×${Math.ceil(w.stoch / 2)})`)
        score -= Math.ceil(w.stoch / 2)
      }
    }
  }

  // ── 11. Money Quality: plata nueva vs short covering (peso: w.mq) ──
  const mq = data.moneyQuality || {}
  const mqByWindow = mq.byWindow || {}
  // Map period to the most relevant MQ window (now supports multi-day windows)
  const mqPeriodMap = { '5m': '1h', '15m': '4h', '1h': '12h', '4h': '24h', '12h': '3d', '1d': '7d', '15d': '14d' }
  const mqKey = mqPeriodMap[period] || '4h'
  const mqInfo = mqByWindow[mqKey]
  if (mqInfo && w.mq > 0) {
    const qMult = { high: 1.0, medium: 0.5, low: 0.25 }[mqInfo.quality] || 0.25
    const points = Math.max(1, Math.round(w.mq * qMult))
    if (mqInfo.direction === 'bullish') {
      states.push(`${mqInfo.label} ${mqKey} (×${points})`)
      score += points
    } else if (mqInfo.direction === 'bearish') {
      states.push(`${mqInfo.label} ${mqKey} (×${points})`)
      score -= points
    }
    // Divergence warning: price moving one way but quality is low (no new money)
    if (mqInfo.quality === 'low' && Math.abs(mqInfo.priceChgPct) > 0.5) {
      if (mqInfo.direction === 'bullish') {
        states.push(`⚠ Rally sin combustible ${mqKey} (covering)`)
      } else if (mqInfo.direction === 'bearish') {
        states.push(`⚠ Caída sin combustible ${mqKey} (cierre)`)
      }
    }
  }
  // Verdict contextual (sin score, solo informativo si score ya es neutro)
  if (mq.verdict && Math.abs(score) <= 1) {
    if (mq.verdict.startsWith('ALCISTA') && w.mq > 0) {
      states.push(`Plata nueva bullish (${mq.verdict})`)
      score += 1
    } else if (mq.verdict.startsWith('BAJISTA') && w.mq > 0) {
      states.push(`Plata nueva bearish (${mq.verdict})`)
      score -= 1
    }
  }

  // ── 12. SETUP DETECTOR: stoch alignment + cut-anchored MQ filter (peso: w.setup) ──
  // Stoch alineado en extremo + fast %K cruza umbral.
  // PRIMARY filter: cut-anchored MQ (impulso desde el corte). Mide |ΔP|/|ΔOI|
  //   desde el bar exacto en que Fast %K (100,10) entró a la zona OB/OS.
  //   block → BLOQUEADO. upgrade-high → A++. upgrade-mid → A+. neutral → A.
  // SECONDARY (informativo): dual regime MQ (no gatea, solo contexto).
  if (w.setup > 0 && data.stochastics) {
    const windows = STOCH_TO_MQ_WINDOWS[stochTf] || { fast: '4h', slow: '24h' }
    const mqFastForSetup = mqByWindow[windows.fast]
    const mqSlowForSetup = mqByWindow[windows.slow]
    const cutInfoForSetup = (data.cutAnchoredMq || {})[stochTf]
    const setupAnalysis = analyzeSetup(data.stochastics[stochTf], mqFastForSetup, mqSlowForSetup, cutInfoForSetup)
    if (setupAnalysis && setupAnalysis.direction && setupAnalysis.state !== 'INACTIVE') {
      const qMult = {
        'A++':     1.5,
        'A+':      1.25,
        'A':       1.0,
        'BLOCKED': 0,
      }[setupAnalysis.quality] || 0
      const stateMult = {
        'TRIGGERED': 1.0,
        'ARMED':     0.6,   // esperando gatillo, peso reducido
        'LATE':      0.2,
      }[setupAnalysis.state] || 0
      const basePoints = w.setup * qMult * stateMult
      const points = Math.round(basePoints)

      if (setupAnalysis.quality === 'BLOCKED') {
        states.push(`Setup ${setupAnalysis.direction === 'short' ? 'SHORT' : 'LONG'} ${stochTf} BLOQUEADO — ${setupAnalysis.blockedReason}`)
      } else if (points > 0) {
        const tag = setupAnalysis.quality
        const st = setupAnalysis.state === 'TRIGGERED' ? 'DISPARADO' : setupAnalysis.state === 'ARMED' ? 'ARMADO' : 'TARDE'
        if (setupAnalysis.direction === 'short') {
          states.push(`Setup SHORT ${tag} ${st} ${stochTf} (×${points})`)
          score -= points
        } else if (setupAnalysis.direction === 'long') {
          states.push(`Setup LONG ${tag} ${st} ${stochTf} (×${points})`)
          score += points
        }
      }
    }
  }

  // ── Conclusión ──
  const volFavorable    = volPct != null && volPct <= 30
  const volDesfavorable = volPct != null && volPct >= 75
  const atPoc = activeVp.pricePosition === 'at_poc'

  // Clamp score to [-10, +10]
  score = Math.max(-10, Math.min(10, score))

  let combustible = '', stateLevel = 'neutral'
  let subNotes = []

  if (oiChange != null && oiChange < -8 && Math.abs(score) <= 1) {
    combustible = 'Mercado limpiándose — esperar nuevo ciclo de posicionamiento'
    stateLevel = 'neutral'
  } else if (score >= 3) {
    stateLevel = 'bullish'
    combustible = volFavorable
      ? 'Combustible para SUBA + vol baja = condiciones ideales para long'
      : volDesfavorable
        ? 'Combustible para SUBA pero vol alta = movimiento puede estar agotándose'
        : 'Combustible para SUBA'
  } else if (score <= -3) {
    stateLevel = 'bearish'
    combustible = volFavorable
      ? 'Combustible para CAÍDA + vol baja = condiciones ideales para short'
      : volDesfavorable
        ? 'Combustible para CAÍDA pero vol alta = movimiento puede estar agotándose'
        : 'Combustible para CAÍDA'
  } else if (score === 2) {
    stateLevel = 'caution'
    combustible = 'Sesgo alcista moderado — confirmar antes de entrar'
  } else if (score === -2) {
    stateLevel = 'caution'
    combustible = 'Sesgo bajista moderado — confirmar antes de entrar'
  } else if (Math.abs(score) === 1) {
    combustible = 'Sesgo leve, sin combustible fuerte — reducir tamaño'
    stateLevel = 'caution'
  } else {
    combustible = 'Sin desequilibrio, sin combustible claro'
  }

  if (atPoc && Math.abs(score) <= 2) {
    subNotes.push('Precio en POC = congestión, esperar ruptura')
  }
  // Overlay de opciones
  if (belowFlip && score <= -3) {
    subNotes.push('Bajo gamma flip → puts dominan, dealers venden spot → cascada bajista')
  } else if (belowFlip && score >= 3) {
    subNotes.push('Bajo gamma flip → subida contra combustible de puts, resistencia por hedging')
  } else if (aboveFlip && score >= 3) {
    subNotes.push('Sobre gamma flip → calls dominan, dealers compran spot → impulso alcista')
  } else if (aboveFlip && score <= -3) {
    subNotes.push('Sobre gamma flip → caída contra combustible de calls, soporte por hedging')
  }
  if (opts.maxPain != null && opts.maxPainDist != null && Math.abs(opts.maxPainDist) < 2) {
    subNotes.push(`Max pain $${Number(opts.maxPain).toFixed(0)} muy cerca → fuerza gravitacional hacia vencimiento`)
  }

  // Regime change detection: contradictions between indicators
  const regimeWarnings = []
  const fundBearish = fundingRate != null && fundingRate > 0.0002
  const fundBullish = fundingRate != null && fundingRate < -0.0002
  const takerBullish = takerRatio != null && takerRatio > 1.1
  const takerBearish = takerRatio != null && takerRatio < 0.9
  // Funding loaded long but takers buying = exhaustion risk
  if (fundBearish && takerBullish) regimeWarnings.push('Funding cargado long + takers comprando — combustible para corrección, posible techo')
  // Funding loaded short but takers selling = capitulation risk
  if (fundBullish && takerBearish) regimeWarnings.push('Funding cargado short + takers vendiendo — combustible para rebote, posible piso')
  // OI dropping fast with vol spike = liquidation cascade
  if (oiChange != null && oiChange < -10 && vol.percentile != null && vol.percentile > 70) {
    regimeWarnings.push('OI cayendo + vol alta — liquidación en cascada en curso')
  }
  // Vol compressed + OI rising = coil ready to spring
  if (vol.ratio != null && vol.ratio < 0.7 && oiChange != null && oiChange > 5) {
    regimeWarnings.push('Vol comprimida + OI creciendo — spring cargado, movimiento explosivo inminente')
  }

  return { funding, oi, taker, divergence, marketState: { level: stateLevel, factors: states, conclusion: combustible, subNotes, score, regimeWarnings } }
}

// ── Main Dashboard ───────────────────────────────────────────────────
export default function Dashboard({ data, depth, depthHistory, error, lastUpdate }) {
  const [statePeriod, setStatePeriod] = useState('1h')
  const [vpPeriod, setVpPeriod] = useState('8d')
  const [stochTf, setStochTf] = useState('1h')
  const signals = useMemo(() => computeSignals(data, statePeriod, stochTf), [data, statePeriod, stochTf])
  const bn = data?.binance || {}
  const okx = data?.okx || {}
  const bybit = data?.bybit || {}
  const hl = data?.hyperliquid || {}

  const timeToFunding = () => {
    const next = bn.funding?.nextTime
    if (!next) return '—'
    const diff = next - Date.now()
    if (diff < 0) return 'Ahora'
    return `${Math.floor(diff / 3600000)}h ${Math.floor((diff % 3600000) / 60000)}m`
  }

  return (
    <div style={{ maxWidth: 1280, margin: '0 auto', padding: '16px 12px' }}>

      {/* HEADER */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14, flexWrap: 'wrap', gap: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{ width: 8, height: 8, borderRadius: '50%', background: error ? '#ef4444' : '#22c55e', boxShadow: error ? '0 0 10px #ef4444' : '0 0 10px #22c55e' }} />
          <span style={{ fontSize: 22, fontWeight: 700, letterSpacing: -0.5 }}>ETH PERP</span>
          <span style={{ fontSize: 10, color: '#5a6a8a', background: '#111a35', padding: '3px 10px', borderRadius: 5, border: '1px solid #1a2544', ...S.mono }}>BINANCE · OKX · BYBIT · HL · DERIBIT</span>
        </div>
        <div style={{ fontSize: 10, color: '#3a4a6a', ...S.mono }}>
          {lastUpdate ? lastUpdate.toLocaleTimeString() : '...'} · 12s refresh
          {error && <span style={{ color: '#ef4444', marginLeft: 8 }}>⚠ {error}</span>}
        </div>
      </div>

      {/* PRICE */}
      <div style={{ ...S.card, marginBottom: 10, display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 14 }}>
          <span style={{ fontSize: 36, fontWeight: 700, ...S.mono, letterSpacing: -1 }}>${fmt(bn.price)}</span>
          <span style={{ fontSize: 15, fontWeight: 600, ...S.mono, color: (bn.priceChange24h || 0) >= 0 ? '#22c55e' : '#ef4444' }}>
            {(bn.priceChange24h || 0) >= 0 ? '+' : ''}{fmt(bn.priceChange24h)}%
          </span>
        </div>
        <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap' }}>
          {[['Mark', `$${fmt(bn.markPrice)}`], ['Vol 24h', bn.volume24h ? `$${(bn.volume24h / 1e9).toFixed(2)}B` : '—'], ['Next Funding', timeToFunding()]].map(([l, v]) => (
            <div key={l}><div style={S.label}>{l}</div><div style={{ ...S.mono, fontSize: 14, fontWeight: 600, color: '#c8d6e5' }}>{v}</div></div>
          ))}
        </div>
      </div>

      {/* MARKET STATE */}
      {signals.marketState && (() => {
        const ms = signals.marketState
        const levelColors = {
          bullish: { bg: '#062015', border: '#16a34a55', accent: '#4ade80', tag: 'ALCISTA', tagBg: '#16a34a22' },
          bearish: { bg: '#200a0a', border: '#dc262655', accent: '#f87171', tag: 'BAJISTA', tagBg: '#dc262622' },
          neutral: { bg: '#101828', border: '#33415555', accent: '#8a9ac0', tag: 'NEUTRAL', tagBg: '#33415522' },
          caution: { bg: '#1a1400', border: '#d9770655', accent: '#fbbf24', tag: 'PRECAUCIÓN', tagBg: '#d9770622' },
        }
        const c = levelColors[ms.level] || levelColors.neutral
        const scoreVal = ms.score || 0
        const scoreMeter = Math.min(Math.abs(scoreVal), 10)
        return (
          <div style={{ ...S.card, marginBottom: 10, padding: 0, overflow: 'hidden', border: `1px solid ${c.border}` }}>
            {/* Header */}
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '12px 18px',
              background: c.bg, borderBottom: `1px solid ${c.border}` }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <span style={{ ...S.sectionTitle, margin: 0, fontSize: 11 }}>Estado del mercado</span>
                <span style={{ padding: '3px 10px', borderRadius: 4, fontSize: 11, fontWeight: 700,
                  background: c.tagBg, color: c.accent, border: `1px solid ${c.border}`,
                  fontFamily: "'IBM Plex Mono', monospace" }}>{c.tag}</span>
                <div style={{ display: 'flex', gap: 3 }}>
                  {['5m', '15m', '1h', '4h', '12h', '1d', '15d'].map(p => (
                    <button key={p} onClick={() => setStatePeriod(p)} style={{
                      padding: '2px 7px', borderRadius: 4, fontSize: 9,
                      border: `1px solid ${statePeriod === p ? c.accent + '88' : '#1a2544'}`,
                      background: statePeriod === p ? c.bg : 'transparent',
                      color: statePeriod === p ? c.accent : '#4a5980',
                      cursor: 'pointer', fontFamily: "'IBM Plex Mono', monospace",
                      fontWeight: statePeriod === p ? 700 : 400,
                    }}>{p}</button>
                  ))}
                </div>
              </div>
              {/* Score meter — bar from -10 to +10 */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ fontSize: 9, color: '#4a5980', ...S.mono }}>Score</span>
                <div style={{ display: 'flex', gap: 1, alignItems: 'center' }}>
                  {/* Negative bars (left side, 5 bars) */}
                  {[...Array(5)].map((_, i) => {
                    const level = 5 - i  // 5,4,3,2,1
                    const filled = scoreVal < 0 && Math.abs(scoreVal) >= level
                    return (
                      <div key={`n${i}`} style={{ width: 5, height: 12 + (5 - i) * 1, borderRadius: 1,
                        background: filled ? '#ef4444' : '#1a2544',
                        opacity: filled ? 0.9 : 0.3, transition: 'all 0.3s' }} />
                    )
                  })}
                  {/* Center zero marker */}
                  <div style={{ width: 2, height: 18, background: '#334155', margin: '0 2px', borderRadius: 1 }} />
                  {/* Positive bars (right side, 5 bars) */}
                  {[...Array(5)].map((_, i) => {
                    const level = i + 1  // 1,2,3,4,5
                    const filled = scoreVal > 0 && scoreVal >= level
                    return (
                      <div key={`p${i}`} style={{ width: 5, height: 12 + i * 1, borderRadius: 1,
                        background: filled ? '#22c55e' : '#1a2544',
                        opacity: filled ? 0.9 : 0.3, transition: 'all 0.3s' }} />
                    )
                  })}
                </div>
                <span style={{ fontSize: 12, fontWeight: 700, color: scoreVal > 0 ? '#4ade80' : scoreVal < 0 ? '#f87171' : '#8a9ac0', ...S.mono, minWidth: 24, textAlign: 'right' }}>
                  {scoreVal > 0 ? '+' : ''}{scoreVal}
                </span>
              </div>
            </div>
            {/* Factors grid */}
            {ms.factors && ms.factors.length > 0 && (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, padding: '10px 18px',
                borderBottom: `1px solid ${c.border}` }}>
                {ms.factors.map((f, i) => {
                  // Color code each factor
                  let fColor = '#8a9ac0'
                  const fl = f.toLowerCase()
                  if (fl.includes('comprador') || fl.includes('alcista') || fl.includes('call heavy') || fl.includes('shorts cargados') || fl.includes('combustible alcista'))
                    fColor = '#4ade80'
                  else if (fl.includes('vendedor') || fl.includes('bajista') || fl.includes('put heavy') || fl.includes('longs cargados') || fl.includes('combustible bajista'))
                    fColor = '#f87171'
                  else if (fl.includes('divergencia') || fl.includes('sesgo') || fl.includes('vol ') || fl.includes('comprimida'))
                    fColor = '#fbbf24'
                  return (
                    <span key={i} style={{ padding: '3px 8px', borderRadius: 4, fontSize: 10,
                      background: '#0a1020', border: '1px solid #1a2544', color: fColor,
                      fontFamily: "'IBM Plex Mono', monospace", whiteSpace: 'nowrap' }}>
                      {f}
                    </span>
                  )
                })}
              </div>
            )}
            {/* Conclusion */}
            <div style={{ padding: '12px 18px', textAlign: 'center' }}>
              <div style={{ fontSize: 15, fontWeight: 700, color: c.accent, letterSpacing: 0.3,
                fontFamily: "'IBM Plex Sans', sans-serif" }}>
                {ms.conclusion}
              </div>
              {ms.subNotes && ms.subNotes.length > 0 && (
                <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 3, alignItems: 'center' }}>
                  {ms.subNotes.map((note, i) => (
                    <div key={i} style={{ fontSize: 11, color: '#8a9ac0', ...S.mono }}>
                      {note}
                    </div>
                  ))}
                </div>
              )}
              {ms.regimeWarnings && ms.regimeWarnings.length > 0 && (
                <div style={{ marginTop: 10, padding: '8px 14px', background: '#1a100033', borderRadius: 6, border: '1px solid #f59e0b33' }}>
                  {ms.regimeWarnings.map((w, i) => (
                    <div key={i} style={{ fontSize: 11, color: '#fbbf24', ...S.mono, marginBottom: 2 }}>
                      ⚡ {w}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )
      })()}

      {/* MONEY QUALITY — Plata Nueva vs Short Covering */}
      <div style={{ ...S.card, marginBottom: 10 }}>
        <MoneyQualityPanel moneyQuality={data?.moneyQuality} />
      </div>

      {/* CEX NETFLOWS — spot exchange pressure via Dune Analytics */}
      <div style={{ ...S.card, marginBottom: 10 }}>
        <CexNetflowsPanel cexNetflows={data?.cexNetflows} />
      </div>

      {/* SETUP DEL MOMENTO — stoch alignment + MQ filter */}
      <div style={{ ...S.card, marginBottom: 10 }}>
        <SetupPanel
          stochastics={data?.stochastics}
          moneyQuality={data?.moneyQuality}
          cutAnchoredMq={data?.cutAnchoredMq}
          stochTf={stochTf}
          setStochTf={setStochTf}
        />
      </div>

      {/* STOCHASTICS */}
      <div style={{ ...S.card, marginBottom: 10 }}>
        <div style={S.sectionTitle}>Osciladores Estocásticos · Slow (400,40,10) + Fast (100,10,4)</div>
        <StochasticPanel stochastics={data?.stochastics} timeframe={stochTf} setTimeframe={setStochTf} />
      </div>

      {/* GRID — 4 cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(270px, 1fr))', gap: 10 }}>

        {/* FUNDING */}
        <div style={S.card}>
          <div style={S.sectionTitle}>Funding Rate</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 6, marginBottom: 10 }}>
            {[
              { label: 'Binance', rate: bn.funding?.rate },
              { label: 'OKX', rate: okx.funding?.rate },
              { label: 'Bybit', rate: bybit.funding?.rate },
              { label: 'Hyperliquid', rate: hl.funding?.rate },
            ].map(({ label, rate }) => (
              <div key={label}>
                <div style={S.label}>{label}</div>
                <div style={{ ...S.value, fontSize: 13, color: (rate || 0) > 0.0001 ? '#22c55e' : (rate || 0) < -0.0001 ? '#ef4444' : '#8a9ac0' }}>
                  {rate != null ? pct(rate) : '—'}
                </div>
              </div>
            ))}
          </div>
          <div style={{ marginBottom: 8 }}>
            <div style={S.label}>Últimos 30 períodos (Binance)</div>
            <div style={{ marginTop: 4 }}>
              <Spark data={bn.funding?.history} color={(bn.funding?.rate || 0) >= 0 ? '#22c55e' : '#ef4444'} showZero valueKey="rate" />
            </div>
          </div>
          {signals.funding && <Signal level={signals.funding.level} text={signals.funding.text} />}
        </div>

        {/* OPEN INTEREST */}
        <div style={S.card}>
          <div style={S.sectionTitle}>Open Interest</div>
          {(() => {
            const price = bn.price || 1
            const oiData = [
              { label: 'Binance', usd: (bn.openInterest?.current || 0) * price },
              { label: 'OKX', usd: (okx.openInterest?.oiCcy || 0) * price },
              { label: 'Bybit', usd: bybit.openInterest?.oiValue || 0 },
              { label: 'Hyperliquid', usd: hl.openInterest || 0 },
            ]
            const totalOi = oiData.reduce((s, x) => s + x.usd, 0)
            return (
              <div style={{ marginBottom: 10 }}>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 6, marginBottom: 6 }}>
                  {oiData.map(({ label, usd }) => (
                    <div key={label}>
                      <div style={S.label}>{label}</div>
                      <div style={{ ...S.value, fontSize: 13 }}>{usd ? `$${(usd/1e9).toFixed(2)}B` : '—'}</div>
                    </div>
                  ))}
                </div>
                <div style={{ ...S.mono, fontSize: 9, color: '#4a5a7a' }}>
                  Total: ${(totalOi/1e9).toFixed(2)}B USD
                </div>
              </div>
            )
          })()}
          <div style={{ marginBottom: 8 }}>
            <div style={S.label}>OI Value 48h (Binance)</div>
            <div style={{ marginTop: 4 }}>
              <Spark data={bn.openInterest?.history} color="#38bdf8" valueKey="value" />
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
            <div style={S.label}>Cambio 48h</div>
            <span style={{ ...S.mono, fontSize: 16, fontWeight: 700, color: (bn.openInterest?.change48h || 0) > 5 ? '#f59e0b' : (bn.openInterest?.change48h || 0) < -5 ? '#38bdf8' : '#5a6a8a' }}>
              {bn.openInterest?.change48h != null ? `${bn.openInterest.change48h > 0 ? '+' : ''}${bn.openInterest.change48h.toFixed(1)}%` : '—'}
            </span>
          </div>
          {signals.oi && <Signal level={signals.oi.level} text={signals.oi.text} />}
        </div>

        {/* LONG/SHORT + DIVERGENCIA */}
        <div style={S.card}>
          <div style={S.sectionTitle}>Long / Short Ratio</div>
          <LongShortPanel longShort={bn.longShort} signal={signals.divergence} />
        </div>

        {/* TAKER */}
        <div style={S.card}>
          <div style={S.sectionTitle}>Taker Buy / Sell Volume</div>

          {/* Perp */}
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 2 }}>
            <div style={{ ...S.label, color: '#a78bfa' }}>PERP — Especulación apalancada</div>
            <div style={{ ...S.mono, fontSize: 9, color: '#3a4a6a' }}>período 1h</div>
          </div>
          <Gauge value={bn.takerBuySell?.ratio || 1} min={0.6} max={1.4} label="Perp Buy/Sell Ratio (1h)" thresholds={{ low: 0.85, mid: 1.05, high: 1.15 }} />
          {/* Interpretación perp */}
          {bn.takerBuySell?.ratio != null && (() => {
            const r = bn.takerBuySell.ratio
            let txt, clr
            if (r > 1.3)      { txt = `${r.toFixed(2)} → compradores agresivos dominan futuros — longs abriendo posiciones`; clr = '#22c55e' }
            else if (r > 1.1) { txt = `${r.toFixed(2)} → más buyers que sellers en perp — sesgo long`; clr = '#86efac' }
            else if (r > 0.9) { txt = `${r.toFixed(2)} → flujo perp equilibrado`; clr = '#8a9ac0' }
            else if (r > 0.7) { txt = `${r.toFixed(2)} → más sellers que buyers en perp — sesgo short`; clr = '#fca5a5' }
            else              { txt = `${r.toFixed(2)} → vendedores agresivos dominan futuros — shorts abriendo posiciones`; clr = '#ef4444' }
            return <div style={{ ...S.mono, fontSize: 10, color: clr, marginBottom: 8, lineHeight: 1.5 }}>{txt}</div>
          })()}
          <div style={{ marginBottom: 10 }}>
            <div style={S.label}>Perp ratio 4h (5min)</div>
            <div style={{ marginTop: 4 }}>
              <Spark data={bn.takerBuySell?.history} color="#a78bfa" showZero valueKey="ratio" />
            </div>
          </div>

          {/* Spot */}
          <div style={{ borderTop: '1px solid #1a2544', paddingTop: 10, marginBottom: 6 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 4 }}>
              <div style={{ ...S.label, color: '#fbbf24' }}>SPOT — Presión real de mercado</div>
              <div style={{ ...S.mono, fontSize: 9, color: '#3a4a6a' }}>período 1h</div>
            </div>
            <div style={{ display: 'flex', gap: 20, marginBottom: 6 }}>
              <div>
                <div style={S.label}>Ratio actual</div>
                <div style={{
                  ...S.mono, fontSize: 18, fontWeight: 700,
                  color: (bn.takerBuySell?.spotRatio || 1) > 1.05 ? '#22c55e' : (bn.takerBuySell?.spotRatio || 1) < 0.95 ? '#ef4444' : '#8a9ac0'
                }}>
                  {bn.takerBuySell?.spotRatio != null ? bn.takerBuySell.spotRatio.toFixed(3) : '—'}
                </div>
              </div>
              <div>
                <div style={S.label}>Buy vol (1h)</div>
                <div style={{ ...S.mono, fontSize: 13, color: '#22c55e' }}>
                  {bn.takerBuySell?.spotBuy != null ? `${(bn.takerBuySell.spotBuy / 1000).toFixed(1)}K ETH` : '—'}
                </div>
              </div>
              <div>
                <div style={S.label}>Sell vol (1h)</div>
                <div style={{ ...S.mono, fontSize: 13, color: '#ef4444' }}>
                  {bn.takerBuySell?.spotSell != null ? `${(bn.takerBuySell.spotSell / 1000).toFixed(1)}K ETH` : '—'}
                </div>
              </div>
              <div>
                <div style={S.label}>Delta (1h)</div>
                <div style={{ ...S.mono, fontSize: 13, color: (bn.takerBuySell?.spotDelta || 0) >= 0 ? '#22c55e' : '#ef4444' }}>
                  {bn.takerBuySell?.spotDelta != null ? `${bn.takerBuySell.spotDelta > 0 ? '+' : ''}${(bn.takerBuySell.spotDelta / 1000).toFixed(1)}K` : '—'}
                </div>
              </div>
            </div>
            {/* Interpretación spot */}
            {bn.takerBuySell?.spotRatio != null && (() => {
              const r = bn.takerBuySell.spotRatio
              let txt, clr
              if (r > 1.3)      { txt = `${r.toFixed(3)} → compradores reales dominan el mercado spot`; clr = '#22c55e' }
              else if (r > 1.05) { txt = `${r.toFixed(3)} → más compra real que venta en spot`; clr = '#86efac' }
              else if (r > 0.95) { txt = `${r.toFixed(3)} → flujo spot equilibrado`; clr = '#8a9ac0' }
              else if (r > 0.7)  { txt = `${r.toFixed(3)} → más venta real que compra en spot`; clr = '#fca5a5' }
              else               { txt = `${r.toFixed(3)} → vendedores reales dominan el mercado spot`; clr = '#ef4444' }
              return <div style={{ ...S.mono, fontSize: 10, color: clr, marginBottom: 8, lineHeight: 1.5 }}>{txt}</div>
            })()}
            <div style={S.label}>Spot ratio histórico — candles 5min (4h)</div>
            <div style={{ marginTop: 4 }}>
              <Spark data={bn.takerBuySell?.spotHistory5m} color="#fbbf24" showZero valueKey="ratio" />
            </div>
          </div>

          {/* Divergencia perp vs spot */}
          {bn.takerBuySell?.ratio != null && bn.takerBuySell?.spotRatio != null && (() => {
            const perpR = bn.takerBuySell.ratio
            const spotR = bn.takerBuySell.spotRatio
            const perpBull = perpR > 1.05, perpBear = perpR < 0.95
            const spotBull = spotR > 1.05, spotBear = spotR < 0.95
            if ((perpBull && spotBear) || (perpBear && spotBull)) {
              return (
                <div style={{ marginTop: 8, padding: '6px 10px', background: '#1a1400', border: '1px solid #d9770655', borderRadius: 6, fontSize: 11, color: '#fbbf24' }}>
                  ⚠ Divergencia perp/spot: perp {perpBull ? 'comprando' : 'vendiendo'}, spot {spotBull ? 'comprando' : 'vendiendo'} — señal mixta, reducir exposición
                </div>
              )
            }
            if (perpBull && spotBull) return <div style={{ marginTop: 8, padding: '6px 10px', background: '#062015', border: '1px solid #16a34a55', borderRadius: 6, fontSize: 11, color: '#4ade80' }}>✓ Perp y spot alineados alcistas — flujo confirmado</div>
            if (perpBear && spotBear) return <div style={{ marginTop: 8, padding: '6px 10px', background: '#200a0a', border: '1px solid #dc262655', borderRadius: 6, fontSize: 11, color: '#f87171' }}>✓ Perp y spot alineados bajistas — presión confirmada</div>
            return null
          })()}

          <div style={{ marginTop: 10 }}>
            {signals.taker && <Signal level={signals.taker.level} text={signals.taker.text} />}
          </div>
        </div>
      </div>

      {/* OPTIONS — Multi-exchange GEX */}
      <div style={{ ...S.card, marginTop: 10 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 12 }}>
          <div style={S.sectionTitle}>Opciones ETH — Deribit + Bybit + OKX</div>
          <div style={{ ...S.mono, fontSize: 9, color: '#3a4a6a' }}>GEX · Gamma Flip · Call/Put Walls · Max Pain · 60 días</div>
        </div>
        <OptionsPanel options={data?.options} marketVolume={data?.marketVolume} />
      </div>

      {/* LIQUIDATION MAP */}
      <div style={{ ...S.card, marginTop: 10 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 12 }}>
          <div style={S.sectionTitle}>Mapa de Liquidaciones Estimado</div>
          <div style={{ ...S.mono, fontSize: 9, color: '#3a4a6a' }}>Binance + OKX + Bybit + Hyperliquid</div>
        </div>
        <LiquidationMap liqMap={data?.liquidationMap} />
      </div>

      {/* IV TERM STRUCTURE + IV vs RV + ETH/BTC + FUNDING SPREAD */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 10, marginTop: 10 }}>
        {/* IV Term Structure */}
        <div style={S.card}>
          <div style={S.sectionTitle}>IV Term Structure</div>
          <IvTermStructure data={data?.ivTermStructure} />
        </div>

        {/* IV vs RV Spread */}
        <div style={S.card}>
          <div style={S.sectionTitle}>IV vs RV Spread</div>
          {data?.ivRvSpread?.spread != null ? (() => {
            const ivRv = data.ivRvSpread
            const spreadColor = ivRv.spread > 10 ? '#f59e0b' : ivRv.spread < -5 ? '#22c55e' : '#8a9ac0'
            const regime = ivRv.spread > 15 ? 'IV Premium alto — mercado paga caro por protección, espera movimiento'
              : ivRv.spread > 5 ? 'IV ligeramente sobre RV — normal'
              : ivRv.spread < -10 ? 'IV Discount — opciones baratas vs vol real, oportunidad de compra vol'
              : ivRv.spread < 0 ? 'IV bajo RV — posible complacencia'
              : 'IV ≈ RV — equilibrio'
            return (
              <div>
                <div style={{ display: 'flex', gap: 20, marginBottom: 12, flexWrap: 'wrap' }}>
                  <div>
                    <div style={S.label}>IV ({ivRv.dte}d ATM)</div>
                    <div style={{ ...S.mono, fontSize: 18, fontWeight: 700, color: '#a78bfa' }}>{ivRv.impliedVol?.toFixed(1)}%</div>
                  </div>
                  <div>
                    <div style={S.label}>RV 24h</div>
                    <div style={{ ...S.mono, fontSize: 18, fontWeight: 700, color: '#38bdf8' }}>{ivRv.realizedVol?.toFixed(1)}%</div>
                  </div>
                  <div>
                    <div style={S.label}>Spread</div>
                    <div style={{ ...S.mono, fontSize: 18, fontWeight: 700, color: spreadColor }}>
                      {ivRv.spread > 0 ? '+' : ''}{ivRv.spread?.toFixed(1)}%
                    </div>
                  </div>
                  <div>
                    <div style={S.label}>Ratio IV/RV</div>
                    <div style={{ ...S.mono, fontSize: 18, fontWeight: 700, color: ivRv.ratio > 1.2 ? '#f59e0b' : ivRv.ratio < 0.9 ? '#22c55e' : '#8a9ac0' }}>
                      {ivRv.ratio?.toFixed(2)}x
                    </div>
                  </div>
                </div>
                <Signal level={ivRv.spread > 10 ? 'caution' : ivRv.spread < -5 ? 'bullish' : 'neutral'} text={regime} />
              </div>
            )
          })() : <div style={{ color: '#4a5980', fontSize: 12 }}>Calculando IV vs RV...</div>}
        </div>
      </div>

      {/* ETH/BTC + FUNDING SPREAD */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 10, marginTop: 10 }}>
        {/* ETH/BTC Relative Strength */}
        <div style={S.card}>
          <div style={S.sectionTitle}>ETH/BTC Relative Strength</div>
          {data?.ethBtc?.price != null ? (() => {
            const eb = data.ethBtc
            const chgColor = (eb.change24h || 0) >= 0 ? '#22c55e' : '#ef4444'
            const regime = eb.change24h > 3 ? 'ETH outperforming BTC significativamente — capital rotando a ETH'
              : eb.change24h > 1 ? 'ETH ligeramente más fuerte que BTC'
              : eb.change24h < -3 ? 'ETH underperforming BTC — capital saliendo de ETH'
              : eb.change24h < -1 ? 'ETH ligeramente más débil que BTC'
              : 'ETH/BTC estable — sin rotación clara'
            return (
              <div>
                <div style={{ display: 'flex', gap: 20, marginBottom: 12, flexWrap: 'wrap' }}>
                  <div>
                    <div style={S.label}>ETH/BTC</div>
                    <div style={{ ...S.mono, fontSize: 20, fontWeight: 700, color: '#e2e8f0' }}>{eb.price?.toFixed(6)}</div>
                  </div>
                  <div>
                    <div style={S.label}>Cambio 24h</div>
                    <div style={{ ...S.mono, fontSize: 20, fontWeight: 700, color: chgColor }}>
                      {eb.change24h >= 0 ? '+' : ''}{eb.change24h?.toFixed(2)}%
                    </div>
                  </div>
                  <div>
                    <div style={S.label}>Rango 24h</div>
                    <div style={{ ...S.mono, fontSize: 12, color: '#6a7aa0' }}>
                      {eb.low24h?.toFixed(6)} — {eb.high24h?.toFixed(6)}
                    </div>
                  </div>
                </div>
                <Signal level={eb.change24h > 2 ? 'bullish' : eb.change24h < -2 ? 'bearish' : 'neutral'} text={regime} />
              </div>
            )
          })() : <div style={{ color: '#4a5980', fontSize: 12 }}>Cargando ETH/BTC...</div>}
        </div>

        {/* Funding Spread */}
        <div style={S.card}>
          <div style={S.sectionTitle}>Funding Spread entre Exchanges</div>
          {data?.fundingSpread?.rates ? (() => {
            const fs = data.fundingSpread
            const spreadAlert = fs.maxSpread > 0.005
            return (
              <div>
                <div style={{ display: 'flex', gap: 12, marginBottom: 12, flexWrap: 'wrap' }}>
                  {Object.entries(fs.rates).map(([ex, rate]) => {
                    const isMax = ex === fs.maxExchange
                    const isMin = ex === fs.minExchange
                    return (
                      <div key={ex} style={{ padding: '6px 10px', background: isMax ? '#1a180008' : isMin ? '#081a1008' : '#0a1020', borderRadius: 6, border: `1px solid ${isMax ? '#f59e0b33' : isMin ? '#22c55e33' : '#1a2544'}` }}>
                        <div style={{ ...S.label, fontSize: 9 }}>{ex} {isMax ? '(max)' : isMin ? '(min)' : ''}</div>
                        <div style={{ ...S.mono, fontSize: 14, fontWeight: 700, color: rate > 0.01 ? '#22c55e' : rate < -0.01 ? '#ef4444' : '#8a9ac0' }}>
                          {rate > 0 ? '+' : ''}{rate.toFixed(4)}%
                        </div>
                      </div>
                    )
                  })}
                </div>
                <div style={{ display: 'flex', gap: 16, marginBottom: 8 }}>
                  <div>
                    <div style={S.label}>Max Spread</div>
                    <div style={{ ...S.mono, fontSize: 14, fontWeight: 700, color: spreadAlert ? '#f59e0b' : '#8a9ac0' }}>
                      {fs.maxSpread?.toFixed(4)}%
                    </div>
                  </div>
                  <div>
                    <div style={S.label}>Media</div>
                    <div style={{ ...S.mono, fontSize: 14, fontWeight: 700, color: '#8a9ac0' }}>{fs.mean?.toFixed(4)}%</div>
                  </div>
                </div>
                {spreadAlert && (
                  <Signal level="caution" text={`Spread funding alto: ${fs.maxExchange} (${fs.rates[fs.maxExchange]?.toFixed(4)}%) vs ${fs.minExchange} (${fs.rates[fs.minExchange]?.toFixed(4)}%) — posible arb oportunity`} />
                )}
                {!spreadAlert && (
                  <Signal level="neutral" text="Funding spread entre exchanges normal — sin divergencia significativa" />
                )}
              </div>
            )
          })() : <div style={{ color: '#4a5980', fontSize: 12 }}>Calculando funding spread...</div>}
        </div>
      </div>

      {/* KEY LEVELS PANEL */}
      <div style={{ ...S.card, marginTop: 10 }}>
        <div style={S.sectionTitle}>Niveles Clave Consolidados — Opciones + Volume Profile + Order Book</div>
        <KeyLevelsPanel data={data ? { ...data, _depth: depth } : null} />
      </div>

      {/* VOLATILITY + VOLUME PROFILE SUMMARY */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(350px, 1fr))', gap: 10, marginTop: 10 }}>
        <div style={S.card}>
          <div style={S.sectionTitle}>Volatilidad Realizada</div>
          <VolatilityPanel volatility={data?.volatility} />
        </div>
        <div style={S.card}>
          <div style={S.sectionTitle}>Volume Profile — Niveles Clave</div>
          <VolumeProfileSummary
            vp={data?.volumeProfile}
            vpByPeriod={data?.volumeProfileByPeriod}
            period={vpPeriod}
            setPeriod={setVpPeriod}
          />
        </div>
      </div>

      {/* TAKER FLOW — cumulative delta by period */}
      <div style={{ ...S.card, marginTop: 10 }}>
        <div style={S.sectionTitle}>Flujo Acumulado Taker — Buy vs Sell por período</div>
        <TakerFlow flow={bn.takerBuySell?.flow} />
      </div>

      {/* ORDER BOOK DEPTH */}
      <div style={{ ...S.card, marginTop: 10 }}>
        <div style={S.sectionTitle}>Order Book — Liquidez y Paredes (±3% del precio)</div>
        <DepthHeatmap depth={depth} depthHistory={depthHistory} />
      </div>

      {/* VOLUME PROFILE */}
      <div style={{ ...S.card, marginTop: 10 }}>
        <div style={S.sectionTitle}>Volume Profile ±10%</div>
        <VolumeProfile
          data={bn.volumeProfile}
          dataByPeriod={bn.volumeProfileByPeriod}
          currentPrice={bn.price}
          period={vpPeriod}
          setPeriod={setVpPeriod}
          pocInfo={data?.volumeProfile}
          pocByPeriod={data?.volumeProfileByPeriod}
        />
      </div>

      {/* INDICATOR EXPLANATIONS */}
      <div style={{ ...S.card, marginTop: 10 }}>
        <div style={S.sectionTitle}>¿Qué significa cada indicador?</div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 20, fontSize: 12, color: '#6a7aa0', lineHeight: 1.7 }}>
          <div>
            <div style={{ color: '#e2e8f0', fontWeight: 700, marginBottom: 4 }}>📊 FUNDING RATE</div>
            <div style={{ fontSize: 11, color: '#4a5980' }}>
              Comisión periódica entre longs y shorts. <span style={{ color: '#22c55e' }}>Positivo</span>: longs pagan → mercado cargado long, vulnerable a caída. <span style={{ color: '#ef4444' }}>Negativo</span>: shorts pagan → cargado short, vulnerable a suba.
            </div>
          </div>
          <div>
            <div style={{ color: '#e2e8f0', fontWeight: 700, marginBottom: 4 }}>📈 OPEN INTEREST</div>
            <div style={{ fontSize: 11, color: '#4a5980' }}>
              Total de contratos abiertos = cuánto apalancamiento hay. <span style={{ color: '#f59e0b' }}>Subiendo</span>: más posiciones, mercado más frágil. <span style={{ color: '#38bdf8' }}>Bajando</span>: desapalancamiento, señales erráticas.
            </div>
          </div>
          <div>
            <div style={{ color: '#e2e8f0', fontWeight: 700, marginBottom: 4 }}>⚖️ LONG/SHORT RATIO</div>
            <div style={{ fontSize: 11, color: '#4a5980' }}>
              Proporción de cuentas long vs short. <span style={{ color: '#f59e0b' }}>Divergencia retail/smart money</span>: cuando retail está muy long pero top traders neutros, el precio históricamente va contra el retail.
            </div>
          </div>
          <div>
            <div style={{ color: '#e2e8f0', fontWeight: 700, marginBottom: 4 }}>🔥 TAKER BUY/SELL</div>
            <div style={{ fontSize: 11, color: '#4a5980' }}>
              Ratio compras agresivas vs ventas agresivas. <span style={{ color: '#22c55e' }}>{'>'} 1.15</span>: compra dominante. <span style={{ color: '#ef4444' }}>{'<'} 0.85</span>: venta dominante. Si va contra tu señal, esperá a que se agote.
            </div>
          </div>
          <div>
            <div style={{ color: '#e2e8f0', fontWeight: 700, marginBottom: 4 }}>📖 ORDER BOOK</div>
            <div style={{ fontSize: 11, color: '#4a5980' }}>
              Dónde están las órdenes grandes. <span style={{ color: '#22c55e' }}>Bid walls</span>: soporte real. <span style={{ color: '#ef4444' }}>Ask walls</span>: resistencia real. Si una pared desaparece, ese nivel se debilitó. El imbalance muestra si hay más liquidez de un lado.
            </div>
          </div>
          <div>
            <div style={{ color: '#e2e8f0', fontWeight: 700, marginBottom: 4 }}>📉 VOLATILIDAD REALIZADA</div>
            <div style={{ fontSize: 11, color: '#4a5980' }}>
              Cuánto se mueve el precio, anualizada. <span style={{ color: '#e2e8f0' }}>Ratio 4h/24h</span>: si la vol de 4h es menor que la de 24h (ratio &lt; 0.8), la volatilidad se comprimió — suele preceder un movimiento grande. <span style={{ color: '#22c55e' }}>P20 o menos</span>: vol muy baja, buena oportunidad — si tu estocástico da señal, el movimiento puede ser grande. <span style={{ color: '#ef4444' }}>P80 o más</span>: vol alta, el movimiento gordo ya pasó, más riesgo.
            </div>
          </div>
          <div>
            <div style={{ color: '#e2e8f0', fontWeight: 700, marginBottom: 4 }}>📊 VOLUME PROFILE</div>
            <div style={{ fontSize: 11, color: '#4a5980' }}>
              A qué precios se operó más volumen. <span style={{ color: '#fbbf24' }}>POC</span>: precio de máximo volumen = precio "justo", actúa como imán. <span style={{ color: '#22c55e' }}>VAH/VAL</span>: límites de la zona donde se operó el 70% del volumen. <span style={{ color: '#fbbf24' }}>HVN</span>: nodos de alto volumen = soporte/resistencia fuerte. <span style={{ color: '#38bdf8' }}>LVN</span>: nodos de bajo volumen = zonas de paso rápido, el precio las cruza fácil. Precio fuera del Value Area → mayor probabilidad de reversión al POC.
            </div>
          </div>
          <div>
            <div style={{ color: '#e2e8f0', fontWeight: 700, marginBottom: 4 }}>💡 CICLO DE APALANCAMIENTO</div>
            <div style={{ fontSize: 11, color: '#4a5980' }}>
              1. <span style={{ color: '#22c55e' }}>Acumulación</span>: OI sube + funding se carga.
              2. <span style={{ color: '#f59e0b' }}>Inflexión</span>: estocástico gira + máximo combustible → mejor entrada.
              3. <span style={{ color: '#ef4444' }}>Liquidación</span>: OI cae + funding normaliza → movimiento explosivo.
              4. <span style={{ color: '#38bdf8' }}>Limpieza</span>: OI bajo + neutral → esperar nuevo ciclo.
            </div>
          </div>
        </div>
      </div>

      <div style={{ textAlign: 'center', marginTop: 14, fontSize: 9, color: '#2a3555', letterSpacing: 0.5 }}>
        ETH POSITIONING DASHBOARD v3 · BINANCE + OKX + BYBIT + HYPERLIQUID + DERIBIT · NO ES ASESORAMIENTO FINANCIERO
      </div>
    </div>
  )
}
