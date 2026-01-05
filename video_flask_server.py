"""
SJTU Video Flask Server - Web UI for downloading course video recordings.

Provides a visual interface to:
- View enrolled courses from Canvas
- List video sessions for each course
- Download video recordings
"""

import argparse
import json
import logging
import threading
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request, Response
from flask_cors import CORS
from jac_login import JACLogin
from canvas_client import CanvasClient
from video_client import VideoClient

app = Flask(__name__)
logging.basicConfig(filename='video_access.log', level=logging.INFO)

CORS(app)

# Load credentials
with open('credentials.json', 'r') as f:
    credentials = json.load(f)

# Initialize clients
jac_login = JACLogin(credentials['username'], credentials['password'])
canvas_client = CanvasClient(jac_login)
video_client = VideoClient(jac_login)

# Cache for courses and sessions
cache = {
    'courses': [],
    'sessions': {},  # course_id -> sessions list
    'last_refresh': None
}
cache_lock = threading.Lock()


def get_client_ip():
    """Get client IP address from request headers."""
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0]
    return request.remote_addr


# HTML template for dashboard
DASHBOARD_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>课程视频下载</title>
<style>
:root {
    --bg: #0a0a0f;
    --card-bg: #16161b;
    --card-hover: #1e1e26;
    --text: #f0f0f5;
    --sub-text: #8888aa;
    --accent: #7c6aff;
    --accent-hover: #9d8fff;
    --accent-glow: rgba(124, 106, 255, 0.15);
    --success: #3fb950;
    --error: #f85149;
    --border: #2a2a35;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { 
    font-family: var(--font); 
    background: linear-gradient(135deg, #0a0a0f 0%, #12121a 100%);
    color: var(--text); 
    padding: 2rem 1rem; 
    line-height: 1.6;
    min-height: 100vh;
}
.wrap { max-width: 900px; margin: 0 auto; }

/* Header */
header { 
    display: flex; 
    align-items: center; 
    justify-content: space-between; 
    margin-bottom: 2.5rem;
    padding-bottom: 1.5rem;
    border-bottom: 1px solid var(--border);
}
h1 { 
    font-size: 1.75rem; 
    font-weight: 700; 
    letter-spacing: -0.5px;
    background: linear-gradient(135deg, #fff 0%, #c5c5ff 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}
.status-dot { 
    font-size: 0.8rem; 
    color: var(--success); 
    display: flex; 
    align-items: center; 
    gap: 8px; 
    background: rgba(63, 185, 80, 0.08); 
    padding: 6px 14px; 
    border-radius: 20px;
    border: 1px solid rgba(63, 185, 80, 0.2);
}
.pulse { 
    display: inline-block; 
    width: 8px; 
    height: 8px; 
    background: currentColor; 
    border-radius: 50%; 
    animation: pulse 2s infinite;
    box-shadow: 0 0 8px currentColor;
}

/* Sections */
h2 { 
    font-size: 0.8rem; 
    text-transform: uppercase; 
    letter-spacing: 1.5px; 
    color: var(--sub-text); 
    margin: 0 0 1rem 0; 
    font-weight: 600; 
}
.section { margin-bottom: 2rem; }

/* Course Selector - Custom styled */
.course-selector-wrap {
    position: relative;
    margin-bottom: 1.5rem;
}
.course-select {
    width: 100%;
    padding: 1rem 3rem 1rem 1.25rem;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    color: var(--text);
    font-size: 1rem;
    font-weight: 500;
    cursor: pointer;
    appearance: none;
    -webkit-appearance: none;
    transition: all 0.25s ease;
}
.course-select:hover {
    border-color: var(--accent);
    background: var(--card-hover);
}
.course-select:focus {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-glow);
}
.course-select option {
    background: var(--card-bg);
    color: var(--text);
    padding: 12px;
}
.course-selector-wrap::after {
    content: '▼';
    position: absolute;
    right: 1.25rem;
    top: 50%;
    transform: translateY(-50%);
    color: var(--sub-text);
    font-size: 0.7rem;
    pointer-events: none;
    transition: transform 0.2s ease;
}
.course-selector-wrap:hover::after {
    color: var(--accent);
}

/* Video List */
.video-list { display: flex; flex-direction: column; gap: 0.75rem; }
.video-item { 
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: var(--card-bg);
    padding: 1.25rem 1.5rem;
    border-radius: 12px;
    border: 1px solid var(--border);
    transition: all 0.25s ease;
    cursor: pointer;
}
.video-item:hover {
    background: var(--card-hover);
    border-color: var(--accent);
    transform: translateX(6px);
    box-shadow: 0 4px 20px rgba(0,0,0,0.3);
}
.video-info { flex: 1; }
.video-name { font-weight: 600; margin-bottom: 0.3rem; font-size: 1.05rem; }
.video-meta { font-size: 0.8rem; color: var(--sub-text); }
.video-actions { display: flex; gap: 0.5rem; }
.btn {
    padding: 0.6rem 1.2rem;
    border-radius: 8px;
    border: none;
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    text-decoration: none;
}
.btn-primary {
    background: linear-gradient(135deg, var(--accent) 0%, #9d8fff 100%);
    color: white;
    box-shadow: 0 2px 10px var(--accent-glow);
}
.btn-primary:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 15px var(--accent-glow);
}
.btn-secondary {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text);
}
.btn-secondary:hover {
    border-color: var(--accent);
    background: var(--accent-glow);
}

/* Date Groups */
.date-group {
    margin-bottom: 1.5rem;
}
.date-header {
    font-size: 0.9rem;
    font-weight: 600;
    color: var(--text);
    padding: 0.5rem 0;
    margin-bottom: 0.75rem;
    border-bottom: 1px solid var(--border);
}
.date-count {
    color: var(--sub-text);
    font-weight: 400;
    font-size: 0.8rem;
}
.date-videos {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
}

/* Loading & Empty States */
.loading, .empty {
    text-align: center;
    padding: 3rem;
    color: var(--sub-text);
    background: var(--card-bg);
    border-radius: 12px;
    border: 1px dashed var(--border);
}
.loading::after {
    content: '';
    display: inline-block;
    animation: dots 1.5s infinite;
}
@keyframes dots {
    0%, 20% { content: '.'; }
    40% { content: '..'; }
    60%, 100% { content: '...'; }
}

/* Video Detail Modal */
.modal {
    display: none;
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0,0,0,0.85);
    backdrop-filter: blur(4px);
    z-index: 100;
    align-items: center;
    justify-content: center;
}
.modal.active { display: flex; }
.modal-content {
    background: var(--card-bg);
    padding: 2rem;
    border-radius: 16px;
    max-width: 500px;
    width: 90%;
    max-height: 80vh;
    overflow-y: auto;
    border: 1px solid var(--border);
    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
}
.modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1.5rem;
    padding-bottom: 1rem;
    border-bottom: 1px solid var(--border);
}
.modal-header h3 {
    font-size: 1.1rem;
    font-weight: 600;
}
.modal-close {
    background: none;
    border: none;
    color: var(--sub-text);
    font-size: 1.5rem;
    cursor: pointer;
    transition: color 0.2s;
    width: 32px;
    height: 32px;
    border-radius: 8px;
}
.modal-close:hover {
    color: var(--error);
    background: rgba(248, 81, 73, 0.1);
}
.channel-list { display: flex; flex-direction: column; gap: 0.75rem; }
.channel-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem;
    background: var(--bg);
    border-radius: 10px;
    border: 1px solid var(--border);
}
.channel-item span {
    font-weight: 500;
}

