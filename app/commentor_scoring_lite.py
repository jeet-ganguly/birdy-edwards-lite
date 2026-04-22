import sqlite3
import os
from datetime import datetime

DB_FILE = "socmint_lite.db"

def get_profile_id(db_file=DB_FILE):
    """Get the most recent profile ID from DB."""
    con = sqlite3.connect(db_file)
    cur = con.cursor()
    cur.execute("SELECT id, profile_url, owner_name FROM profiles ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    con.close()
    if row:
        return {'id': row[0], 'profile_url': row[1], 'owner_name': row[2]}
    return None


def get_all_interactors(db_file=DB_FILE, profile_id=None):
    """
    Return all interactors sorted by total interaction frequency.
    Used for the full interactors table in UI.
    """
    if not profile_id:
        p = get_profile_id(db_file)
        if not p:
            return []
        profile_id = p['id']

    con = sqlite3.connect(db_file)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("""
        SELECT
            co.id           as commentor_id,
            co.name,
            co.profile_url,
            cf.photo_count,
            cf.reel_count,
            cf.text_count,
            cf.total_count
        FROM commentor_frequency cf
        JOIN commentors co ON co.id = cf.commentor_id
        WHERE cf.profile_id = ?
        ORDER BY cf.total_count DESC
    """, (profile_id,))

    results = [dict(row) for row in cur.fetchall()]
    con.close()
    return results


def get_top7(db_file=DB_FILE, profile_id=None):
    """
    Return top 7 interactors with their about metadata.
    Used for the Top 7 section in UI.
    """
    if not profile_id:
        p = get_profile_id(db_file)
        if not p:
            return []
        profile_id = p['id']

    con = sqlite3.connect(db_file)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("""
        SELECT
            t7.id           as top7_id,
            t7.commentor_id,
            t7.profile_url,
            t7.name,
            t7.comment_count,
            t7.rank
        FROM top7_profiles t7
        WHERE t7.profile_id = ?
        ORDER BY t7.rank ASC
    """, (profile_id,))

    top7 = []
    for row in cur.fetchall():
        entry = dict(row)

        # Fetch about fields for this top7 profile
        cur.execute("""
            SELECT section, field_type, label, value, sub_label
            FROM top7_profile_fields
            WHERE top7_profile_id = ?
            ORDER BY section, id
        """, (entry['top7_id'],))
        entry['fields'] = [dict(f) for f in cur.fetchall()]

        top7.append(entry)

    con.close()
    return top7


def get_graph_data(db_file=DB_FILE, profile_id=None):
    """
    Return commentor data formatted for the frequency network graph.
    Same structure as the full version's comment-graph API.
    """
    if not profile_id:
        p = get_profile_id(db_file)
        if not p:
            return {'commentors': []}
        profile_id = p['id']

    con = sqlite3.connect(db_file)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("""
        SELECT
            co.id           as commentor_id,
            co.name,
            co.profile_url,
            cf.total_count  as comment_count
        FROM commentor_frequency cf
        JOIN commentors co ON co.id = cf.commentor_id
        WHERE cf.profile_id = ?
        ORDER BY cf.total_count DESC
    """, (profile_id,))

    commentors = []
    for row in cur.fetchall():
        c = dict(row)

        # Fetch sample comments for onclick panel
        cur.execute("""
            SELECT pc.comment_text, pp.photo_url as post_url
            FROM photo_comments pc
            JOIN photo_posts pp ON pp.id = pc.photo_post_id
            WHERE pc.commentor_id = ? AND pp.profile_id = ?
        """, (c['commentor_id'], profile_id))
        comments = [{'comment_text': r[0], 'post_url': r[1], 'type': 'photo'}
                    for r in cur.fetchall()]

        cur.execute("""
            SELECT rc.comment_text, rp.reel_url as post_url
            FROM reel_comments rc
            JOIN reel_posts rp ON rp.id = rc.reel_post_id
            WHERE rc.commentor_id = ? AND rp.profile_id = ?
        """, (c['commentor_id'], profile_id))
        comments += [{'comment_text': r[0], 'post_url': r[1], 'type': 'reel'}
                     for r in cur.fetchall()]

        cur.execute("""
            SELECT tc.comment_text, tp.post_url
            FROM text_comments tc
            JOIN text_posts tp ON tp.id = tc.text_post_id
            WHERE tc.commentor_id = ? AND tp.profile_id = ?
        """, (c['commentor_id'], profile_id))
        comments += [{'comment_text': r[0], 'post_url': r[1], 'type': 'text'}
                     for r in cur.fetchall()]

        c['comments'] = comments
        commentors.append(c)

    con.close()
    return {'commentors': commentors}


def get_cocomment_graph(db_file=DB_FILE, profile_id=None):
    """
    Return co-commentor graph data (nodes + edges) for matrix and force graph.
    Edge weight = number of posts both commentors interacted on.
    """
    if not profile_id:
        p = get_profile_id(db_file)
        if not p:
            return {'nodes': [], 'edges': []}
        profile_id = p['id']

    con = sqlite3.connect(db_file)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Nodes — all commentors with frequency
    cur.execute("""
        SELECT
            co.id           as commentor_id,
            co.name,
            co.profile_url,
            cf.total_count  as comment_count
        FROM commentor_frequency cf
        JOIN commentors co ON co.id = cf.commentor_id
        WHERE cf.profile_id = ?
        ORDER BY cf.total_count DESC
    """, (profile_id,))
    nodes = [dict(row) for row in cur.fetchall()]

    # Edges — co-commenting pairs across all post types
    edges_map = {}

    # Photo co-commentors
    cur.execute("""
        SELECT a.commentor_id as id1, b.commentor_id as id2, COUNT(*) as w
        FROM photo_comments a
        JOIN photo_comments b ON b.photo_post_id = a.photo_post_id
            AND b.commentor_id > a.commentor_id
        JOIN photo_posts pp ON pp.id = a.photo_post_id
        WHERE pp.profile_id = ?
        GROUP BY a.commentor_id, b.commentor_id
    """, (profile_id,))
    for row in cur.fetchall():
        key = (row[0], row[1])
        edges_map[key] = edges_map.get(key, 0) + row[2]

    # Reel co-commentors
    cur.execute("""
        SELECT a.commentor_id as id1, b.commentor_id as id2, COUNT(*) as w
        FROM reel_comments a
        JOIN reel_comments b ON b.reel_post_id = a.reel_post_id
            AND b.commentor_id > a.commentor_id
        JOIN reel_posts rp ON rp.id = a.reel_post_id
        WHERE rp.profile_id = ?
        GROUP BY a.commentor_id, b.commentor_id
    """, (profile_id,))
    for row in cur.fetchall():
        key = (row[0], row[1])
        edges_map[key] = edges_map.get(key, 0) + row[2]

    # Text co-commentors
    cur.execute("""
        SELECT a.commentor_id as id1, b.commentor_id as id2, COUNT(*) as w
        FROM text_comments a
        JOIN text_comments b ON b.text_post_id = a.text_post_id
            AND b.commentor_id > a.commentor_id
        JOIN text_posts tp ON tp.id = a.text_post_id
        WHERE tp.profile_id = ?
        GROUP BY a.commentor_id, b.commentor_id
    """, (profile_id,))
    for row in cur.fetchall():
        key = (row[0], row[1])
        edges_map[key] = edges_map.get(key, 0) + row[2]

    edges = [
        {'source': k[0], 'target': k[1], 'weight': v}
        for k, v in edges_map.items()
    ]

    con.close()
    return {'nodes': nodes, 'edges': edges}


def get_interaction_timeline(db_file=DB_FILE, profile_id=None):
    """
    Return date-wise interaction counts for photo and text posts.
    Used for the interaction growth chart.
    """
    if not profile_id:
        p = get_profile_id(db_file)
        if not p:
            return []
        profile_id = p['id']

    con = sqlite3.connect(db_file)
    cur = con.cursor()

    timeline = {}

    # Photo post interactions by date
    cur.execute("""
        SELECT pp.date_text, COUNT(pc.id) as interactions
        FROM photo_posts pp
        LEFT JOIN photo_comments pc ON pc.photo_post_id = pp.id
        WHERE pp.profile_id = ? AND pp.date_text IS NOT NULL
        GROUP BY pp.date_text
    """, (profile_id,))
    for row in cur.fetchall():
        date = row[0]
        if date not in timeline:
            timeline[date] = {'date': date, 'photo': 0, 'text': 0, 'total': 0}
        timeline[date]['photo'] += row[1]
        timeline[date]['total'] += row[1]

    # Text post interactions by date
    cur.execute("""
        SELECT tp.date_text, COUNT(tc.id) as interactions
        FROM text_posts tp
        LEFT JOIN text_comments tc ON tc.text_post_id = tp.id
        WHERE tp.profile_id = ? AND tp.date_text IS NOT NULL
        GROUP BY tp.date_text
    """, (profile_id,))
    for row in cur.fetchall():
        date = row[0]
        if date not in timeline:
            timeline[date] = {'date': date, 'photo': 0, 'text': 0, 'total': 0}
        timeline[date]['text'] += row[1]
        timeline[date]['total'] += row[1]

    con.close()
    return sorted(timeline.values(), key=lambda x: x['date'])


def get_post_type_counts(db_file=DB_FILE, profile_id=None):
    """
    Return photo/reel/text post counts for donut chart.
    """
    if not profile_id:
        p = get_profile_id(db_file)
        if not p:
            return {}
        profile_id = p['id']

    con = sqlite3.connect(db_file)
    cur = con.cursor()

    cur.execute("SELECT COUNT(*) FROM photo_posts WHERE profile_id = ?", (profile_id,))
    photo_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM reel_posts WHERE profile_id = ?", (profile_id,))
    reel_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM text_posts WHERE profile_id = ?", (profile_id,))
    text_count = cur.fetchone()[0]

    # Total interactions per type
    cur.execute("""
        SELECT COUNT(pc.id) FROM photo_comments pc
        JOIN photo_posts pp ON pp.id = pc.photo_post_id
        WHERE pp.profile_id = ?
    """, (profile_id,))
    photo_interactions = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(rc.id) FROM reel_comments rc
        JOIN reel_posts rp ON rp.id = rc.reel_post_id
        WHERE rp.profile_id = ?
    """, (profile_id,))
    reel_interactions = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(tc.id) FROM text_comments tc
        JOIN text_posts tp ON tp.id = tc.text_post_id
        WHERE tp.profile_id = ?
    """, (profile_id,))
    text_interactions = cur.fetchone()[0]

    con.close()
    return {
        'posts': {
            'photo': photo_count,
            'reel':  reel_count,
            'text':  text_count,
            'total': photo_count + reel_count + text_count
        },
        'interactions': {
            'photo': photo_interactions,
            'reel':  reel_interactions,
            'text':  text_interactions,
            'total': photo_interactions + reel_interactions + text_interactions
        }
    }


