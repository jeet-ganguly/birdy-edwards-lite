import os
import io
import json
import pickle
import shutil
import sqlite3
import traceback
import urllib.request
from datetime import datetime

import numpy as np
from PIL import Image, ImageOps

try:
    import face_recognition
    FACE_RECOGNITION_AVAILABLE = True
except ImportError:
    FACE_RECOGNITION_AVAILABLE = False


TOLERANCE     = 0.42    # face matching threshold (lower = stricter)
PADDING       = 0.30    # face crop padding fraction
MAX_IMG_PX    = 1200    # longest side limit before resize (lower = less RAM)
CNN_MAX_PX    = 900     # if image > this after resize, fall back to HOG for CNN
UPSAMPLE      = 1       # upsample passes for small-face detection
FACE_DATA_DIR = 'face_data'

def run_face_clustering(db_file: str, profile_id: int):
    """
    Main entry point called by app.py pipeline.
    Safe to call even if face_recognition is not installed — logs and returns.
    """
    if not FACE_RECOGNITION_AVAILABLE:
        print("  ⚠ face_recognition not installed — skipping face clustering")
        return

    print(f"\n{'═'*65}")
    print("FACE INTELLIGENCE LITE · CNN+HOG")
    print(f"Profile ID: {profile_id}")
    print('═'*65)

    con = sqlite3.connect(db_file)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("SELECT owner_name FROM profiles WHERE id = ?", (profile_id,))
    row = cur.fetchone()
    if not row:
        print(f"  ⚠ Profile {profile_id} not found in DB")
        con.close()
        return

    owner_name = (row['owner_name'] or f'profile_{profile_id}') \
        .replace(' ', '_').replace('/', '_').replace('\\', '_')

    cur.execute("""
        SELECT id, photo_url, image_src, caption
        FROM photo_posts
        WHERE profile_id = ? AND image_src IS NOT NULL AND image_src != ''
        ORDER BY id
    """, (profile_id,))
    photo_posts = cur.fetchall()

    # also fetch text post screenshots
    cur.execute("""
        SELECT id, post_url, screenshot_path
        FROM text_posts
        WHERE profile_id = ? AND screenshot_path IS NOT NULL AND screenshot_path != ''
        ORDER BY id
    """, (profile_id,))
    text_posts = cur.fetchall()
    con.close()

    if not photo_posts and not text_posts:
        print("  ⚠ No images or screenshots to process — skipping face detection")
        return

    print(f" {len(photo_posts)} photos to process")

    raw_dir  = os.path.join(FACE_DATA_DIR, owner_name, 'raw')
    pers_dir = os.path.join(FACE_DATA_DIR, owner_name, 'persons')
    os.makedirs(raw_dir,  exist_ok=True)
    os.makedirs(pers_dir, exist_ok=True)

    all_face_data = []

    for post in photo_posts:
        post_id   = post['id']
        image_src = post['image_src']

        faces = _process_image(post_id, image_src, raw_dir)
        for f in faces:
            f['source_type'] = 'photo'   
        all_face_data.extend(faces)

    # process text post screenshots
    for post in text_posts:
        post_id       = post['id']
        screenshot    = post['screenshot_path']

        img_bytes = _load_local_image(screenshot)
        if img_bytes is None:
            continue

        faces = _process_image(post_id, None, raw_dir, img_bytes=img_bytes)
        for f in faces:
            f['source_type'] = 'text'   
        all_face_data.extend(faces)

    print(f"\n Total faces detected: {len(all_face_data)}")

    if not all_face_data:
        print("  ⚠ No faces found — skipping clustering")
        return

    clusters = _cluster_faces(all_face_data, tolerance=TOLERANCE)
    print(f" Clusters formed: {len(clusters)}")

    # save cluster images 
    for cluster_id, members in clusters.items():
        person_dir = os.path.join(pers_dir, f'person_{cluster_id}')
        os.makedirs(person_dir, exist_ok=True)
        for m in members:
            src = m['image_path']
            dst = os.path.join(person_dir, os.path.basename(src))
            if os.path.exists(src) and not os.path.exists(dst):
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass

    # write DB 
    _save_to_db(db_file, profile_id, all_face_data, clusters)

    print(f"\n  Face intelligence complete")
    print(f"     {len(all_face_data)} faces · {len(clusters)} clusters")
    print(f"     Saved to: {os.path.join(FACE_DATA_DIR, owner_name)}/")

#  CORE IMAGE PROCESSING