/* Footer */
.meta { 
    border-top: 1px solid var(--border); 
    padding-top: 1.5rem; 
    margin-top: 2rem;
    font-size: 0.75rem; 
    color: var(--sub-text); 
    display: flex; 
    justify-content: space-between;
}

@keyframes pulse { 
    0%, 100% { opacity: 1; transform: scale(1); } 
    50% { opacity: 0.5; transform: scale(0.9); } 
}
</style>
</head>
<body>
<div class="wrap">
    <header>
        <h1>课程视频下载</h1>
    </header>

    <div class="section">
        <h2>选择课程</h2>
        <div class="course-selector-wrap">
            <select class="course-select" id="courseSelect" onchange="loadSessions()">
                <option value="">请选择课程...</option>
                {% for course in courses %}
                <option value="{{course.id}}">{{course.name}}</option>
                {% endfor %}
            </select>
        </div>
    </div>

    <div class="section">
        <h2>视频列表 <span id="videoCount"></span></h2>
        <div id="videoList" class="video-list">
            <div class="empty">👆 请先选择一门课程</div>
        </div>
    </div>

    <div class="meta">
        <div>共 {{courses|length}} 门课程</div>
        <div>SJTU Suite</div>
    </div>
</div>

<!-- Video Detail Modal -->
<div class="modal" id="videoModal" onclick="if(event.target===this)closeModal()">
    <div class="modal-content">
        <div class="modal-header">
            <h3 id="modalTitle">视频详情</h3>
            <button class="modal-close" onclick="closeModal()">×</button>
        </div>
        <div id="modalContent">
            <div class="loading">加载中</div>
        </div>
    </div>
