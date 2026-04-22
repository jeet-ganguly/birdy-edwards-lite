import os
import json
import pickle
import time
import threading
import traceback
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file
 

from socmint_lite_db import (
    init_db, import_all, compute_frequency, extract_top7
)
from commentor_scoring_lite import (
    get_profile_id,
    get_all_interactors,
    get_top7,
    get_graph_data,
    get_cocomment_graph,
    get_interaction_timeline,
    get_post_type_counts,
    get_profile_summary,
)
 

from fb_about_sb  import main as scrape_about
from fb_photos_sb import main as scrape_photos
from fb_reels_sb  import main as scrape_reels
from fb_posts_sb  import main as scrape_posts

try:
    from face_intelligence_lite import run_face_clustering
    FACE_AVAILABLE = True
except ImportError:
    FACE_AVAILABLE = False

 
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))   
ROOT_DIR    = os.path.dirname(BASE_DIR)                     
ICONS_DIR   = os.path.join(BASE_DIR, 'icons')
COOKIE_FILE = os.path.join(BASE_DIR, 'fb_cookies.pkl')
DB_FILE     = os.path.join(BASE_DIR, 'socmint_lite.db')
FACE_DIR    = os.path.join(BASE_DIR, 'face_data')
 
os.makedirs(ICONS_DIR, exist_ok=True)
os.makedirs(FACE_DIR,  exist_ok=True)

DEPTH_LIMITS = {
    'light':  {'posts': 5,  'reels': 5,  'photos': 5},
    'medium': {'posts': 10, 'reels': 10, 'photos': 10},
    'deep':   {'posts': 20, 'reels': 20, 'photos': 20},
}
 

pipeline_state = {
    'running':     False,
    'profile_url': '',
    'depth':       '',
    'steps':       [],
    'error':       None,
    'profile_id':  None,
    'started_at':  None,
    'finished_at': None,
}
 
PIPELINE_STEPS = [
    {'id': 'about',     'label': 'Scraping — About / Profile Info'},
    {'id': 'photos',    'label': 'Scraping — Photos + Comments'},
    {'id': 'reels',     'label': 'Scraping — Reels + Comments'},
    {'id': 'posts',     'label': 'Scraping — Posts + Comments'},
    {'id': 'db',        'label': 'Database Import'},
    {'id': 'frequency', 'label': 'Frequency Scoring'},
    {'id': 'top7',      'label': 'Top 7 Metadata Gather'},
    {'id': 'face',      'label': 'Face Clustering — CNN'},
]
 
def reset_pipeline(profile_url='', depth=''):
    pipeline_state.update({
        'running':     False,
        'profile_url': profile_url,
        'depth':       depth,
        'error':       None,
        'profile_id':  None,
        'started_at':  None,
        'finished_at': None,
        'steps': [
            {'id': s['id'], 'label': s['label'], 'status': 'pending'}
            for s in PIPELINE_STEPS
        ],
    })
 
def set_step(step_id, status):
    for s in pipeline_state['steps']:
        if s['id'] == step_id:
            s['status'] = status
            break
 

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, 'templates'),
    static_folder=os.path.join(BASE_DIR, 'static'),
)
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(32)
 
# ensure DB on startup
init_db(DB_FILE)
 
 
 
 
@app.route('/')
def home():
    cookie_status = _check_cookie_status_fast()
    return render_template('index.html', cookie_status=cookie_status)
 
 
@app.route('/analysis')
def analysis():
    profile_id = request.args.get('id', type=int)
    if not profile_id:
        return redirect(url_for('home'))
    profile = get_profile_summary(DB_FILE, profile_id)
    if not profile:
        return redirect(url_for('home'))
    return render_template('analysis.html', profile=profile, profile_id=profile_id)
 

 
