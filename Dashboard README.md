# Handoff: AlertSol Dashboard — Mobile & Desktop

## Overview

Redesign of the AlertSol trading dashboard — a real-time monitoring tool for SOL/USDT algorithmic trading setups. The design covers both a **mobile app view** (bottom tab navigation) and a **desktop web view** (sidebar navigation), with a consistent dark theme optimized for data readability.

## About the Design Files

The files in this bundle are **HTML design prototypes** — high-fidelity mockups showing intended look, layout, and interactive behavior. They are **not production code**. The task is to **recreate these designs in your target codebase** using its existing framework, component library, and conventions (e.g. React, Next.js, Vue). If no framework exists yet, React + TypeScript is recommended.

## Fidelity

**High-fidelity.** The prototypes use final colors, typography, spacing, and interactions. Recreate the UI pixel-precisely using the token values listed in the Design Tokens section.

---

## Screens / Views

### 1. Mobile — Dashboard Tab (`Dashboard Mobile.html`)

**Layout:** Full-screen mobile view (max-width 390px), flex column.

#### Topbar
- Background: `#0d1520`, border-bottom: `1px solid #1a2840`
- Row 1: brand label (left) + date/time (right)
  - Brand: `IBM Plex Sans`, 11px, weight 600, uppercase, letter-spacing 0.12em, color `#8a9bbf`
  - Date: `IBM Plex Mono`, 11px, color `#8a9bbf`
- Row 2: price (left) + regime badge + pair (right)
  - Price: `IBM Plex Mono`, 32px, weight 600, color `#e8edf5`
  - Change: `IBM Plex Mono`, 13px, color `#ff4d6a`
  - Regime badge TREND↓: background `#ff4d6a18`, color `#ff4d6a`, border `1px solid #ff4d6a44`, border-radius 3px, padding 3px 9px, font 11px weight 700 uppercase

#### Stats Strip
- Background: `#121c2e`, border-bottom: `1px solid #1a2840`
- 5 equal cells: Balans / 3m P&L / 5d P&L / Win Rate / Aktywne
- Cell: padding 10px 8px, centered, border-right `1px solid #1a2840`
- Value: `IBM Plex Mono` 14px weight 600; Label: 9px uppercase letter-spacing 0.06em color `#6b7fa3`
- Colors: neutral `#8a9bbf`, positive `#00d68f`, negative `#ff4d6a`, warning `#f5a623`

#### Alert Banner
- Full-width, no side margins, background `#f5a62318`, border-top/bottom `1px solid #f5a62344`
- Font: `IBM Plex Mono`, 11px, color `#f5a623`, padding 9px 12px, line-height 1.5

#### Chart Area
- Full-width, no side margins, background `#0d1520`, border-top/bottom `1px solid #1a2840`
- Height: 150px. Shows candlestick SVG chart (SOL/USDT 1h)
- Green candles `#00d68f`, red candles `#ff4d6a`
- Price line: `#4a9eff` dashed

#### Active Setups (cards)
- Full-width cards, no side margins, border-bottom `1px solid #1a2840`, background `#0d1520`
- Card top (padding 10px 12px): ID (Mono 13px 600) + LONG/SHORT badge + status pill
  - LONG badge: bg `#00d68f18`, color `#00d68f`, border `1px solid #00d68f33`
  - SHORT badge: bg `#ff4d6a18`, color `#ff4d6a`, border `1px solid #ff4d6a33`
  - Status "czeka": bg `#f5a62318`, color `#f5a623`; "short": bg `#ff4d6a18`, color `#ff4d6a`
  - Status dot: 6px circle, color matches status
- Card bottom (padding 8px 12px): stat columns (W / TPS / SL / action button)
  - Stat value: Mono 13px 600; label: 9px uppercase color `#6b7fa3`
  - Dividers: `1px solid #1a2840` between columns
  - Action button LONG: bg `#00d68f18`, color `#00d68f`; SHORT: bg `#ff4d6a18`, color `#ff4d6a`