</div>

<script>
let currentCourseId = null;

async function loadSessions() {
    const courseId = document.getElementById('courseSelect').value;
    const list = document.getElementById('videoList');
    const countEl = document.getElementById('videoCount');
    
    if (!courseId) {
        list.innerHTML = '<div class="empty">请先选择一门课程</div>';
        countEl.textContent = '';
        return;
    }
    
    // Always reload when course changes
    currentCourseId = courseId;
    list.innerHTML = '<div class="loading">加载视频列表中</div>';
    countEl.textContent = '';
    
    try {
        const resp = await fetch(`/sessions/${courseId}`);
        const data = await resp.json();
        
        // Check if user switched course while loading
        if (currentCourseId !== courseId) return;
        
        if (data.error) {
            list.innerHTML = `<div class="empty">${data.error}</div>`;
            return;
        }
        
        if (!data.sessions || data.sessions.length === 0) {
            list.innerHTML = '<div class="empty">该课程暂无视频录像</div>';
            return;
        }
        
        countEl.textContent = `(${data.sessions.length})`;
        
        // Group sessions by date
        const groups = {};
        data.sessions.forEach(s => {
            const dateTime = s.courseBeginTime || '';
            const datePart = dateTime.split(' ')[0] || dateTime.split('T')[0] || '未知日期';
            if (!groups[datePart]) {
                groups[datePart] = [];
            }
            groups[datePart].push(s);
        });
        
        // Sort dates descending (newest first)
        const sortedDates = Object.keys(groups).sort().reverse();
        
        // Build HTML with date groups
        let html = '';
        sortedDates.forEach(date => {
            const sessions = groups[date];
            const displayDate = formatDate(date);
            html += `<div class="date-group">
                <div class="date-header">${displayDate} <span class="date-count">(${sessions.length})</span></div>
                <div class="date-videos">`;
            
            sessions.forEach(s => {
                html += `
                <div class="video-item" onclick="showVideoDetail('${s.id || s.videoId}', '${encodeURIComponent(s.videoName || s.name || "视频")}')">
                    <div class="video-info">
                        <div class="video-name">${s.videoName || s.name || '未命名视频'}</div>
                        <div class="video-meta">${s.courseBeginTime ? s.courseBeginTime + ' · ' : ''}${s.courseEndTime || ''}</div>
                    </div>
                    <div class="video-actions">
                        <span class="btn btn-secondary">查看详情 →</span>
                    </div>
                </div>`;
            });
            
            html += '</div></div>';
        });
        
        list.innerHTML = html;
    } catch (e) {
        list.innerHTML = `<div class="empty">加载失败: ${e.message}</div>`;
    }
}

function formatDate(dateStr) {
    if (!dateStr || dateStr === '未知日期') return dateStr;
    try {
        const parts = dateStr.split('-');
        if (parts.length === 3) {
            const year = parts[0];
            const month = parseInt(parts[1], 10);
            const day = parseInt(parts[2], 10);
            const weekdays = ['日', '一', '二', '三', '四', '五', '六'];
            const d = new Date(year, month - 1, day);
            const weekday = weekdays[d.getDay()];
            return `${month}月${day}日 周${weekday}`;
        }
    } catch (e) {}
    return dateStr;
}

async function showVideoDetail(videoId, name) {
    const modal = document.getElementById('videoModal');
    const title = document.getElementById('modalTitle');
    const content = document.getElementById('modalContent');
    
    modal.classList.add('active');
    title.textContent = decodeURIComponent(name);
    content.innerHTML = '<div class="loading">加载中</div>';
    
    try {
        const resp = await fetch(`/video/${videoId}`);
        const data = await resp.json();
        
        if (data.error) {
            content.innerHTML = `<div class="empty">${data.error}</div>`;
            return;
        }
        
        const channels = data.channels || [];
        if (channels.length === 0) {
            content.innerHTML = '<div class="empty">无可用下载链接</div>';
            return;
        }
        
        content.innerHTML = `
            <div class="channel-list">
                ${channels.map(ch => `
                    <div class="channel-item">
                        <span>${ch.description}</span>
                        <a href="${ch.url}" target="_blank" class="btn btn-primary">下载</a>
                    </div>
                `).join('')}
            </div>
        `;
    } catch (e) {
        content.innerHTML = `<div class="empty">加载失败: ${e.message}</div>`;
    }
}