def _process_image(post_id: int, url: str | None, raw_dir: str, img_bytes: bytes = None) -> list:
    """
    Download, verify, detect, encode faces for one image.
    Returns list of face dicts. Never raises — logs and returns [] on any error.
    """
    # download 
    if img_bytes is None:
        img_bytes = _download_bytes(url)
    if img_bytes is None:
        return []

    # verify + decode (fully, not lazily) 
    img = _safe_open(img_bytes)
    if img is None:
        return []

    # normalise to RGB numpy array
    img_rgb = _to_rgb_array(img)
    if img_rgb is None:
        return []

    # choose model based on image size
    h, w = img_rgb.shape[:2]
    model = 'hog' if max(h, w) > CNN_MAX_PX else 'cnn'

    # detect locations
    locations = _detect_locations(img_rgb, model)
    if not locations:
        return []

    # encode 
    encodings = _detect_encodings(img_rgb, locations)
    if not encodings:
        return []

    # crop + save each face
    faces = []
    for idx, (loc, enc) in enumerate(zip(locations, encodings)):
        
        if not isinstance(enc, np.ndarray) or enc.shape != (128,):
            continue

        crop_path = os.path.join(raw_dir, f'post{post_id}_face{idx}.jpg')
        saved = _save_face_crop(img, loc, crop_path)
        if not saved:
            continue

        faces.append({
            'post_id':    post_id,
            'face_index': idx,
            'image_path': crop_path,
            'encoding':   enc,
        })

    return faces


def _download_bytes(url: str) -> bytes | None:
    """Download URL → raw bytes. Returns None on failure."""
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': (
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
            )
        })
        return urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print(f"    ⚠ Download failed ({url[:70]}): {e}")
        return None

def _load_local_image(path: str) -> bytes | None:
    """Load a local screenshot file as bytes."""
    try:
        full = os.path.join(os.path.dirname(os.path.abspath(__file__)), path.lstrip('/'))
        if not os.path.exists(full):
            print(f"    ⚠ Screenshot not found: {full}")
            return None
        with open(full, 'rb') as f:
            return f.read()
    except Exception as e:
        print(f"    ⚠ Screenshot load failed ({path}): {e}")
        return None

def _safe_open(img_bytes: bytes) -> Image.Image | None:
    """
    Fully decode image from bytes.
    Uses img.verify() to catch truncated/corrupt files before face_recognition
    ever sees the pixel data.
    Returns None on any failure.
    """
    try:
        # verify pass (this consumes the stream, so open twice)
        Image.open(io.BytesIO(img_bytes)).verify()
    except Exception as e:
        print(f"    ⚠ Image verify failed: {e}")
        return None

    try:
        img = Image.open(io.BytesIO(img_bytes))
        # Force full decode now — not lazily later
        img.load()

        # resize if too large
        w, h = img.size
        if max(w, h) > MAX_IMG_PX:
            ratio = MAX_IMG_PX / max(w, h)
            img = img.resize(
                (int(w * ratio), int(h * ratio)),
                Image.LANCZOS
            )
        # fix EXIF rotation
        img = ImageOps.exif_transpose(img)
        return img
    except Exception as e:
        print(f"    ⚠ Image decode failed: {e}")
        return None


def _to_rgb_array(img: Image.Image) -> np.ndarray | None:
    """
    Convert any PIL image mode to a plain uint8 (H, W, 3) numpy array.
    Handles RGB, RGBA, P (palette), L (greyscale), CMYK, etc.
    Returns None on failure.
    """
    try:
        # normalise mode RGB before touching numpy
        if img.mode == 'RGBA':
            # paste onto white background to drop alpha
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        arr = np.array(img, dtype=np.uint8)

        # sanity check: must be (H, W, 3)
        if arr.ndim != 3 or arr.shape[2] != 3:
            print(f"    ⚠ Unexpected array shape {arr.shape} — skipping")
            return None

        return arr
    except Exception as e:
        print(f"    ⚠ Image→array conversion failed: {e}")
        return None


def _detect_locations(img_rgb: np.ndarray, model: str) -> list:
    """
    Detect face bounding boxes.
    If CNN raises any error, automatically retries with HOG.
    Returns list of (top, right, bottom, left) tuples.
    """
    try:
        locs = face_recognition.face_locations(
            img_rgb,
            number_of_times_to_upsample=UPSAMPLE,
            model=model
        )
        return locs
    except Exception as e:
        if model == 'cnn':
            print(f"    ⚠ CNN detection failed ({e}) — retrying with HOG")
            try:
                return face_recognition.face_locations(
                    img_rgb,
                    number_of_times_to_upsample=UPSAMPLE,
                    model='hog'
                )
            except Exception as e2:
                print(f"    ⚠ HOG detection also failed: {e2}")
                return []
        print(f"    ⚠ HOG detection failed: {e}")
        return []


def _detect_encodings(img_rgb: np.ndarray, locations: list) -> list:
    """
    Compute 128-d face encodings for given locations.
    Returns list of np.ndarray (may be shorter than locations if some fail).
    """
    try:
        return face_recognition.face_encodings(img_rgb, known_face_locations=locations)
    except Exception as e:
        print(f"    ⚠ Encoding failed: {e}")
        return []