#### Bottom Navigation
- Background: `#0d1520`, border-top `1px solid #1a2840`
- 4 tabs: Dashboard / Setups / Algo / Historia
- SVG icons 22×22px; inactive opacity 0.35; active color `#4a9eff`
- Label: 9px weight 600 uppercase; active color `#4a9eff`
- Orange dot (active setups indicator) on Setups tab when not active

---

### 2. Mobile — Setups Tab

Full list of active setups. Same card design as Dashboard tab but shows all 4 cards with model name + full action buttons.

### 3. Mobile — Algo Tab

Segment control (1m / 3m / 6m+) + filter chips (wariant / per data / per model).

**Tables:**
- Full-width, rows with padding 10px 16px, border-bottom `1px solid #1a2840`
- Column header: bg `#121c2e`, color `#6b7fa3`, 10px uppercase
- Row hover: bg `#121c2e`
- P&L values: positive `#00d68f`, negative `#ff4d6a`

### 4. Mobile — Historia Tab

Filter chips + list of closed trade cards.

**Trade card:**
- Full-width, no side margins, padding 10px 12px, border-bottom `1px solid #1a2840`
- Row 1: LONG/SHORT badge + trade ID + date (right)
- Row 2: strategy type name (11px color `#8a9bbf`)
- Row 3: WE / TP / Wynik (TPS/SL) / Model — Mono 11px color `#6b7fa3`

---

### 5. Desktop — Dashboard View (`Dashboard Desktop.html`)

**Layout:** Sidebar (220px fixed) + main area (flex 1), full viewport height, overflow hidden.

#### Sidebar (`#0d1520`, border-right `1px solid #1a2840`)
- **Logo block** (padding 20px): "AlertSol" 13px weight 700 uppercase letter-spacing 0.14em
- **Price block** (padding 16px): price 26px Mono weight 600 + change + pair + regime badge
- **Nav items** (padding 9px 20px): icon 16×16 + label 13px weight 500, color `#8a9bbf`
  - Active: bg `#4a9eff18`, color `#4a9eff`, border-left `2px solid #4a9eff`
  - Hover: bg `#121c2e`
  - Nav badge (setup count): bg `#f5a62318`, color `#f5a623`, border `1px solid #f5a62333`, Mono 10px, border-radius 8px
- **Bottom**: date Mono 11px color `#6b7fa3`

#### Top Stats Bar (height 58px, `#0d1520`, border-bottom `1px solid #1a2840`)
- Horizontal stat cells: Balans / 3m P&L / 5d P&L / Win Rate / Aktywne
  - Value: Mono 16px weight 600; label: 10px uppercase color `#6b7fa3`
  - Divider: `1px solid #1a2840` between cells
- Alert banner (margin-left auto): bg `#f5a62318`, border `1px solid #f5a62333`, Mono 11px color `#f5a623`, max-width 420px, truncated

#### Content Area (padding 20px 24px, gap 20px, overflow-y auto)
- **Row 1:** Chart panel (flex 1) + Quick Stats panel (340px)
- **Row 2:** Full-width panel (Setups table)

**Panel:**
- Background `#0d1520`, border `1px solid #1a2840`, border-radius 8px
- Panel head: bg `#121c2e`, padding 12px 16px, border-bottom `1px solid #1a2840`
  - Title: 11px weight 700 uppercase letter-spacing 0.1em color `#8a9bbf`

**Data Tables:**
- `th`: bg `#121c2e`, color `#6b7fa3`, 10px uppercase letter-spacing 0.06em, padding 8px 12px
- `td`: padding 9px 12px, border-bottom `1px solid #1a2840`, font 12px
- Row hover: `td` bg `#121c2e`

---

## Interactions & Behavior

- **Tab switching** (mobile bottom nav / desktop sidebar): instant render of new view, no animation needed
- **Segment controls** (Algo tab periods): active tab gets highlighted background
- **Filter chips** (Historia): single-select, filters the trades list
- **Action buttons**: "wejście" — triggers order entry modal (not designed, TBD); "setup" — opens setup detail view (not designed, TBD)
- **Collapsible sections**: not used in final design (scrapped from Variant B)
- **Chart**: static in prototype; in production replace with TradingView widget or Recharts