function closeModal() {
    document.getElementById('videoModal').classList.remove('active');
}

document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeModal();
});
</script>
</body>
</html>'''


@app.route('/')
def dashboard():
    """Render the video download dashboard."""
    try:
        if not canvas_client.logged_in:
            canvas_client.login()
        
        with cache_lock:
            if not cache['courses']:
                cache['courses'] = canvas_client.get_courses_with_video()
                cache['last_refresh'] = datetime.now()
            courses = cache['courses']
    except Exception as e:
        app.logger.error("Failed to load courses: %s", e)
        courses = []
    
    return render_template_string(DASHBOARD_TEMPLATE, courses=courses)


@app.route('/courses')
def list_courses():
    """API endpoint to get list of courses."""
    try:
        if not canvas_client.logged_in:
            canvas_client.login()
        
        courses = canvas_client.get_courses_with_video()
        
        with cache_lock:
            cache['courses'] = courses
            cache['last_refresh'] = datetime.now()
        
        return jsonify({
            'courses': courses,
            'count': len(courses)
        })
    except Exception as e:
        app.logger.error("Failed to fetch courses: %s", e)
        return jsonify({'error': str(e)}), 500


@app.route('/sessions/<int:course_id>')
def list_sessions(course_id):
    """API endpoint to get video sessions for a course."""
    try:
        video_url = canvas_client.get_course_video_url(course_id)
        
        # IMPORTANT: Video portal token is tied to specific course
        # Must re-authenticate for each course switch
        video_client.token = None
        video_client.token_id = None
        video_client.canvas_course_id = None
        
        video_client.login(video_url)
        
        if not video_client.token:
            return jsonify({'error': '视频系统认证失败，请检查登录状态'}), 401
        
        sessions = video_client.get_sessions()
        
        with cache_lock:
            cache['sessions'][course_id] = sessions
        
        return jsonify({
            'course_id': course_id,
            'sessions': sessions,
            'count': len(sessions)
        })
    except Exception as e:
        app.logger.error("Failed to fetch sessions for course %d: %s", course_id, e)
        return jsonify({'error': str(e)}), 500


@app.route('/video/<video_id>')
def get_video(video_id):
    """API endpoint to get video info with download URLs."""
    try:
        if not video_client.token:
            return jsonify({'error': '视频系统未认证'}), 401
        
        video_info = video_client.get_video_info(video_id=video_id)
        
        if not video_info:
            return jsonify({'error': '无法获取视频信息'}), 404
        
        urls = video_client.get_video_urls(video_info)
        
        channels = [
            {
                'channel': ch,
                'url': url,
                'description': desc
            }
            for ch, url, desc in urls
        ]
        
        return jsonify({
            'video_id': video_id,
            'name': video_info.get('videName', 'Unknown'),
            'channels': channels,
            'raw': video_info
        })
    except Exception as e:
        app.logger.error("Failed to get video info for %s: %s", video_id, e)
        return jsonify({'error': str(e)}), 500


@app.route('/status')
def status():
    """API endpoint to get current status."""
    return jsonify({
        'status': 'running',
        'canvas_logged_in': canvas_client.logged_in,
        'video_authenticated': video_client.token is not None,
        'cached_courses': len(cache.get('courses', [])),
        'last_refresh': cache.get('last_refresh').isoformat() if cache.get('last_refresh') else None
    })


@app.after_request
def log_request(response):
    """Log all requests."""
    app.logger.info(
        '%s: %s %s %s from %s',
        datetime.now(),
        request.method,
        request.path,
        response.status_code,
        get_client_ip()
    )
    return response


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run Video Flask server')
    parser.add_argument('-p', '--port', type=int, default=5003,
                        help='Port to listen on (default: 5003)')
    
    args = parser.parse_args()
    
    print(f"Starting Video Server on port {args.port}")
    print(f"Dashboard: http://localhost:{args.port}/")
    
    # Initial login attempt
    try:
        if canvas_client.login():
            print("Canvas login successful!")
        else:
            print("Canvas login failed - courses may not load properly")
    except Exception as e:
        print(f"Canvas login error: {e}")
    
    app.run(debug=False, port=args.port, threaded=True)
