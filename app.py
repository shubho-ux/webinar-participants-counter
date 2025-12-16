#app.py
import os
import uuid
import threading
from queue import Queue, Empty
from datetime import datetime, timedelta
from flask import (
    Flask, request, render_template_string, jsonify, send_from_directory, Response
)
import pandas as pd
from werkzeug.utils import secure_filename

# ---------------- CONFIG ----------------
UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
ALLOWED_EXTENSIONS = {"xlsx", "xls", "csv"}

# Default timeline and annotations (can be changed in Settings)
DEFAULT_TIMELINE = [
    "09:00", "09:15", "09:30", "09:45",
    "10:00", "10:15", "10:30", "10:45",
    "11:02", "11:12", "11:15", "11:30", "11:45",
    "12:00", "12:15", "12:21", "12:33", "12:35", "12:50", "12:51"
]

DEFAULT_ANNOTATIONS = {
    "11:02": "Break starts",
    "11:12": "Break ends",
    "12:21": "PACE Intro",
    "12:33": "PACE investment",
    "12:35": "Pitch starts",
    "12:50": "Pitch ends",
    "12:51": "Workshop ends"
}

CSV_PREFIX = "Webinar_Attendee_Counter_Report"
# ----------------------------------------

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB limit
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["OUTPUT_FOLDER"] = OUTPUT_FOLDER

# In-memory queues & results
TASK_QUEUES = {}
TASK_RESULTS = {}

# Global, mutable settings stored server-side (in-memory)
CURRENT_SETTINGS = {
    "timeline": list(DEFAULT_TIMELINE),
    "annotations": dict(DEFAULT_ANNOTATIONS)
}

# ---------- Helpers ----------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def push_log(task_id, message):
    q = TASK_QUEUES.get(task_id)
    if q:
        q.put(message)

def read_input_file(path):
    """Read Excel or CSV into DataFrame, preserving raw columns."""
    ext = path.rsplit(".", 1)[1].lower()
    if ext == "csv":
        # Let pandas infer; treat all as strings first
        df = pd.read_csv(path, dtype=str)
    else:
        df = pd.read_excel(path, engine="openpyxl", dtype=str)
    return df

def normalize_columns(df):
    """Normalize headings (strip/title) but preserve original columns names if needed."""
    df.columns = df.columns.str.strip().str.title()
    return df

