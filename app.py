import os
from datetime import datetime, timedelta
from flask import Flask, request, render_template_string, jsonify
import pandas as pd
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB

# Default timeline and annotations
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

def process_file_simple(file_data, timeline, annotations):
    """Process file entirely in memory, return results immediately"""
    
    # Read file into DataFrame
    if file_data.filename.endswith('.csv'):
        df = pd.read_csv(file_data, dtype=str)
    else:
        df = pd.read_excel(file_data, engine="openpyxl", dtype=str)
    
    # Normalize columns
    df.columns = df.columns.str.strip().str.title()
    
    # Check required columns
    if 'Join Time' not in df.columns or 'Leave Time' not in df.columns:
        return {"error": "Missing 'Join Time' or 'Leave Time' columns"}
    
    # Parse datetimes
    df['Join Time'] = pd.to_datetime(df['Join Time'], dayfirst=True, errors='coerce')
    df['Leave Time'] = pd.to_datetime(df['Leave Time'], dayfirst=True, errors='coerce')
    df = df.dropna(subset=['Join Time', 'Leave Time'])
    
    if len(df) == 0:
        return {"error": "No valid date/time rows found"}
    
    # Create dedupe key
    if 'Email' in df.columns:
        df['clean_email'] = df['Email'].astype(str).str.lower().str.strip()
    else:
        for c in ['Name', 'Name (Original Name)', 'Full Name']:
            if c in df.columns:
                df['clean_email'] = df[c].astype(str).str.lower().str.strip()
                break
        if 'clean_email' not in df.columns:
            df['clean_email'] = df.index.astype(str)
    
    # Get event date
    df['Event_Date'] = df['Join Time'].dt.date
    report_date = df['Event_Date'].iloc[0]
    
    # Calculate counts for each timeline point
    results = []
    for time_str in timeline:
        t_val = datetime.strptime(time_str, "%H:%M").time()
        base_dt = datetime.combine(report_date, t_val)
        
        # First timestamp special handling
        if time_str == timeline[0]:
            check_dt = pd.Timestamp(base_dt + timedelta(seconds=59))
        else:
            check_dt = pd.Timestamp(base_dt)
        
        active_rows = df[(df['Join Time'] <= check_dt) & (df['Leave Time'] >= check_dt)]
        count = int(active_rows['clean_email'].nunique())
        
        # Add annotation if exists
        if time_str in annotations:
            display = f"{count} ({annotations[time_str]})"
        else:
            display = str(count)
        
        results.append([time_str, display])
    
    return {"success": True, "results": results, "date": str(report_date)}