---

## State Management

| State | Type | Description |
|-------|------|-------------|
| `activeTab` | `'dash' \| 'setups' \| 'algo' \| 'hist'` | Current bottom tab / sidebar item |
| `algoPeriod` | `'1m' \| '3m' \| '6m+'` | Selected time period in Algo view |
| `algoSubTab` | `'wariant' \| 'per data' \| 'per model'` | Selected sub-view in Algo view |
| `histFilter` | `'wszystkie' \| 'long' \| 'short' \| 'tps' \| 'sl'` | Filter for Historia view |

Data is currently hardcoded in prototypes. In production, fetch from API and refresh on interval (recommend 5s for price, 30s for setups).

---

## Design Tokens

### Colors
```
--bg0:      #080d14   /* page background */
--bg1:      #0d1520   /* panels, sidebar, topbar */
--bg2:      #121c2e   /* table headers, hover states */
--bg3:      #182336   /* active states, chips */
--border:   #1a2840   /* all dividers and borders */
--border2:  #223050   /* secondary borders */

--text1:    #e8edf5   /* primary text */
--text2:    #8a9bbf   /* secondary text */
--text3:    #6b7fa3   /* labels, metadata */

--green:    #00d68f   /* positive P&L, LONG, TPS */
--red:      #ff4d6a   /* negative P&L, SHORT, SL */
--amber:    #f5a623   /* warnings, alerts, active count */
--blue:     #4a9eff   /* active nav, price line, links */

/* Dimmed variants (backgrounds) */
--green-dim: #00d68f18
--red-dim:   #ff4d6a18
--amber-dim: #f5a62318
--blue-dim:  #4a9eff18
```

### Typography
```
Font stack:
  Headings/UI:  'IBM Plex Sans', sans-serif
  Numbers/Code: 'IBM Plex Mono', monospace

Scale:
  9px  — micro labels, nav labels
  10px — table headers, small metadata
  11px — secondary body, alerts, badges
  12px — table rows, card body
  13px — nav items, card titles
  14px — stat strip values
  16px — desktop stat bar values
  18px — (mobile price change)
  26px — desktop sidebar price
  32px — mobile topbar price
```

### Spacing
```
Micro:    4px
Small:    8px
Base:     12px
Medium:   16px
Large:    20px
XLarge:   24px
```

### Border Radius
```
Badge/button:  3px
Pill/chip:     10–12px
Card/panel:    8px (desktop only; mobile cards are full-width, no radius)
Segment tab:   5px outer, 3–4px inner
```

---

## Assets

- **No external images** — chart is SVG-rendered
- **Icons** — custom inline SVG (16×16 or 22×22), described in README; recreate or replace with icon library (e.g. Lucide, Phosphor)
- **Fonts** — Google Fonts: IBM Plex Sans + IBM Plex Mono (weights 400/500/600/700)

---

## Files in This Package

| File | Description |
|------|-------------|
| `Dashboard Mobile.html` | Hi-fi mobile prototype (390px, bottom tab nav) |
| `Dashboard Desktop.html` | Hi-fi desktop prototype (full viewport, sidebar nav) |
| `README.md` | This handoff document |

---

## Notes for Developer

1. **Responsive breakpoint**: switch from mobile to desktop layout at `768px` or `1024px` — your call based on target device matrix.
2. **Chart**: Replace the SVG placeholder with a real charting library. TradingView Lightweight Charts is recommended for crypto data.
3. **Real-time data**: Price, regime, and setups should poll or subscribe via WebSocket. Show a "stale" indicator if data is older than 60s.
4. **Polish language**: All copy in the UI is in Polish — keep it as-is.
5. **Scrollable tables on mobile**: The Algo and Historia tables may overflow on small screens — use `overflow-x: auto` on table wrappers.