@app.route('/api/import-cookies', methods=['POST'])
def import_cookies():
    """
    Receive cookie JSON from Cookie-Editor extension,
    convert to pickle format and save as fb_cookies.pkl.
    """
    import json as _json
    import pickle
 
    try:
        data = request.get_json()
        if not data:
            return jsonify({'ok': False, 'error': 'No data received'})
 
        cookies_json = data.get('cookies', '')
        if not cookies_json:
            return jsonify({'ok': False, 'error': 'No cookies provided'})
 
        # Parse JSON — Cookie-Editor exports as array
        if isinstance(cookies_json, str):
            cookies = _json.loads(cookies_json)
        else:
            cookies = cookies_json  # already parsed
 
        if not isinstance(cookies, list):
            return jsonify({'ok': False, 'error': 'Invalid format — expected JSON array'})
 
        if len(cookies) == 0:
            return jsonify({'ok': False, 'error': 'Cookie array is empty'})
 
        # Validate — must contain c_user (core FB session cookie)
        names = [c.get('name', '') for c in cookies]
        if 'c_user' not in names:
            return jsonify({
                'ok': False,
                'error': 'Missing c_user cookie — make sure you are logged into Facebook before exporting'
            })
 
        # Convert Cookie-Editor format to Selenium format
        # Cookie-Editor uses: name, value, domain, path, expires, httpOnly, secure, sameSite
        # Selenium uses:      name, value, domain, path, expiry, httpOnly, secure
        converted = []
        for c in cookies:
            selenium_cookie = {
                'name':     c.get('name', ''),
                'value':    c.get('value', ''),
                'domain':   c.get('domain', '.facebook.com'),
                'path':     c.get('path', '/'),
                'httpOnly': c.get('httpOnly', False),
                'secure':   c.get('secure', False),
            }
            # expiry field
            expires = c.get('expirationDate') or c.get('expires') or c.get('expiry')
            if expires and isinstance(expires, (int, float)):
                selenium_cookie['expiry'] = int(expires)
 
            # sameSite
            same_site = c.get('sameSite', 'None')
            if same_site in ('Strict', 'Lax', 'None'):
                selenium_cookie['sameSite'] = same_site
 
            converted.append(selenium_cookie)
 
        # Save to fb_cookies.pkl
        cookie_path = os.path.join(BASE_DIR, 'fb_cookies.pkl')
        pickle.dump(converted, open(cookie_path, 'wb'))
 
        # Update app config
        app.config['COOKIES_OK'] = True
 
        return jsonify({
            'ok': True,
            'count': len(converted),
            'message': f'{len(converted)} cookies imported successfully'
        })
 
    except _json.JSONDecodeError as e:
        return jsonify({'ok': False, 'error': f'Invalid JSON: {str(e)}'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

 
@app.route('/api/check-cookies')
def check_cookies():
    """Fast check — file exists + readable. No Selenium, instant response."""
    status = _check_cookie_status_fast()
    # don't expose raw cookies list over API
    return jsonify({
        'exists': status['exists'],
        'count':  status['count'],
        'error':  status['error'],
    })
 
 
@app.route('/api/verify-session')
def verify_session():
    """
    On-demand Selenium session check.
    Only called when user clicks 'Verify Session' button — not on page load.
    """
    fast = _check_cookie_status_fast()
    if not fast['exists'] or fast['count'] == 0:
        return jsonify({'ok': False, 'valid': False,
                        'error': 'No cookies found — import cookies first'})
 
    try:
        from seleniumbase import SB
        result = {'valid': False, 'error': None}
 
        def _run():
            try:
                with SB(uc=True, headless=True, xvfb=True) as sb:
                    sb.open('https://www.facebook.com')
                    time.sleep(2)
                    for c in fast['cookies']:
                        try:
                            sb.driver.add_cookie({
                                'name':   c.get('name', ''),
                                'value':  c.get('value', ''),
                                'domain': c.get('domain', '.facebook.com'),
                            })
                        except Exception:
                            pass
                    sb.driver.refresh()
                    time.sleep(4)
                    cur_url = sb.driver.current_url
                    if 'login' in cur_url or 'checkpoint' in cur_url:
                        result['valid'] = False
                        result['error'] = 'Session expired — please re-import fresh cookies'
                    else:
                        result['valid'] = True
            except Exception as e:
                result['error'] = str(e)
 
        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=35)
 
        return jsonify({'ok': True, 'valid': result['valid'], 'error': result['error']})
 
    except ImportError:
        return jsonify({'ok': True, 'valid': True, 'error': None,
                        'note': 'Selenium unavailable — skipped check'})
 
 
def _check_cookie_status_fast():
    """
    Fast cookie status — no Selenium, no network.
    Returns: {exists, count, cookies, error}
    """
    if not os.path.exists(COOKIE_FILE):
        return {'exists': False, 'count': 0, 'cookies': [], 'error': None}
    try:
        with open(COOKIE_FILE, 'rb') as f:
            cookies = pickle.load(f)
        if not isinstance(cookies, list) or len(cookies) == 0:
            return {'exists': True, 'count': 0, 'cookies': [],
                    'error': 'Cookie file empty or corrupt'}
        return {'exists': True, 'count': len(cookies), 'cookies': cookies, 'error': None}
    except Exception as e:
        return {'exists': True, 'count': 0, 'cookies': [],
                'error': f'Cannot read cookie file: {e}'}
 


 
@app.route('/api/investigations')
def get_investigations():
    import sqlite3
    try:
        con = sqlite3.connect(DB_FILE)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("""
            SELECT
                p.id, p.profile_url, p.owner_name, p.is_locked, p.scraped_at,
                (SELECT COUNT(*) FROM photo_posts WHERE profile_id = p.id) +
                (SELECT COUNT(*) FROM reel_posts  WHERE profile_id = p.id) +
                (SELECT COUNT(*) FROM text_posts  WHERE profile_id = p.id) AS post_count,
                (SELECT COUNT(DISTINCT commentor_id) FROM commentor_frequency
                 WHERE profile_id = p.id) AS interactor_count,
                (SELECT COUNT(*) FROM detected_faces df
                 JOIN photo_posts pp ON pp.id = df.photo_post_id
                 WHERE pp.profile_id = p.id) AS face_count
            FROM profiles p
            ORDER BY p.id DESC
        """)
        rows = cur.fetchall()
        con.close()
        return jsonify({'ok': True, 'records': [
            {
                'id':               r['id'],
                'name':             r['owner_name'] or 'Unknown',
                'url':              r['profile_url'],
                'is_locked':        bool(r['is_locked']),
                'scraped_at':       r['scraped_at'] or '',
                'post_count':       r['post_count'] or 0,
                'interactor_count': r['interactor_count'] or 0,
                'face_count':       r['face_count'] or 0,
            }
            for r in rows
        ]})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
 
 
@app.route('/api/investigations/<int:profile_id>', methods=['DELETE'])
def delete_investigation(profile_id):
    """
    Completely purge all data for a given profile_id.
    Deletion order respects FK dependencies:
      detected_faces → face_clusters (orphans) →
      top7_profile_fields → top7_profiles →
      commentor_frequency →
      photo_comments / reel_comments / text_comments →
      photo_posts / reel_posts / text_posts →
      profile_fields → profiles
    Orphaned commentors (no remaining comments) are also removed.
    Face image files are deleted from disk.
    """
    import sqlite3, glob
 
    if not profile_id:
        return jsonify({'ok': False, 'error': 'profile_id required'}), 400
 
    try:
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
 
        # collect face image paths before deleting them 
        cur.execute("""
            SELECT df.face_image_path
            FROM detected_faces df
            JOIN photo_posts pp ON pp.id = df.photo_post_id
            WHERE pp.profile_id = ? AND df.face_image_path IS NOT NULL
        """, (profile_id,))
        face_paths = [row[0] for row in cur.fetchall()]
 
        # collect face_cluster IDs used by this profile 
        cur.execute("""
            SELECT DISTINCT df.person_id
            FROM detected_faces df
            JOIN photo_posts pp ON pp.id = df.photo_post_id
            WHERE pp.profile_id = ? AND df.person_id IS NOT NULL
        """, (profile_id,))
        cluster_ids = [row[0] for row in cur.fetchall()]
 
        # delete detected_faces for this profile
        cur.execute("""
            DELETE FROM detected_faces
            WHERE photo_post_id IN (
                SELECT id FROM photo_posts WHERE profile_id = ?
            )
        """, (profile_id,))
 
        # delete face_clusters that now have no detected_faces 
        if cluster_ids:
            placeholders = ','.join('?' * len(cluster_ids))
            cur.execute(f"""
                DELETE FROM face_clusters
                WHERE id IN ({placeholders})
                AND id NOT IN (SELECT DISTINCT person_id FROM detected_faces WHERE person_id IS NOT NULL)
            """, cluster_ids)
 
        # top7_profile_fields → top7_profiles 
        cur.execute("""
            DELETE FROM top7_profile_fields
            WHERE top7_profile_id IN (
                SELECT id FROM top7_profiles WHERE profile_id = ?
            )
        """, (profile_id,))
        cur.execute("DELETE FROM top7_profiles WHERE profile_id = ?", (profile_id,))
 
        # commentor_frequency 
        cur.execute("DELETE FROM commentor_frequency WHERE profile_id = ?", (profile_id,))
 
        # comments (all three types)
        cur.execute("""
            DELETE FROM photo_comments
            WHERE photo_post_id IN (SELECT id FROM photo_posts WHERE profile_id = ?)
        """, (profile_id,))
        cur.execute("""
            DELETE FROM reel_comments
            WHERE reel_post_id IN (SELECT id FROM reel_posts WHERE profile_id = ?)
        """, (profile_id,))
        cur.execute("""
            DELETE FROM text_comments
            WHERE text_post_id IN (SELECT id FROM text_posts WHERE profile_id = ?)
        """, (profile_id,))
 
        # posts (all three types)
        cur.execute("DELETE FROM photo_posts WHERE profile_id = ?", (profile_id,))
        cur.execute("DELETE FROM reel_posts  WHERE profile_id = ?", (profile_id,))
        cur.execute("DELETE FROM text_posts  WHERE profile_id = ?", (profile_id,))
 
        #  profile fields + profile itself 
        cur.execute("DELETE FROM profile_fields WHERE profile_id = ?", (profile_id,))
        cur.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
 
        # orphaned commentors (not referenced in any remaining comment)
        cur.execute("""
            DELETE FROM commentors
            WHERE id NOT IN (SELECT commentor_id FROM photo_comments)
            AND   id NOT IN (SELECT commentor_id FROM reel_comments)
            AND   id NOT IN (SELECT commentor_id FROM text_comments)
        """)
 
        con.commit()
        con.close()
 
        # delete face image files from disk 
        deleted_files = 0
        for rel_path in face_paths:
            if not rel_path:
                continue
            full = os.path.join(BASE_DIR, rel_path.lstrip('/'))
            if os.path.exists(full):
                try:
                    os.remove(full)
                    deleted_files += 1
                except Exception:
                    pass
 
        return jsonify({
            'ok': True,
            'message': f'Investigation #{profile_id} deleted',
            'face_files_removed': deleted_files,
        })
 
    except Exception as e:
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500
 
@app.route('/api/start-pipeline', methods=['POST'])
def start_pipeline():
    if pipeline_state['running']:
        return jsonify({'ok': False, 'error': 'Pipeline already running'}), 409
 
    data        = request.get_json(silent=True) or {}
    profile_url = data.get('profile_url', '').strip()
    depth       = data.get('depth', 'light').strip().lower()
 
    if not profile_url:
        return jsonify({'ok': False, 'error': 'profile_url required'}), 400
    if depth not in DEPTH_LIMITS:
        depth = 'light'
 
    status = _check_cookie_status_fast()
    if not status['exists'] or status['count'] == 0:
        return jsonify({'ok': False,
                        'error': 'Cookie file not found — import cookies first'}), 400
 
    reset_pipeline(profile_url=profile_url, depth=depth)
    pipeline_state['running']    = True
    pipeline_state['started_at'] = datetime.now().isoformat()
 
    threading.Thread(target=_run_pipeline, args=(profile_url, depth), daemon=True).start()
    return jsonify({'ok': True, 'message': 'Pipeline started'})
 
 
@app.route('/api/pipeline-status')
def pipeline_status():
    return jsonify({
        'ok':          True,
        'running':     pipeline_state['running'],
        'profile_url': pipeline_state['profile_url'],
        'depth':       pipeline_state['depth'],
        'steps':       pipeline_state['steps'],
        'error':       pipeline_state['error'],
        'profile_id':  pipeline_state['profile_id'],
        'started_at':  pipeline_state['started_at'],
        'finished_at': pipeline_state['finished_at'],
    })
 
 
def _run_pipeline(profile_url, depth):
    import multiprocessing, random
 
    limits       = DEPTH_LIMITS[depth]
    posts_limit  = limits['posts']
    reels_limit  = limits['reels']
    photos_limit = limits['photos']
 
    def _staggered(delay_s, fn, kwargs):
        """Sleep, then run a scraper. Used as multiprocessing target."""
        time.sleep(delay_s + random.uniform(0, 1.0))   # fixed stagger + jitter
        fn(**kwargs)
 
    try:
        # Parallel scraping 
        
        set_step('about',  'active')
        set_step('photos', 'active')
        set_step('reels',  'active')
        set_step('posts',  'active')
 
        p1 = multiprocessing.Process(
            target=_staggered,
            args=(0, scrape_about, {'PROFILE_URL': profile_url}),
            name='scrape-about'
        )
        p2 = multiprocessing.Process(
            target=_staggered,
            args=(2, scrape_photos, {'PROFILE_URL': profile_url, 'MAX_PHOTOS': photos_limit}),
            name='scrape-photos'
        )
        p3 = multiprocessing.Process(
            target=_staggered,
            args=(4, scrape_reels, {'PROFILE_URL': profile_url, 'MAX_REELS': reels_limit}),
            name='scrape-reels'
        )
        p4 = multiprocessing.Process(
            target=_staggered,
            args=(6, scrape_posts, {'profile_url': profile_url, 'max_posts': posts_limit}),
            name='scrape-posts'
        )
 
        for p in [p1, p2, p3, p4]:
            p.start()
 
        # wait and track exit codes so we can mark steps done/error
        for p, step_id in [(p1, 'about'), (p2, 'photos'), (p3, 'reels'), (p4, 'posts')]:
            p.join()
            if p.exitcode == 0:
                set_step(step_id, 'done')
            else:
                set_step(step_id, 'error')
                print(f'[PIPELINE] {step_id} exited with code {p.exitcode}')
 
        # step 5 — db import
        set_step('db', 'active')
        try:
            import_all(
                about_json  = os.path.join(BASE_DIR, 'fb_about.json'),
                photos_json = os.path.join(BASE_DIR, 'fb_photos.json'),
                reels_json  = os.path.join(BASE_DIR, 'fb_reels.json'),
                posts_json  = os.path.join(BASE_DIR, 'fb_posts.json'),
                db_file     = DB_FILE,
            )
            p = get_profile_id(DB_FILE)
            if p:
                pipeline_state['profile_id'] = p['id']
            set_step('db', 'done')
        except Exception as e:
            _step_error('db', e)
            _finish_pipeline(error=str(e))
            return
 
        profile_id = pipeline_state['profile_id']
 
        # step 6 — frequency
        set_step('frequency', 'active')
        try:
            compute_frequency(DB_FILE, profile_id)
            set_step('frequency', 'done')
        except Exception as e:
            _step_error('frequency', e)
 
        # step 7 — top 7
        set_step('top7', 'active')
        try:
            top7 = extract_top7(DB_FILE, profile_id)
            _scrape_top7_about(top7, profile_id)
            set_step('top7', 'done')
        except Exception as e:
            _step_error('top7', e)
 
        # step 8 — face clustering
        set_step('face', 'active')
        try:
            if FACE_AVAILABLE:
                run_face_clustering(DB_FILE, profile_id)
            set_step('face', 'done')
        except Exception as e:
            _step_error('face', e)
 
        _finish_pipeline()
 
    except Exception as e:
        traceback.print_exc()
        _finish_pipeline(error=str(e))
 
 
def _scrape_top7_about(top7, profile_id):
    import sqlite3
    for entry in top7:
        url = entry.get('profile_url', '')
        if not url:
            continue
        try:
            scrape_about(PROFILE_URL=url)
            about_file = os.path.join(BASE_DIR, 'fb_about.json')
            if not os.path.exists(about_file):
                continue
            with open(about_file, encoding='utf-8') as f:
                data = json.load(f)
            sections = data.get('sections', {})
            con = sqlite3.connect(DB_FILE)
            cur = con.cursor()
            cur.execute("""
                SELECT id FROM top7_profiles
                WHERE profile_id = ? AND commentor_id = ?
            """, (profile_id, entry['commentor_id']))
            row = cur.fetchone()
            if not row:
                con.close()
                continue
            t7id = row[0]
            cur.execute("DELETE FROM top7_profile_fields WHERE top7_profile_id = ?", (t7id,))
            for section, fields in sections.items():
                for field in fields:
                    cur.execute("""
                        INSERT INTO top7_profile_fields
                            (top7_profile_id, section, field_type, label, value, sub_label)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (t7id, section, field.get('field_type'), field.get('label'),
                          field.get('value'), field.get('sub_label')))
            con.commit()
            con.close()
        except Exception as e:
            print(f'  [TOP7] {url}: {e}')
 
 
def _step_error(step_id, exc):
    set_step(step_id, 'error')
    print(f'[PIPELINE] {step_id} error: {exc}')
 
 
def _finish_pipeline(error=None):
    pipeline_state['running']     = False
    pipeline_state['finished_at'] = datetime.now().isoformat()
    if error:
        pipeline_state['error'] = error
 
 
@app.route('/api/profile-summary/<int:profile_id>')
def api_profile_summary(profile_id):
    try:
        return jsonify({'ok': True, 'data': get_profile_summary(DB_FILE, profile_id)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
 
@app.route('/api/all-interactors/<int:profile_id>')
def api_all_interactors(profile_id):
    try:
        return jsonify({'ok': True, 'data': get_all_interactors(DB_FILE, profile_id)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
 
@app.route('/api/top7/<int:profile_id>')
def api_top7(profile_id):
    try:
        return jsonify({'ok': True, 'data': get_top7(DB_FILE, profile_id)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
 
@app.route('/api/graph-data/<int:profile_id>')
def api_graph_data(profile_id):
    try:
        return jsonify({'ok': True, 'data': get_graph_data(DB_FILE, profile_id)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
 
@app.route('/api/cocomment-graph/<int:profile_id>')
def api_cocomment_graph(profile_id):
    try:
        return jsonify({'ok': True, 'data': get_cocomment_graph(DB_FILE, profile_id)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
 
@app.route('/api/timeline/<int:profile_id>')
def api_timeline(profile_id):
    try:
        return jsonify({'ok': True, 'data': get_interaction_timeline(DB_FILE, profile_id)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
 
@app.route('/api/post-type-counts/<int:profile_id>')
def api_post_type_counts(profile_id):
    try:
        return jsonify({'ok': True, 'data': get_post_type_counts(DB_FILE, profile_id)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
 
@app.route('/api/face-clusters/<int:profile_id>')
def api_face_clusters(profile_id):
    import sqlite3
    try:
        con = sqlite3.connect(DB_FILE)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("""
            SELECT DISTINCT fc.id, fc.person_label, fc.representative_face,
                            fc.appearance_count, fc.post_ids, fc.created_at
            FROM face_clusters fc
            WHERE fc.id IN (
                SELECT DISTINCT df.person_id
                FROM detected_faces df
                LEFT JOIN photo_posts pp ON pp.id = df.photo_post_id
                LEFT JOIN text_posts  tp ON tp.id = df.text_post_id
                WHERE df.person_id IS NOT NULL
                  AND (pp.profile_id = ? OR tp.profile_id = ?)
            )
            ORDER BY fc.appearance_count DESC
        """, (profile_id, profile_id))
        rows = [dict(r) for r in cur.fetchall()]
        con.close()
        return jsonify({'ok': True, 'data': rows})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
 
 
@app.route('/api/face-cluster/<int:cluster_id>/members')
def api_face_cluster_members(cluster_id):
    import sqlite3
    try:
        con = sqlite3.connect(DB_FILE)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("""
            SELECT id, photo_post_id, text_post_id,
                   face_index, face_image_path, person_id
            FROM detected_faces
            WHERE person_id = ?
            ORDER BY id
        """, (cluster_id,))
        rows = [dict(r) for r in cur.fetchall()]
        con.close()
        return jsonify({'ok': True, 'data': rows})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

 
@app.route('/logo')
def serve_logo():
    """Serve icons/logo.jpeg from birdy-edwards-lite/icons/"""
    logo = os.path.join(BASE_DIR, 'icons', 'logo.jpeg')
    if os.path.exists(logo):
        return send_file(logo, mimetype='image/jpeg')
    # fallback: 1x1 transparent pixel
    import base64
    px = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
    from flask import Response
    return Response(px, mimetype='image/gif')
 
@app.route('/threat')
def serve_icon():
    """Serve icons/logo.jpeg from birdy-edwards-lite/icons/"""
    logo = os.path.join(BASE_DIR, 'icons', 'search.png')
    if os.path.exists(logo):
        return send_file(logo, mimetype='image/jpeg')
    # fallback: 1x1 transparent pixel
    import base64
    px = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
    from flask import Response
    return Response(px, mimetype='image/gif')

@app.route('/user')
def serve_user():
    """Serve icons/logo.jpeg from birdy-edwards-lite/icons/"""
    logo = os.path.join(BASE_DIR, 'icons', 'spy.png')
    if os.path.exists(logo):
        return send_file(logo, mimetype='image/jpeg')
    # fallback: 1x1 transparent pixel
    import base64
    px = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
    from flask import Response
    return Response(px, mimetype='image/gif')

@app.route('/face-image/<path:filepath>')
def face_image(filepath):
    """Serve face crop images from face_data/"""
    full = os.path.join(BASE_DIR, filepath)
    if os.path.exists(full):
        return send_file(full)
    return '', 404

@app.route('/api/photo-posts/<int:profile_id>')
def api_photo_posts(profile_id):
    import sqlite3
    try:
        con = sqlite3.connect(DB_FILE)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("""
            SELECT
                pp.id, pp.photo_url, pp.image_src, pp.caption, pp.date_text,
                COUNT(pc.id) AS interaction_count
            FROM photo_posts pp
            LEFT JOIN photo_comments pc ON pc.photo_post_id = pp.id
            WHERE pp.profile_id = ?
            GROUP BY pp.id
            ORDER BY pp.id DESC
        """, (profile_id,))
        rows = [dict(r) for r in cur.fetchall()]
        con.close()
        return jsonify({'ok': True, 'data': rows})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/text-posts/<int:profile_id>')
def api_text_posts(profile_id):
    import sqlite3
    try:
        con = sqlite3.connect(DB_FILE)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("""
            SELECT
                tp.id, tp.post_url, tp.screenshot_path, tp.date_text,
                COUNT(tc.id) AS interaction_count
            FROM text_posts tp
            LEFT JOIN text_comments tc ON tc.text_post_id = tp.id
            WHERE tp.profile_id = ?
            GROUP BY tp.id
            ORDER BY tp.id DESC
        """, (profile_id,))
        rows = [dict(r) for r in cur.fetchall()]
        con.close()
        return jsonify({'ok': True, 'data': rows})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/reel-posts/<int:profile_id>')
def api_reel_posts(profile_id):
    import sqlite3
    try:
        con = sqlite3.connect(DB_FILE)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("""
            SELECT
                rp.id, rp.reel_url, rp.scraped_at,
                COUNT(rc.id) AS interaction_count
            FROM reel_posts rp
            LEFT JOIN reel_comments rc ON rc.reel_post_id = rp.id
            WHERE rp.profile_id = ?
            GROUP BY rp.id
            ORDER BY rp.id DESC
        """, (profile_id,))
        rows = [dict(r) for r in cur.fetchall()]
        con.close()
        return jsonify({'ok': True, 'data': rows})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/screenshot/<path:filepath>')
def serve_screenshot(filepath):
    full = os.path.join(BASE_DIR, filepath)
    if os.path.exists(full):
        return send_file(full)
    return '', 404
    
#  MAIN

if __name__ == '__main__':
    import sys

    # Colors (ANSI escape codes)
    RED = '\033[0;31m'
    CYAN = '\033[0;36m'
    YELLOW = '\033[1;33m'
    RESET = '\033[0m'

    try:
        print()
        print(f"{RED}██████╗ ██╗██████╗ ██████╗ ██╗   ██╗      ███████╗██████╗ ██╗    ██╗ █████╗ ██████╗ ██████╗ ███████╗{RESET}")
        print(f"{RED}██╔══██╗██║██╔══██╗██╔══██╗╚██╗ ██╔╝      ██╔════╝██╔══██╗██║    ██║██╔══██╗██╔══██╗██╔══██╗██╔════╝{RESET}")
        print(f"{RED}██████╔╝██║██████╔╝██║  ██║ ╚████╔╝ █████╗█████╗  ██║  ██║██║ █╗ ██║███████║██████╔╝██║  ██║███████╗{RESET}")
        print(f"{RED}██╔══██╗██║██╔══██╗██║  ██║  ╚██╔╝  ╚════╝██╔══╝  ██║  ██║██║███╗██║██╔══██║██╔══██╗██║  ██║╚════██║{RESET}")
        print(f"{RED}██████╔╝██║██║  ██║██████╔╝   ██║         ███████╗██████╔╝╚███╔███╔╝██║  ██║██║  ██║██████╔╝███████║{RESET}")
        print(f"{RED}╚═════╝ ╚═╝╚═╝  ╚═╝╚═════╝    ╚═╝         ╚══════╝╚═════╝  ╚══╝╚══╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚══════╝{RESET}")
        print()
        print(f"{YELLOW}                        Infiltrate & Expose — Setup v1.0{RESET}")
        print(f"{CYAN}                        Developed by Jeet Ganguly{RESET}")
        print("Visit -> http://127.0.0.1:5000")

    except KeyboardInterrupt:
        sys.exit(0)

    app.run(debug=False, host='127.0.0.1', port=5000, threaded=True)