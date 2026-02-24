"""Coinbase Design System (CDS) theme for the Streamlit UI.

Injects CSS that maps CDS design tokens onto Streamlit's rendered HTML.
Call inject_theme() once, immediately after st.set_page_config().

Design tokens sourced from:
  github.com/coinbase/cds  —  packages/web/src/themes/coinbaseTheme.ts
  Font: Inter (CDS uses Inter as its public-facing sans-serif stack)
"""

import streamlit as st


# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------
_TOKENS = """
  /* ── Color ──────────────────────────────────────────────────────────── */
  --cds-blue:           #0052FF;
  --cds-blue-hover:     #0041CC;
  --cds-blue-active:    #0033A3;
  --cds-blue-subtle:    rgba(0, 82, 255, 0.06);
  --cds-blue-wash:      rgba(0, 82, 255, 0.10);

  --cds-bg:             #FFFFFF;
  --cds-bg-alt:         #F8F9FA;
  --cds-bg-elevated:    #FFFFFF;

  --cds-text:           #0A0B0D;
  --cds-text-muted:     #5B616E;
  --cds-text-subtle:    #8A919E;

  --cds-border:         rgba(10, 11, 13, 0.10);
  --cds-border-strong:  rgba(10, 11, 13, 0.20);

  /* Semantic — positive / green */
  --cds-green:          #00D180;
  --cds-green-text:     #06794D;
  --cds-green-bg:       rgba(0, 209, 128, 0.08);

  /* Semantic — warning / yellow */
  --cds-yellow:         #FFC801;
  --cds-yellow-text:    #7A5400;
  --cds-yellow-bg:      rgba(255, 200, 1, 0.10);

  /* Semantic — negative / red */
  --cds-red:            #CF202F;
  --cds-red-text:       #CF202F;
  --cds-red-bg:         rgba(207, 32, 47, 0.08);

  /* Semantic — caution / orange */
  --cds-orange:         #FF7900;
  --cds-orange-bg:      rgba(255, 121, 0, 0.08);

  /* ── Typography ─────────────────────────────────────────────────────── */
  --cds-font:       'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI',
                    Helvetica, Arial, sans-serif;
  --cds-font-mono:  'Menlo', 'Consolas', 'Monaco', monospace;

  /* ── Radius ─────────────────────────────────────────────────────────── */
  --cds-r-xs:   4px;
  --cds-r-sm:   8px;
  --cds-r-md:   12px;
  --cds-r-lg:   16px;
  --cds-r-pill: 100px;

  /* ── Shadow / Elevation ──────────────────────────────────────────────── */
  --cds-shadow-1: 0 2px 8px  rgba(0, 0, 0, 0.08);
  --cds-shadow-2: 0 8px 24px rgba(0, 0, 0, 0.12);

  /* ── Spacing (8px base) ──────────────────────────────────────────────── */
  --cds-sp-1: 8px;
  --cds-sp-2: 16px;
  --cds-sp-3: 24px;
  --cds-sp-4: 32px;
"""