# HTML Template
HTML_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Webinar Attendee Counter</title>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap');
    
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { 
      font-family: 'Poppins', sans-serif; 
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      min-height: 100vh;
      padding: 20px;
    }
    
    .container {
      max-width: 800px;
      margin: 0 auto;
    }
    
    .card {
      background: white;
      border-radius: 20px;
      padding: 40px;
      box-shadow: 0 20px 60px rgba(0,0,0,0.3);
      margin-bottom: 20px;
    }
    
    h1 {
      color: #667eea;
      margin-bottom: 10px;
      font-size: 32px;
    }
    
    .subtitle {
      color: #666;
      margin-bottom: 30px;
      font-size: 14px;
    }
    
    .upload-area {
      border: 3px dashed #667eea;
      border-radius: 15px;
      padding: 40px;
      text-align: center;
      cursor: pointer;
      transition: all 0.3s;
      margin-bottom: 20px;
    }
    
    .upload-area:hover {
      background: #f8f9ff;
      border-color: #764ba2;
    }
    
    .upload-area.processing {
      border-color: #ffa500;
      background: #fff9f0;
    }
    
    .upload-icon {
      font-size: 48px;
      margin-bottom: 15px;
    }
    
    input[type="file"] {
      display: none;
    }
    
    .btn {
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      color: white;
      border: none;
      padding: 12px 30px;
      border-radius: 25px;
      font-weight: 600;
      cursor: pointer;
      font-size: 16px;
      transition: transform 0.2s;
    }
    
    .btn:hover {
      transform: translateY(-2px);
    }
    
    .btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    
    #status {
      text-align: center;
      padding: 15px;
      border-radius: 10px;
      margin: 20px 0;
      font-weight: 600;
      display: none;
    }
    
    #status.processing {
      background: #fff9f0;
      color: #ff8c00;
      display: block;
    }
    
    #status.success {
      background: #f0fff4;
      color: #22c55e;
      display: block;
    }
    
    #status.error {
      background: #fff0f0;
      color: #ef4444;
      display: block;
    }
    
    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 20px;
    }
    
    th {
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      color: white;
      padding: 15px;
      text-align: left;
      font-weight: 600;
    }
    
    td {
      padding: 12px 15px;
      border-bottom: 1px solid #eee;
    }
    
    tr:hover {
      background: #f8f9ff;
    }
    
    .copy-btn {
      background: white;
      color: #667eea;
      border: 2px solid #667eea;
      margin-top: 20px;
    }
    
    .copy-btn:hover {
      background: #667eea;
      color: white;
    }
    
    #resultsCard {
      display: none;
    }
    
    .settings-btn {
      background: white;
      color: #667eea;
      border: 2px solid #667eea;
      padding: 8px 20px;
      margin-left: 10px;
    }
    
    .header-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 30px;
    }
    
    .modal {
      display: none;
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background: rgba(0,0,0,0.7);
      z-index: 1000;
      align-items: center;
      justify-content: center;
    }
    
    .modal-content {
      background: white;
      padding: 30px;
      border-radius: 20px;
      max-width: 600px;
      width: 90%;
      max-height: 80vh;
      overflow-y: auto;
    }
    
    .timeline-item {
      display: flex;
      gap: 10px;
      margin-bottom: 10px;
      align-items: center;
    }
    
    .timeline-item input {
      flex: 1;
      padding: 8px;
      border: 2px solid #eee;
      border-radius: 8px;
    }
    
    .remove-btn {
      background: #ef4444;
      color: white;
      border: none;
      padding: 8px 15px;
      border-radius: 8px;
      cursor: pointer;
    }
    
    .add-section {
      margin-top: 20px;
      padding-top: 20px;
      border-top: 2px solid #eee;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="card">
      <div class="header-row">
        <div>
          <h1>📊 Webinar Attendee Counter</h1>
          <p class="subtitle">Upload your Zoom attendance report (.xlsx, .xls, or .csv)</p>
        </div>
        <button class="btn settings-btn" onclick="openSettings()">⚙️ Settings</button>
      </div>
      
      <div class="upload-area" id="uploadArea" onclick="document.getElementById('fileInput').click()">
        <div class="upload-icon">📁</div>
        <div><strong>Click to upload</strong> or drag and drop</div>
        <div style="color: #999; margin-top: 10px; font-size: 14px;">Supports .xlsx, .xls, .csv (max 25MB)</div>
      </div>
      
      <input type="file" id="fileInput" accept=".xlsx,.xls,.csv" onchange="handleUpload()">
      
      <div id="status"></div>
    </div>
    
    <div class="card" id="resultsCard">
      <h2 style="margin-bottom: 20px; color: #667eea;">Results</h2>
      <div id="dateInfo" style="color: #666; margin-bottom: 15px;"></div>
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Attendee Count</th>
          </tr>
        </thead>
        <tbody id="resultsTable"></tbody>
      </table>
      <button class="btn copy-btn" onclick="copyResults()">📋 Copy Results (TSV)</button>
    </div>
  </div>
  
  <!-- Settings Modal -->
  <div class="modal" id="settingsModal">
    <div class="modal-content">
      <h2 style="color: #667eea; margin-bottom: 20px;">Settings</h2>
      
      <h3 style="margin-bottom: 15px;">Timeline Points</h3>
      <div id="timelineList"></div>
      <div class="add-section">
        <input type="text" id="newTime" placeholder="Add time (HH:MM)" style="padding: 10px; border: 2px solid #eee; border-radius: 8px; margin-right: 10px;">
        <button class="btn" onclick="addTimePoint()" style="padding: 10px 20px;">Add Time</button>
      </div>
      
      <h3 style="margin: 30px 0 15px 0;">Annotations</h3>
      <div id="annotationsList"></div>
      <div class="add-section">
        <input type="text" id="newAnnTime" placeholder="Time (HH:MM)" style="width: 30%; padding: 10px; border: 2px solid #eee; border-radius: 8px; margin-right: 10px;">
        <input type="text" id="newAnnLabel" placeholder="Label" style="width: 50%; padding: 10px; border: 2px solid #eee; border-radius: 8px; margin-right: 10px;">
        <button class="btn" onclick="addAnnotation()" style="padding: 10px 20px;">Add</button>
      </div>
      
      <div style="margin-top: 30px; display: flex; gap: 10px; justify-content: flex-end;">
        <button class="btn settings-btn" onclick="closeSettings()">Cancel</button>
        <button class="btn" onclick="saveSettings()">Save Settings</button>
      </div>
    </div>
  </div>

<script>
let currentTimeline = {{ timeline | tojson }};
let currentAnnotations = {{ annotations | tojson }};

function openSettings() {
  renderSettings();
  document.getElementById('settingsModal').style.display = 'flex';
}

function closeSettings() {
  document.getElementById('settingsModal').style.display = 'none';
}

function renderSettings() {
  const timelineList = document.getElementById('timelineList');
  const annotationsList = document.getElementById('annotationsList');
  
  timelineList.innerHTML = currentTimeline.map((time, idx) => `
    <div class="timeline-item">
      <input type="text" value="${time}" data-idx="${idx}">
      <button class="remove-btn" onclick="removeTime(${idx})">Remove</button>
    </div>
  `).join('');
  
  annotationsList.innerHTML = Object.entries(currentAnnotations).map(([time, label]) => `
    <div class="timeline-item">
      <input type="text" value="${time}" readonly style="width: 30%">
      <input type="text" value="${label}" data-anntime="${time}">
      <button class="remove-btn" onclick="removeAnnotation('${time}')">Remove</button>
    </div>
  `).join('');
}

function addTimePoint() {
  const input = document.getElementById('newTime');
  const time = input.value.trim();
  if (!time.match(/^([01]\d|2[0-3]):([0-5]\d)$/)) {
    alert('Please enter time in HH:MM format (24-hour)');
    return;
  }
  if (!currentTimeline.includes(time)) {
    currentTimeline.push(time);
    currentTimeline.sort();
    renderSettings();
  }
  input.value = '';
}

function removeTime(idx) {
  currentTimeline.splice(idx, 1);
  renderSettings();
}

function addAnnotation() {
  const timeInput = document.getElementById('newAnnTime');
  const labelInput = document.getElementById('newAnnLabel');
  const time = timeInput.value.trim();
  const label = labelInput.value.trim();
  
  if (!time.match(/^([01]\d|2[0-3]):([0-5]\d)$/)) {
    alert('Please enter time in HH:MM format');
    return;
  }
  if (!label) {
    alert('Please enter a label');
    return;
  }
  
  currentAnnotations[time] = label;
  renderSettings();
  timeInput.value = '';
  labelInput.value = '';
}

function removeAnnotation(time) {
  delete currentAnnotations[time];
  renderSettings();
}

function saveSettings() {
  // Update timeline from inputs
  const inputs = document.querySelectorAll('#timelineList input');
  const newTimeline = [];
  inputs.forEach(inp => {
    const val = inp.value.trim();
    if (val.match(/^([01]\d|2[0-3]):([0-5]\d)$/)) {
      newTimeline.push(val);
    }
  });
  currentTimeline = [...new Set(newTimeline)].sort();
  
  // Update annotations from inputs
  const annInputs = document.querySelectorAll('#annotationsList .timeline-item');
  const newAnnotations = {};
  annInputs.forEach(div => {
    const inputs = div.querySelectorAll('input');
    const time = inputs[0].value.trim();
    const label = inputs[1].value.trim();
    if (time && label) {
      newAnnotations[time] = label;
    }
  });
  currentAnnotations = newAnnotations;
  
  closeSettings();
  alert('Settings saved! They will be used for the next file upload.');
}

async function handleUpload() {
  const fileInput = document.getElementById('fileInput');
  const file = fileInput.files[0];
  if (!file) return;
  
  const uploadArea = document.getElementById('uploadArea');
  const status = document.getElementById('status');
  const resultsCard = document.getElementById('resultsCard');
  
  uploadArea.classList.add('processing');
  status.className = 'processing';
  status.textContent = '⏳ Processing file...';
  resultsCard.style.display = 'none';
  
  const formData = new FormData();
  formData.append('file', file);
  formData.append('timeline', JSON.stringify(currentTimeline));
  formData.append('annotations', JSON.stringify(currentAnnotations));
  
  try {
    const response = await fetch('/process', {
      method: 'POST',
      body: formData
    });
    
    const data = await response.json();
    
    if (data.error) {
      status.className = 'error';
      status.textContent = '❌ Error: ' + data.error;
      uploadArea.classList.remove('processing');
    } else {
      status.className = 'success';
      status.textContent = '✅ Processing complete!';
      uploadArea.classList.remove('processing');
      
      displayResults(data.results, data.date);
    }
  } catch (error) {
    status.className = 'error';
    status.textContent = '❌ Upload failed: ' + error.message;
    uploadArea.classList.remove('processing');
  }
  
  fileInput.value = '';
}

function displayResults(results, date) {
  const resultsTable = document.getElementById('resultsTable');
  const dateInfo = document.getElementById('dateInfo');
  const resultsCard = document.getElementById('resultsCard');
  
  dateInfo.textContent = `Event Date: ${date}`;
  
  resultsTable.innerHTML = results.map(([time, count]) => `
    <tr>
      <td><strong>${time}</strong></td>
      <td>${count}</td>
    </tr>
  `).join('');
  
  resultsCard.style.display = 'block';
  resultsCard.scrollIntoView({ behavior: 'smooth' });
}

function copyResults() {
  const rows = document.querySelectorAll('#resultsTable tr');
  const text = Array.from(rows).map(row => {
    const cells = row.querySelectorAll('td');
    return `${cells[0].textContent}\t${cells[1].textContent}`;
  }).join('\\n');
  
  navigator.clipboard.writeText(text).then(() => {
    const btn = event.target;
    const originalText = btn.textContent;
    btn.textContent = '✓ Copied!';
    setTimeout(() => btn.textContent = originalText, 2000);
  });
}

// Drag and drop support
const uploadArea = document.getElementById('uploadArea');
uploadArea.addEventListener('dragover', (e) => {
  e.preventDefault();
  uploadArea.style.background = '#f8f9ff';
});

uploadArea.addEventListener('dragleave', () => {
  uploadArea.style.background = 'white';
});

uploadArea.addEventListener('drop', (e) => {
  e.preventDefault();
  uploadArea.style.background = 'white';
  const file = e.dataTransfer.files[0];
  if (file) {
    document.getElementById('fileInput').files = e.dataTransfer.files;
    handleUpload();
  }
});
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(
        HTML_TEMPLATE, 
        timeline=DEFAULT_TIMELINE,
        annotations=DEFAULT_ANNOTATIONS
    )

@app.route("/process", methods=["POST"])
def process():
    """Process file immediately and return results (no background tasks)"""
    
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400
    
    # Get custom timeline/annotations from request
    timeline_str = request.form.get('timeline')
    annotations_str = request.form.get('annotations')
    
    timeline = DEFAULT_TIMELINE
    annotations = DEFAULT_ANNOTATIONS
    
    try:
        if timeline_str:
            timeline = eval(timeline_str)  # In production, use json.loads
        if annotations_str:
            annotations = eval(annotations_str)
    except:
        pass
    
    # Process file directly (no saving to disk)
    try:
        result = process_file_simple(file, timeline, annotations)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