def get_profile_summary(db_file=DB_FILE, profile_id=None):
    """
    Return full profile summary for dashboard header.
    """
    if not profile_id:
        p = get_profile_id(db_file)
        if not p:
            return {}
        profile_id = p['id']

    con = sqlite3.connect(db_file)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("""
        SELECT id, profile_url, owner_name, is_locked, scraped_at
        FROM profiles WHERE id = ?
    """, (profile_id,))
    profile = dict(cur.fetchone() or {})

    cur.execute("""
        SELECT section, field_type, label, value
        FROM profile_fields
        WHERE profile_id = ?
        ORDER BY section, id
    """, (profile_id,))
    profile['fields'] = [dict(row) for row in cur.fetchall()]

    counts = get_post_type_counts(db_file, profile_id)
    profile['post_counts']        = counts.get('posts', {})
    profile['interaction_counts'] = counts.get('interactions', {})

    cur.execute("""
        SELECT COUNT(DISTINCT commentor_id) FROM commentor_frequency
        WHERE profile_id = ?
    """, (profile_id,))
    profile['total_commentors'] = cur.fetchone()[0]

    con.close()
    return profile


if __name__ == "__main__":
    # Quick test
    profile = get_profile_id()
    if profile:
        print(f"Profile: {profile['owner_name']} ({profile['profile_url']})")
        counts = get_post_type_counts(profile_id=profile['id'])
        print(f"Posts:   {counts['posts']}")
        print(f"Interactions: {counts['interactions']}")
        top7 = get_top7(profile_id=profile['id'])
        print(f"Top 7:")
        for t in top7:
            print(f"  #{t['rank']} {t['name']} — {t['comment_count']} interactions")
    else:
        print("No profile found in DB")