# ---------- Processing (backend logic preserved) ----------
def process_file(task_id, filepath):
    try:
        push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] ✅ System: Processing {os.path.basename(filepath)}")
        # Load file
        try:
            df = read_input_file(filepath)
            push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] 📥 File loaded ({os.path.basename(filepath)})")
        except Exception as e:
            push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Error reading file: {e}")
            push_log(task_id, "FAILED")
            return

        # Normalize column names
        df = normalize_columns(df)
        push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] ✨ Columns normalized.")

        # Check for Join/Leave columns
        if 'Join Time' not in df.columns or 'Leave Time' not in df.columns:
            push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Missing 'Join Time' or 'Leave Time'.")
            push_log(task_id, "FAILED")
            return

        # Parse datetimes with dayfirst (keeps your original fix)
        push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ Parsing Join/Leave times (dayfirst=True)...")
        df['Join Time'] = pd.to_datetime(df['Join Time'], dayfirst=True, errors='coerce')
        df['Leave Time'] = pd.to_datetime(df['Leave Time'], dayfirst=True, errors='coerce')

        initial = len(df)
        df = df.dropna(subset=['Join Time', 'Leave Time'])
        push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] 🧹 Dropped {initial - len(df)} invalid rows.")

        # Deduplicate key: prefer Email, fallback to name columns or index
        if 'Email' in df.columns:
            df['clean_email'] = df['Email'].astype(str).str.lower().str.strip()
            push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] 🔑 Using 'Email' for dedupe.")
        else:
            fallback = None
            for c in ['Name', 'Name (Original Name)', 'Full Name']:
                if c in df.columns:
                    fallback = c
                    break
            if fallback:
                df['clean_email'] = df[fallback].astype(str).str.lower().str.strip()
                push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] 🔑 Using '{fallback}' as dedupe key.")
            else:
                df['clean_email'] = df.index.astype(str)
                push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] 🔑 No Email/Name — using row index.")

        # Event dates
        df['Event_Date'] = df['Join Time'].dt.date
        unique_dates = sorted(df['Event_Date'].unique())
        push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] 📅 Dates found: {', '.join(str(d) for d in unique_dates)}")

        if not unique_dates:
            push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] ❌ No valid dates found.")
            push_log(task_id, "FAILED")
            return

        # Use CURRENT_SETTINGS for timeline and annotations
        timeline = list(CURRENT_SETTINGS.get("timeline", DEFAULT_TIMELINE))
        annotations = dict(CURRENT_SETTINGS.get("annotations", DEFAULT_ANNOTATIONS))

        final_data = {"Time": timeline}
        date_to_counts = {}

        for report_date in unique_dates:
            push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] 🔎 Analyzing {report_date} ...")
            day_df = df[df['Event_Date'] == report_date]
            counts = []
            for time_str in timeline:
                # parse time
                t_val = datetime.strptime(time_str, "%H:%M").time()
                base_dt = datetime.combine(report_date, t_val)

                # Keep the first-minute fix: treat first timestamp as "end of that minute"
                if time_str == timeline[0]:
                    check_dt = pd.Timestamp(base_dt + timedelta(seconds=59))
                else:
                    check_dt = pd.Timestamp(base_dt)

                active_rows = day_df[(day_df['Join Time'] <= check_dt) & (day_df['Leave Time'] >= check_dt)]
                count = int(active_rows['clean_email'].nunique())
                if time_str in annotations:
                    text = f"{count} ({annotations[time_str]})"
                else:
                    text = str(count)
                counts.append(text)
                push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] [ {time_str} ] -> {text}")
            final_data[f"Count ({report_date})"] = counts
            date_to_counts[str(report_date)] = counts

        # Save CSV for the first date (same behavior)
        first_date = unique_dates[0]
        out_filename = f"{CSV_PREFIX}_{uuid.uuid4().hex[:8]}.csv"
        out_path = os.path.join(app.config["OUTPUT_FOLDER"], out_filename)
        csv_df = pd.DataFrame({
            "Time": timeline,
            f"Count ({first_date})": final_data[f"Count ({first_date})"]
        })
        csv_df.to_csv(out_path, index=False)
        push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Report saved to: {out_path}")

        # Save results for frontend display (rows)
        rows_for_ui = [[t, final_data[f"Count ({first_date})"][i]] for i, t in enumerate(timeline)]
        TASK_RESULTS[task_id] = {
            "csv": out_filename,
            "rows": rows_for_ui,
            "dates": [str(d) for d in unique_dates],
            "all_counts": date_to_counts
        }

        push_log(task_id, "DONE")
    except Exception as e:
        push_log(task_id, f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Unexpected: {e}")
        push_log(task_id, "FAILED")

# ---------- Routes ----------
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Webinar Attendee Counter</title>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <style>
    /* New light, clean design:
       - Removes black background / green accents
       - Uses Poppins for modern rounded look
       - Removes the status explanatory paragraph as requested
    */
    @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap');

    :root{
      --page-bg: #f6f8fb;
      --card-bg: #ffffff;
      --muted: #6b7280;
      --accent: #0b6ef6; /* primary blue accent */
      --accent-2: #ff7a59; /* secondary warm accent for small highlights */
      --border: rgba(12, 20, 35, 0.06);
      --shadow: 0 10px 30px rgba(12, 20, 35, 0.06);
      --pill-bg: rgba(11,110,246,0.08);
    }

    *{ box-sizing: border-box; }
    body{ margin:0; background: linear-gradient(180deg, var(--page-bg) 0%, #ffffff 100%); font-family:Poppins,Inter,system-ui,Arial; color:#0b1220; -webkit-font-smoothing:antialiased; -moz-osx-font-smoothing:grayscale; }
    .wrap{ max-width:1100px; margin:30px auto; padding:24px; }

    /* Header / hero */
    .hero{ display:flex; gap:20px; align-items:center; justify-content:space-between; margin-bottom:18px; }
    .brand{
      display:flex; gap:14px; align-items:center;
    }
    .logo{
      width:56px; height:56px; border-radius:12px; background:linear-gradient(135deg,var(--accent),#2aa7ff); display:flex; align-items:center; justify-content:center; color:#fff; font-weight:700; box-shadow: 0 8px 22px rgba(11,110,246,0.12);
    }
    .title-block .title{ font-size:22px; font-weight:700; margin:0; color:#071227; letter-spacing:0.2px; }
    .title-block .sub{ font-size:13px; color:var(--muted); margin-top:4px; }

    .controls{ display:flex; gap:12px; align-items:center; }

    /* Upload button (pill) */
    .btn{
      background: linear-gradient(90deg, var(--accent), #2aa7ff);
      color: #ffffff; border: none; padding:10px 16px; border-radius:999px; cursor:pointer; font-weight:700;
      display:inline-flex; gap:10px; align-items:center; box-shadow: var(--shadow);
    }
    .btn.alt{
      background: transparent; border:1px solid var(--border); color:var(--accent);
      padding:8px 14px; border-radius:10px; font-weight:600;
    }

    /* page grid */
    .grid{ display:grid; grid-template-columns: 1fr 420px; gap:18px; align-items:start; }
    @media (max-width: 980px){
      .grid{ grid-template-columns: 1fr; }
    }

    .card{ background:var(--card-bg); padding:16px; border-radius:12px; border:1px solid var(--border); box-shadow: var(--shadow); }

    /* Status card simplified (paragraph removed per request) */
    .status-row{ display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; }
    .status-pill{
      background: var(--pill-bg); padding:8px 12px; border-radius:999px; color:var(--accent); font-weight:700; border:1px solid rgba(11,110,246,0.12);
    }
    .status-label{ font-weight:700; color:#071227; }

    /* Results table */
    .table{ margin-top:8px; border-radius:10px; padding:10px; }
    table{ width:100%; border-collapse:collapse; }
    td{ padding:10px 12px; border-bottom:1px solid rgba(12,20,35,0.04); font-size:15px; color:#0b1220; }
    td.time{ color:#0b6ef6; font-weight:700; width:140px; }
    td.val{ color:#374151; font-weight:700; text-align:right; }

    .toolbar{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }
    .copybtn{ background:transparent; color:var(--accent); padding:8px 12px; border-radius:8px; border:1px solid var(--border); cursor:pointer; font-weight:700; display:inline-flex; gap:8px; align-items:center; }

    /* Download link */
    a.dl{ color:var(--accent); font-weight:700; text-decoration:none; }

    /* Settings modal (keeps same structure but light) */
    .modal-backdrop{ position:fixed; inset:0; background:rgba(12,20,35,0.35); display:none; align-items:center; justify-content:center; z-index:999; }
    .modal{ width:920px; max-width:94%; background:var(--card-bg); padding:18px; border-radius:12px; border:1px solid var(--border); box-shadow: 0 30px 80px rgba(12,20,35,0.12); }
    .modal h3{ margin:0 0 8px 0; color:#071227; font-size:18px; }
    .row{ display:flex; gap:12px; margin-bottom:8px; align-items:center; }
    input[type="text"], textarea{ width:100%; padding:8px 10px; border-radius:8px; border:1px solid rgba(12,20,35,0.06); background: #fbfdff; color:#071227; }
    .list-item{ display:flex; gap:8px; align-items:center; padding:8px; border-radius:8px; background:#fbfdff; border:1px solid rgba(12,20,35,0.04); margin-bottom:6px; }
    .list-item input[type="text"]{ background:transparent; border:none; outline:none; color:#071227; font-weight:700; }
    .remove-btn{ background:#fff; color:#ef4444; border:1px solid rgba(12,20,35,0.04); padding:6px 8px; border-radius:8px; cursor:pointer; font-weight:700; }

    .add-row{ display:flex; gap:8px; margin-top:8px; }

    .save { background: var(--accent); color: #ffffff; border: none; padding:10px 14px; border-radius:8px; cursor:pointer; font-weight:800; }
    .close-btn { background:transparent; color:var(--muted); border:1px solid var(--border); padding:10px 14px; border-radius:8px; cursor:pointer; font-weight:700; }

    footer{ text-align:center; color:var(--muted); margin-top:18px; font-size:13px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div class="brand">
        <div class="logo">WC</div>
        <div class="title-block">
          <div class="title">Webinar Attendee Counter</div>
          <div class="sub">Upload a Zoom attendance report (.xlsx/.xls/.csv). Use Settings (⚙️) to change timeline & annotations.</div>
        </div>
      </div>

      <div class="controls">
        <label class="btn" id="uploadBtn">📁 Upload & Start
          <input id="fileInput" type="file" accept=".xlsx,.xls,.csv" style="display:none"/>
        </label>
        <button class="btn alt" id="openSettings" title="Settings ⚙️">⚙️ Settings</button>
      </div>
    </div>

    <div class="grid">
      <!-- Left: main card (upload + results) -->
      <div>
        <div class="card">
          <div class="status-row">
            <div class="status-label">Status</div>
            <div class="status-pill" id="status">Idle</div>
          </div>
          <!-- explanatory paragraph removed per your request -->

          <div style="display:flex; justify-content:flex-end; margin-top:12px;">
            <div id="downloadZone"></div>
          </div>
        </div>

        <div class="card table" style="margin-top:14px;">
          <div class="toolbar">
            <div style="font-weight:800">Timeline Counts</div>
            <div style="display:flex; gap:8px; align-items:center;">
              <button id="copyBtn" class="copybtn" title="Copy both columns (no header)">
                <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" width="16" height="16" style="fill:currentColor;"><path d="M16 1H4c-1.1 0-2 .9-2 2v12h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>
                <span>Copy</span>
              </button>
              <a id="downloadLink" class="dl" style="display:none; margin-left:8px;">Download CSV</a>
            </div>
          </div>
          <table id="resultTable"></table>
          <div style="margin-top:8px; color:var(--muted); font-size:13px">No header — paste into Google Sheets to get Timestamp (col A) and Value (col B).</div>
        </div>
      </div>

      <!-- Right column: small card with quick actions / info -->
      <div>
        <div class="card" style="display:flex; flex-direction:column; gap:10px;">
          <div style="font-weight:700; font-size:15px;">Quick actions</div>
          <div style="display:flex; flex-direction:column; gap:8px;">
            <button id="copyAllBtn" class="btn alt">Copy All (TSV)</button>
            <button id="clearBtn" class="btn alt">Clear Results</button>
          </div>

          <div style="border-top:1px solid rgba(12,20,35,0.04); padding-top:10px;">
            <div style="font-weight:700; margin-bottom:6px;">Timeline preview</div>
            <div id="timelinePreview" style="color:var(--muted); font-size:13px; max-height:180px; overflow:auto;"></div>
          </div>
        </div>
      </div>
    </div>

    <footer>Part by Quantum Leap Solutions</footer>
  </div>

  <!-- Settings Modal -->
  <div class="modal-backdrop" id="modalBackdrop">
    <div class="modal" role="dialog" aria-modal="true">
      <h3>Settings — Timeline & Annotations</h3>
      <div style="display:flex; gap:12px; margin-bottom:12px;">
        <div style="flex:1;">
          <div style="font-weight:700; margin-bottom:6px;">Timeline points</div>
          <div id="timelineList" style="display:flex; flex-direction:column; max-height:360px; overflow:auto;"></div>
          <div class="add-row">
            <input id="newTime" type="text" placeholder="Add time (HH:MM)"/>
            <button id="addTimeBtn" class="btn" style="padding:8px 12px; border-radius:8px;">Add</button>
          </div>
        </div>
        <div style="flex:1;">
          <div style="font-weight:700; margin-bottom:6px;">Annotations (time → label)</div>
          <div id="annotationsList" style="display:flex; flex-direction:column; gap:6px; max-height:360px; overflow:auto;"></div>
          <div class="add-row" style="margin-top:8px;">
            <input id="annTime" type="text" placeholder="HH:MM" style="width:28%"/>
            <input id="annLabel" type="text" placeholder="Annotation label (e.g. Break starts)" style="width:60%"/>
            <button id="addAnnBtn" class="btn" style="padding:8px 12px; border-radius:8px;">Add</button>
          </div>
        </div>
      </div>

      <div style="display:flex; justify-content:flex-end; gap:8px;">
        <button id="closeModal" class="close-btn">Close</button>
        <button id="saveSettings" class="save">Save Settings</button>
      </div>
    </div>
  </div>

<script>
/* Front-end JS preserved from original (only minimal additions for new UI) */

const fileInput = document.getElementById('fileInput');
const uploadBtn = document.getElementById('uploadBtn');
const statusEl = document.getElementById('status');
const resultTable = document.getElementById('resultTable');
const downloadLink = document.getElementById('downloadLink');
const downloadZone = document.getElementById('downloadZone');
const copyBtn = document.getElementById('copyBtn');

const modalBackdrop = document.getElementById('modalBackdrop');
const openSettings = document.getElementById('openSettings');
const closeModal = document.getElementById('closeModal');
const timelineList = document.getElementById('timelineList');
const annotationsList = document.getElementById('annotationsList');
const newTime = document.getElementById('newTime');
const addTimeBtn = document.getElementById('addTimeBtn');
const annTime = document.getElementById('annTime');
const annLabel = document.getElementById('annLabel');
const addAnnBtn = document.getElementById('addAnnBtn');
const saveSettings = document.getElementById('saveSettings');

const copyAllBtn = document.getElementById('copyAllBtn');
const clearBtn = document.getElementById('clearBtn');
const timelinePreview = document.getElementById('timelinePreview');

let es = null;
let lastTaskId = null;

// small helper to update timeline preview
function updateTimelinePreview(){
  fetch('/settings').then(r=>r.json()).then(data=>{
    const t = data.timeline || [];
    timelinePreview.textContent = t.join(' • ');
  }).catch(()=>{ timelinePreview.textContent = ''; });
}
updateTimelinePreview();

fileInput.addEventListener('change', async function(){
  const file = this.files[0];
  if(!file) return;
  // clear previous
  resultTable.innerHTML = '';
  downloadZone.innerHTML = '';
  downloadLink.style.display = 'none';
  statusEl.textContent = 'Uploading...';

  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await fetch('/upload', { method: 'POST', body: fd });
    const j = await res.json();
    if(j.error){ statusEl.textContent='Error'; alert('Upload error: ' + j.error); return; }
    const taskId = j.task_id;
    lastTaskId = taskId;
    statusEl.textContent = 'Processing...';

    // start SSE (hidden log streaming) — we only react to DONE/FAILED
    if(es){ try{ es.close(); }catch(e){} }
    es = new EventSource('/stream/' + taskId);
    es.onmessage = function(evt){
      const text = evt.data;
      // we don't show system logs in UI, only change status on DONE/FAILED
      if(text === 'DONE'){
        statusEl.textContent = 'Completed';
        fetch('/result/' + taskId).then(r=>r.json()).then(data=>{
          if(data.rows){
            populateTable(data.rows);
            if(data.csv){
              downloadLink.href = '/download/' + data.csv;
              downloadLink.textContent = 'Download CSV';
              downloadLink.style.display = 'inline-block';
              downloadZone.innerHTML = '';
              downloadZone.appendChild(downloadLink);
            }
          }
        });
        es.close();
      }
      if(text === 'FAILED'){
        statusEl.textContent = 'Failed';
        es.close();
        alert('Processing failed. Check the file format and required columns (Join Time, Leave Time).');
      }
    };
    es.onerror = function(e){
      // keep UI simple: if SSE connection errors, set Idle but allow result fetch later
      statusEl.textContent = 'Idle';
    };
  } catch(err){
    statusEl.textContent = 'Error';
    alert('Upload failed: ' + err);
  }
});

function populateTable(rows){
  resultTable.innerHTML = '';
  rows.forEach(r=>{
    const tr = document.createElement('tr');
    const td1 = document.createElement('td'); td1.className='time'; td1.textContent = r[0];
    const td2 = document.createElement('td'); td2.className='val'; td2.textContent = r[1];
    tr.appendChild(td1); tr.appendChild(td2);
    resultTable.appendChild(tr);
  });
}

// Copy both columns as TSV (no header)
copyBtn.addEventListener('click', function(){
  const rows = Array.from(document.querySelectorAll('#resultTable tr'));
  if(rows.length === 0){ alert('No results to copy'); return; }
  const lines = rows.map(tr => {
    const t = tr.children[0].textContent.trim();
    const v = tr.children[1].textContent.trim();
    return t + '\\t' + v;
  });
  const tsv = lines.join('\\n');
  navigator.clipboard.writeText(tsv).then(()=> {
    const old = copyBtn.innerHTML;
    copyBtn.innerHTML = '✓ Copied';
    setTimeout(()=> copyBtn.innerHTML = old, 1400);
  }, ()=> alert('Copy failed — try manually selecting the table.'));
});

// Quick actions (right column)
copyAllBtn.addEventListener('click', ()=>{
  copyBtn.click();
});
clearBtn.addEventListener('click', ()=>{
  resultTable.innerHTML = '';
  downloadLink.style.display = 'none';
  downloadZone.innerHTML = '';
  statusEl.textContent = 'Idle';
});

// Settings modal behavior
openSettings.addEventListener('click', openSettingsModal);
closeModal.addEventListener('click', () => modalBackdrop.style.display = 'none');

function openSettingsModal(){
  // fetch current settings from server
  fetch('/settings').then(r=>r.json()).then(data=>{
    const timeline = data.timeline || [];
    const annotations = data.annotations || {};
    renderTimelineList(timeline);
    renderAnnotationsList(annotations);
    modalBackdrop.style.display = 'flex';
  });
}

function renderTimelineList(timeline){
  timelineList.innerHTML = '';
  // Sort times
  const sorted = timeline.slice().sort();
  sorted.forEach(t=>{
    const div = document.createElement('div');
    div.className = 'list-item';
    const input = document.createElement('input');
    input.type = 'text';
    input.value = t;
    input.style.width = '120px';
    input.addEventListener('blur', ()=> {
      // basic validation on blur; if invalid, highlight red briefly
      if(!/^([01]\\d|2[0-3]):([0-5]\\d)$/.test(input.value.trim())){
        input.style.border = '1px solid #ff6b6b';
        setTimeout(()=> input.style.border = 'none', 1200);
      }
    });
    const rem = document.createElement('button');
    rem.textContent = 'Remove';
    rem.className = 'remove-btn';
    rem.onclick = ()=> div.remove();
    div.appendChild(input);
    div.appendChild(rem);
    timelineList.appendChild(div);
  });
}

function renderAnnotationsList(annotations){
  annotationsList.innerHTML = '';
  const keys = Object.keys(annotations).sort();
  keys.forEach(k=>{
    const div = document.createElement('div');
    div.className = 'list-item';
    const kInput = document.createElement('input');
    kInput.type = 'text';
    kInput.value = k;
    kInput.style.width = '100px';
    const vInput = document.createElement('input');
    vInput.type = 'text';
    vInput.value = annotations[k];
    vInput.style.flex = '1';
    const rem = document.createElement('button');
    rem.textContent = 'Remove';
    rem.className = 'remove-btn';
    rem.onclick = ()=> div.remove();
    div.appendChild(kInput); div.appendChild(vInput); div.appendChild(rem);
    annotationsList.appendChild(div);
  });
}

addTimeBtn.addEventListener('click', ()=>{
  const t = newTime.value.trim();
  if(!t) return;
  if(!/^([01]\\d|2[0-3]):([0-5]\\d)$/.test(t)){
    alert('Enter time in HH:MM (24h) format');
    return;
  }
  const div = document.createElement('div');
  div.className = 'list-item';
  const input = document.createElement('input');
  input.type = 'text';
  input.value = t;
  input.style.width = '120px';
  const rem = document.createElement('button');
  rem.textContent = 'Remove';
  rem.className = 'remove-btn';
  rem.onclick = ()=> div.remove();
  div.appendChild(input); div.appendChild(rem);
  timelineList.appendChild(div);
  newTime.value = '';
});

addAnnBtn.addEventListener('click', ()=>{
  const t = annTime.value.trim();
  const lbl = annLabel.value.trim();
  if(!t || !lbl){ alert('Time and label required'); return; }
  if(!/^([01]\\d|2[0-3]):([0-5]\\d)$/.test(t)){ alert('Time must be HH:MM'); return; }
  const div = document.createElement('div');
  div.className = 'list-item';
  const kInput = document.createElement('input');
  kInput.type = 'text'; kInput.value = t; kInput.style.width = '100px';
  const vInput = document.createElement('input');
  vInput.type = 'text'; vInput.value = lbl; vInput.style.flex = '1';
  const rem = document.createElement('button'); rem.textContent='Remove'; rem.className='remove-btn';
  rem.onclick = ()=> div.remove();
  div.appendChild(kInput); div.appendChild(vInput); div.appendChild(rem);
  annotationsList.appendChild(div);
  annTime.value = ''; annLabel.value = '';
});

saveSettings.addEventListener('click', ()=>{
  // gather timeline from timelineList inputs
  const items = Array.from(timelineList.querySelectorAll('.list-item'));
  const times = [];
  items.forEach(div=>{
    const inp = div.querySelector('input[type=text]');
    if(inp){
      const v = inp.value.trim();
      if(/^([01]\\d|2[0-3]):([0-5]\\d)$/.test(v)) times.push(v);
    }
  });
  // dedupe & sort
  const uniqueSorted = Array.from(new Set(times)).sort();

  // gather annotations
  const annDivs = Array.from(annotationsList.querySelectorAll('.list-item'));
  const anns = {};
  annDivs.forEach(div=>{
    const inputs = div.querySelectorAll('input[type=text]');
    if(inputs.length >= 2){
      const k = inputs[0].value.trim();
      const v = inputs[1].value.trim();
      if(/^([01]\\d|2[0-3]):([0-5]\\d)$/.test(k) && v) anns[k] = v;
    }
  });

  // Send to server
  fetch('/save_settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ timeline: uniqueSorted, annotations: anns })
  }).then(r=>r.json()).then(resp=>{
    if(resp.ok){
      statusEl.textContent = 'Settings saved';
      setTimeout(()=> statusEl.textContent = 'Idle', 1200);
      modalBackdrop.style.display = 'none';
      updateTimelinePreview();
    } else {
      alert('Failed to save settings.');
    }
  }).catch(err=>{
    alert('Failed to save settings: ' + err);
  });
});

/* Initialize nothing else on load; settings are loaded when modal opens */
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

@app.route("/upload", methods=["POST"])
def upload():
    if 'file' not in request.files:
        return jsonify({"error": "no file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "no selected file"}), 400
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        save_name = f"{uuid.uuid4().hex}_{filename}"
        path = os.path.join(app.config["UPLOAD_FOLDER"], save_name)
        file.save(path)

        # create task queue and id
        task_id = uuid.uuid4().hex
        q = Queue()
        TASK_QUEUES[task_id] = q

        # start processing in background
        thread = threading.Thread(target=process_file, args=(task_id, path), daemon=True)
        thread.start()

        return jsonify({"task_id": task_id})
    else:
        return jsonify({"error": "invalid file type"}), 400

@app.route("/stream/<task_id>")
def stream(task_id):
    q = TASK_QUEUES.get(task_id)
    if not q:
        return Response("No such task", status=404)

    def event_stream():
        while True:
            try:
                msg = q.get(timeout=0.5)
                yield f"data: {msg}\n\n"
                if msg in ("DONE", "FAILED"):
                    break
            except Empty:
                continue
    return Response(event_stream(), mimetype="text/event-stream")

@app.route("/result/<task_id>")
def result(task_id):
    data = TASK_RESULTS.get(task_id)
    if not data:
        return jsonify({"error":"no result"}), 404
    rows = data.get("rows", [])
    return jsonify({"rows": rows, "csv": data.get("csv"), "dates": data.get("dates")})

@app.route("/download/<filename>")
def download(filename):
    return send_from_directory(app.config["OUTPUT_FOLDER"], filename, as_attachment=True)

# Settings endpoints
@app.route("/settings", methods=["GET"])
def get_settings():
    return jsonify({
        "timeline": CURRENT_SETTINGS["timeline"],
        "annotations": CURRENT_SETTINGS["annotations"]
    })

@app.route("/save_settings", methods=["POST"])
def save_settings():
    payload = request.get_json() or {}
    tl = payload.get("timeline")
    anns = payload.get("annotations")
    # Basic validation
    if isinstance(tl, list):
        # validate times format HH:MM
        good = []
        for t in tl:
            t = t.strip()
            try:
                datetime.strptime(t, "%H:%M")
                good.append(t)
            except:
                continue
        # sort and set
        CURRENT_SETTINGS["timeline"] = sorted(list(dict.fromkeys(good)))
    if isinstance(anns, dict):
        # keep only valid HH:MM keys
        good_ann = {}
        for k, v in anns.items():
            k2 = k.strip()
            try:
                datetime.strptime(k2, "%H:%M")
                if v and isinstance(v, str):
                    good_ann[k2] = v.strip()
            except:
                continue
        CURRENT_SETTINGS["annotations"] = good_ann
    return jsonify({"ok": True})

if __name__ == "__main__":
    # Use PORT env var for local testing; production will use gunicorn
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