def _save_face_crop(img: Image.Image, location: tuple, save_path: str) -> bool:
    """
    Crop face region with padding and save as JPEG.
    Returns True on success, False on any failure (including zero-size crop).
    """
    try:
        top, right, bottom, left = location
        w, h = img.size

        pad_v = int((bottom - top)  * PADDING)
        pad_h = int((right  - left) * PADDING)

        top    = max(0, top    - pad_v)
        left   = max(0, left   - pad_h)
        bottom = min(h, bottom + pad_v)
        right  = min(w, right  + pad_h)

        # guard zero-size crop
        if bottom <= top or right <= left:
            print(f"    ⚠ Zero-size crop at {location} — skipping")
            return False

        face_img = img.crop((left, top, right, bottom))

        # normalise to RGB before saving (crop preserves original mode)
        if face_img.mode != 'RGB':
            face_img = face_img.convert('RGB')

        face_img.save(save_path, 'JPEG', quality=88)
        return True
    except Exception as e:
        print(f"    ⚠ Crop/save failed ({save_path}): {e}")
        return False


#  CLUSTERING

def _cluster_faces(face_data: list, tolerance: float) -> dict:
    """
    Greedy nearest-centroid clustering.
    Each face is assigned to the closest existing cluster within tolerance,
    or starts a new cluster. Cluster centroids are updated as running means.

    Returns: {cluster_id (1-based): [face_data_items]}
    """
    clusters      = {}   # cluster_id → [face_data items]
    cluster_means = {}   # cluster_id → centroid np.array
    next_id       = 1

    for face in face_data:
        enc = face['encoding']
        best_cluster = None
        best_dist    = float('inf')

        for cid, mean_enc in cluster_means.items():
            try:
                dist = float(np.linalg.norm(enc - mean_enc))
                if dist < tolerance and dist < best_dist:
                    best_dist    = dist
                    best_cluster = cid
            except Exception:
                continue

        if best_cluster is not None:
            clusters[best_cluster].append(face)
            cluster_means[best_cluster] = np.mean(
                [m['encoding'] for m in clusters[best_cluster]], axis=0
            )
        else:
            clusters[next_id]      = [face]
            cluster_means[next_id] = enc.copy()
            next_id += 1

    return clusters


#  DATABASE


def _save_to_db(db_file: str, profile_id: int, all_face_data: list, clusters: dict):
    """Write face_clusters and detected_faces to SQLite."""
    con = sqlite3.connect(db_file)
    cur = con.cursor()

    # clear existing photo post faces
    cur.execute("""
        DELETE FROM detected_faces WHERE photo_post_id IN (
            SELECT id FROM photo_posts WHERE profile_id = ?
        )
    """, (profile_id,))

    # clear existing text post faces
    cur.execute("""
        DELETE FROM detected_faces WHERE text_post_id IN (
            SELECT id FROM text_posts WHERE profile_id = ?
        )
    """, (profile_id,))

    # clear orphaned clusters
    cur.execute("""
        DELETE FROM face_clusters WHERE id NOT IN (
            SELECT DISTINCT person_id FROM detected_faces WHERE person_id IS NOT NULL
        )
    """)

    now = datetime.now().isoformat()

    # map (post_id, face_index) → cluster_id
    face_to_cluster = {}
    for cid, members in clusters.items():
        for m in members:
            face_to_cluster[(m['post_id'], m['face_index'])] = cid

    # insert clusters
    db_cluster_ids = {}
    for cid, members in clusters.items():
        rep_face = members[0]['image_path'] if members else None
        post_ids = list({m['post_id'] for m in members})

        cur.execute("""
            INSERT INTO face_clusters
                (person_label, representative_face, appearance_count, post_ids, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            f'person_{cid}',
            rep_face,
            len(members),
            json.dumps(post_ids),
            now,
        ))
        db_cluster_ids[cid] = cur.lastrowid

    # insert detected faces
    for face in all_face_data:
        key    = (face['post_id'], face['face_index'])
        cid    = face_to_cluster.get(key)
        db_cid = db_cluster_ids.get(cid) if cid else None

        try:
            enc_blob = pickle.dumps(face['encoding'])
        except Exception:
            enc_blob = None

        is_photo = face.get('source_type') == 'photo'

        cur.execute("""
            INSERT INTO detected_faces
                (photo_post_id, text_post_id, face_index,
                face_image_path, encoding, person_id, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            face['post_id'] if is_photo else None,   # photo_post_id
            face['post_id'] if not is_photo else None, # text_post_id
            face['face_index'],
            face['image_path'],
            enc_blob,
            db_cid,
            now,
        ))

    con.commit()
    con.close()
    print(f"  DB: {len(all_face_data)} detected_faces · {len(clusters)} face_clusters written")


# standalone test 
if __name__ == '__main__':
    import sys
    db  = sys.argv[1] if len(sys.argv) > 1 else 'socmint_lite.db'
    pid = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    run_face_clustering(db, pid)