# ---------------------------------------------------------------------------
# Full stylesheet
# ---------------------------------------------------------------------------
_CSS = """
<style>
/* ═══════════════════════════════════════════════════════════════════════════
   COINBASE DESIGN SYSTEM — STREAMLIT SKIN
   Tokens: github.com/coinbase/cds  /  packages/web/src/themes/coinbaseTheme.ts
   Font:   Inter (Google Fonts, CDS public stack)
   ═══════════════════════════════════════════════════════════════════════════ */

/* ── Google Fonts ──────────────────────────────────────────────────────── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

/* ── Token root ────────────────────────────────────────────────────────── */
:root {
  _TOKENS_
}

/* ── Base ──────────────────────────────────────────────────────────────── */
html, body, [data-testid="stAppViewContainer"],
[data-testid="stApp"] {
  font-family: var(--cds-font) !important;
  background-color: var(--cds-bg) !important;
  color: var(--cds-text) !important;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

/* ── Main content area ─────────────────────────────────────────────────── */
[data-testid="block-container"] {
  padding-top: var(--cds-sp-3) !important;
  max-width: 1200px;
}

/* ── Sidebar ───────────────────────────────────────────────────────────── */
[data-testid="stSidebar"] {
  background-color: var(--cds-bg-alt) !important;
  border-right: 1px solid var(--cds-border) !important;
}
[data-testid="stSidebarContent"] {
  padding: var(--cds-sp-3) var(--cds-sp-2) !important;
}

/* Sidebar — page title */
[data-testid="stSidebar"] .stTitle,
[data-testid="stSidebar"] h1 {
  font-size: 1.125rem !important;
  font-weight: 600 !important;
  letter-spacing: -0.01em !important;
  color: var(--cds-text) !important;
}
/* Sidebar — section headers */
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3 {
  font-size: 0.6875rem !important;
  font-weight: 600 !important;
  text-transform: uppercase !important;
  letter-spacing: 0.08em !important;
  color: var(--cds-text-subtle) !important;
  margin-top: var(--cds-sp-3) !important;
  margin-bottom: var(--cds-sp-1) !important;
}

/* ── Typography ────────────────────────────────────────────────────────── */
h1 {
  font-family: var(--cds-font) !important;
  font-size: 2rem !important;
  font-weight: 600 !important;
  letter-spacing: -0.02em !important;
  color: var(--cds-text) !important;
  line-height: 1.2 !important;
}
h2 {
  font-family: var(--cds-font) !important;
  font-size: 1.375rem !important;
  font-weight: 600 !important;
  letter-spacing: -0.015em !important;
  color: var(--cds-text) !important;
  line-height: 1.3 !important;
}
h3 {
  font-family: var(--cds-font) !important;
  font-size: 1.0625rem !important;
  font-weight: 600 !important;
  letter-spacing: -0.01em !important;
  color: var(--cds-text) !important;
  line-height: 1.4 !important;
}
h4, h5, h6 {
  font-family: var(--cds-font) !important;
  font-weight: 600 !important;
  color: var(--cds-text) !important;
}
p, li, td, th, label {
  font-family: var(--cds-font) !important;
}

/* ── Captions ──────────────────────────────────────────────────────────── */
[data-testid="stCaptionContainer"],
.stCaptionContainer,
small {
  color: var(--cds-text-muted) !important;
  font-size: 0.8125rem !important;
  line-height: 1.55 !important;
}

/* ── Divider ───────────────────────────────────────────────────────────── */
hr {
  border: none !important;
  border-top: 1px solid var(--cds-border) !important;
  margin: var(--cds-sp-3) 0 !important;
}

/* ── Buttons ───────────────────────────────────────────────────────────── */
.stButton > button,
.stDownloadButton > button,
.stFormSubmitButton > button {
  background-color: var(--cds-blue) !important;
  color: #FFFFFF !important;
  border: none !important;
  border-radius: var(--cds-r-md) !important;
  font-family: var(--cds-font) !important;
  font-weight: 600 !important;
  font-size: 0.9375rem !important;
  padding: 10px 20px !important;
  letter-spacing: 0 !important;
  transition: background-color 0.15s ease, box-shadow 0.15s ease,
              transform 0.1s ease !important;
  cursor: pointer !important;
}
.stButton > button:hover,
.stDownloadButton > button:hover,
.stFormSubmitButton > button:hover {
  background-color: var(--cds-blue-hover) !important;
  box-shadow: var(--cds-shadow-1) !important;
}
.stButton > button:active,
.stDownloadButton > button:active {
  background-color: var(--cds-blue-active) !important;
  transform: translateY(1px) !important;
}
/* Secondary / outline style */
.stButton > button[kind="secondary"],
.stButton > button[data-testid*="secondary"] {
  background-color: transparent !important;
  color: var(--cds-blue) !important;
  border: 1.5px solid var(--cds-blue) !important;
}
.stButton > button[kind="secondary"]:hover {
  background-color: var(--cds-blue-subtle) !important;
}
/* Dismiss / minimal buttons */
.stButton > button[kind="minimal"] {
  background-color: transparent !important;
  color: var(--cds-text-muted) !important;
  border: 1px solid var(--cds-border-strong) !important;
}

/* ── Metric cards ──────────────────────────────────────────────────────── */
[data-testid="metric-container"] {
  background-color: var(--cds-bg-elevated) !important;
  border: 1px solid var(--cds-border) !important;
  border-radius: var(--cds-r-md) !important;
  padding: var(--cds-sp-2) var(--cds-sp-2) var(--cds-sp-2) !important;
  box-shadow: var(--cds-shadow-1) !important;
}
[data-testid="stMetricLabel"] > div {
  font-size: 0.6875rem !important;
  font-weight: 600 !important;
  text-transform: uppercase !important;
  letter-spacing: 0.07em !important;
  color: var(--cds-text-subtle) !important;
}
[data-testid="stMetricValue"] > div {
  font-size: 1.75rem !important;
  font-weight: 600 !important;
  color: var(--cds-text) !important;
  letter-spacing: -0.02em !important;
  line-height: 1.2 !important;
}
[data-testid="stMetricDelta"] > div {
  font-size: 0.8125rem !important;
  font-weight: 500 !important;
}

/* ── Tabs ──────────────────────────────────────────────────────────────── */
[data-baseweb="tab-list"] {
  background-color: transparent !important;
  border-bottom: 1px solid var(--cds-border) !important;
  gap: 0 !important;
  padding: 0 !important;
}
[data-baseweb="tab"] {
  background-color: transparent !important;
  color: var(--cds-text-muted) !important;
  font-family: var(--cds-font) !important;
  font-size: 0.9375rem !important;
  font-weight: 500 !important;
  padding: 10px 16px !important;
  border-radius: 0 !important;
  border-bottom: 2px solid transparent !important;
  margin-bottom: -1px !important;
  transition: color 0.15s ease, border-color 0.15s ease !important;
}
[data-baseweb="tab"]:hover {
  color: var(--cds-text) !important;
  background-color: transparent !important;
}
[aria-selected="true"][data-baseweb="tab"] {
  color: var(--cds-blue) !important;
  border-bottom: 2px solid var(--cds-blue) !important;
  background-color: transparent !important;
  font-weight: 600 !important;
}
/* Tab highlight bar (Base Web renders this separately) */
[data-baseweb="tab-highlight"] {
  background-color: var(--cds-blue) !important;
  height: 2px !important;
}
[data-baseweb="tab-border"] {
  background-color: var(--cds-border) !important;
  height: 1px !important;
}

/* ── Alert / notification boxes ────────────────────────────────────────── */
[data-testid="stAlert"] {
  border-radius: var(--cds-r-sm) !important;
  padding: 12px 16px !important;
  font-size: 0.9375rem !important;
  line-height: 1.55 !important;
  border-left-width: 3px !important;
}
/* Info */
[data-testid="stAlert"] [data-baseweb="notification"][kind="info"],
[data-testid="stNotification"][data-type="info"],
div[class*="stInfo"],
div[data-testid="stAlertContentInfo"] {
  background-color: var(--cds-blue-subtle) !important;
  border-left-color: var(--cds-blue) !important;
}
/* Success */
[data-testid="stAlert"] [data-baseweb="notification"][kind="positive"],
div[class*="stSuccess"],
div[data-testid="stAlertContentSuccess"] {
  background-color: var(--cds-green-bg) !important;
  border-left-color: var(--cds-green) !important;
  color: var(--cds-green-text) !important;
}
/* Warning */
[data-testid="stAlert"] [data-baseweb="notification"][kind="warning"],
div[class*="stWarning"],
div[data-testid="stAlertContentWarning"] {
  background-color: var(--cds-yellow-bg) !important;
  border-left-color: var(--cds-yellow) !important;
  color: var(--cds-yellow-text) !important;
}
/* Error */
[data-testid="stAlert"] [data-baseweb="notification"][kind="negative"],
div[class*="stError"],
div[data-testid="stAlertContentError"] {
  background-color: var(--cds-red-bg) !important;
  border-left-color: var(--cds-red) !important;
  color: var(--cds-red-text) !important;
}

/* ── Text inputs ───────────────────────────────────────────────────────── */
[data-testid="stNumberInput"] input,
[data-testid="stTextInput"] input,
.stTextArea textarea,
[data-testid="stTextArea"] textarea {
  border: 1.5px solid var(--cds-border-strong) !important;
  border-radius: var(--cds-r-sm) !important;
  font-family: var(--cds-font) !important;
  font-size: 0.9375rem !important;
  background-color: var(--cds-bg) !important;
  color: var(--cds-text) !important;
  transition: border-color 0.15s ease, box-shadow 0.15s ease !important;
}
[data-testid="stNumberInput"] input:focus,
[data-testid="stTextInput"] input:focus,
.stTextArea textarea:focus,
[data-testid="stTextArea"] textarea:focus {
  border-color: var(--cds-blue) !important;
  box-shadow: 0 0 0 3px var(--cds-blue-subtle) !important;
  outline: none !important;
}

/* Input labels */
[data-testid="stNumberInput"] label,
[data-testid="stTextInput"] label,
[data-testid="stTextArea"] label,
[data-testid="stSelectbox"] label,
[data-testid="stMultiSelect"] label,
[data-testid="stSlider"] label {
  font-size: 0.8125rem !important;
  font-weight: 500 !important;
  color: var(--cds-text) !important;
  margin-bottom: 4px !important;
}

/* ── Select / dropdown ─────────────────────────────────────────────────── */
[data-testid="stSelectbox"] [data-baseweb="select"] > div:first-child,
[data-testid="stMultiSelect"] [data-baseweb="select"] > div:first-child {
  border: 1.5px solid var(--cds-border-strong) !important;
  border-radius: var(--cds-r-sm) !important;
  font-family: var(--cds-font) !important;
  font-size: 0.9375rem !important;
  background-color: var(--cds-bg) !important;
}

/* ── Toggle / switch ───────────────────────────────────────────────────── */
[data-testid="stToggle"] input:checked + div,
[role="switch"][aria-checked="true"] {
  background-color: var(--cds-blue) !important;
}

/* ── Checkbox ──────────────────────────────────────────────────────────── */
[data-testid="stCheckbox"] [data-baseweb="checkbox"] [data-checked="true"],
[data-testid="stCheckbox"] input:checked + span {
  background-color: var(--cds-blue) !important;
  border-color: var(--cds-blue) !important;
}

/* ── Slider ────────────────────────────────────────────────────────────── */
[data-testid="stSlider"] [data-baseweb="slider"] [role="slider"] {
  background-color: var(--cds-blue) !important;
  border-color: var(--cds-blue) !important;
}
[data-testid="stSlider"] div[class*="Track"] > div:first-child {
  background-color: var(--cds-blue) !important;
}

/* ── Progress bar ──────────────────────────────────────────────────────── */
[data-testid="stProgress"] > div {
  background-color: var(--cds-border) !important;
  border-radius: var(--cds-r-pill) !important;
  height: 6px !important;
}
[data-testid="stProgress"] > div > div {
  background-color: var(--cds-blue) !important;
  border-radius: var(--cds-r-pill) !important;
}

/* ── Expander ──────────────────────────────────────────────────────────── */
[data-testid="stExpander"] {
  border: 1px solid var(--cds-border) !important;
  border-radius: var(--cds-r-sm) !important;
  background-color: var(--cds-bg) !important;
  overflow: hidden !important;
}
[data-testid="stExpander"] summary,
[data-testid="stExpanderToggleIcon"] {
  font-weight: 500 !important;
  font-size: 0.9375rem !important;
  color: var(--cds-text) !important;
  padding: 12px 16px !important;
}
[data-testid="stExpander"] [data-testid="stExpanderDetails"] {
  padding: 0 16px 16px !important;
}

/* ── Data editor (table) ───────────────────────────────────────────────── */
[data-testid="stDataFrame"],
[data-testid="data_editor"] {
  border: 1px solid var(--cds-border) !important;
  border-radius: var(--cds-r-sm) !important;
  overflow: hidden !important;
}

/* ── Spinner ───────────────────────────────────────────────────────────── */
div[data-testid="stSpinner"] > div {
  border-top-color: var(--cds-blue) !important;
}

/* ── File uploader ─────────────────────────────────────────────────────── */
[data-testid="stFileUploader"] > section {
  border: 1.5px dashed var(--cds-border-strong) !important;
  border-radius: var(--cds-r-sm) !important;
  background-color: var(--cds-bg-alt) !important;
  transition: border-color 0.15s ease !important;
}
[data-testid="stFileUploader"] > section:hover {
  border-color: var(--cds-blue) !important;
  background-color: var(--cds-blue-subtle) !important;
}

/* ── Scrollbars ────────────────────────────────────────────────────────── */
::-webkit-scrollbar              { width: 6px; height: 6px; }
::-webkit-scrollbar-track        { background: transparent; }
::-webkit-scrollbar-thumb        { background: var(--cds-border-strong);
                                    border-radius: 3px; }
::-webkit-scrollbar-thumb:hover  { background: var(--cds-text-subtle); }

/* ── Success / info / warning colour for st.success() text ─────────────── */
.stSuccess { color: var(--cds-green-text) !important; }
.stWarning { color: var(--cds-yellow-text) !important; }
.stError   { color: var(--cds-red-text)    !important; }

</style>
"""

# Splice tokens into the CSS string once at import time
_INJECTED_CSS = _CSS.replace("_TOKENS_", _TOKENS)


def inject_theme() -> None:
    """Inject the Coinbase Design System stylesheet into the Streamlit app.

    Call once, immediately after st.set_page_config().
    """
    st.markdown(_INJECTED_CSS, unsafe_allow_html=True)
