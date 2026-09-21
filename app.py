import streamlit as st
import sqlite3
import pandas as pd
import re
import zipfile
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

# ---------- CONFIG ----------
st.set_page_config(page_title="Rush Tracker", page_icon="🏠", layout="wide")

DB_PATH = "rush.db"
HOUSE_PIN = st.secrets.get("HOUSE_PIN", "changeme")        # regular members
ADMIN_PIN = st.secrets.get("ADMIN_PIN", "adminchangeme")   # the one person who can tag "dirty"

# Curated quick-add tags — kept short and non-sensitive on purpose (no drug
# references, nothing that could read badly if a CSV export ever left the
# house). Anyone can still type a custom tag in the box below these.
SUGGESTED_TAGS = [
    "Sports", "Athlete", "Gym Rat", "Skiing/Snow", "Outdoors", "Hiking",
    "Film/Art", "Music",
    "Shy but Cool", "Ferda",
    "Out of State", "Junior Transfer",
    "Pre-Med", "Business", "STEM",
]

# ---------- DB SETUP ----------
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    # WAL lets reads and writes coexist instead of blocking each other, and
    # busy_timeout makes a write wait/retry for 5s instead of instantly
    # erroring — both matter once dozens of people are voting at once.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pnms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            hometown TEXT,
            major TEXT,
            photo BLOB,
            dirty INTEGER DEFAULT 0,
            assigned_to TEXT,
            texted INTEGER DEFAULT 0,
            phone TEXT,
            tags TEXT,
            checked_in_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migration for the already-deployed db, which was created before these
    # columns existed — CREATE TABLE IF NOT EXISTS above is a no-op against
    # it, so add columns by hand if missing.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(pnms)").fetchall()}
    if "assigned_to" not in existing_cols:
        conn.execute("ALTER TABLE pnms ADD COLUMN assigned_to TEXT")
    if "texted" not in existing_cols:
        conn.execute("ALTER TABLE pnms ADD COLUMN texted INTEGER DEFAULT 0")
    if "phone" not in existing_cols:
        conn.execute("ALTER TABLE pnms ADD COLUMN phone TEXT")
    if "tags" not in existing_cols:
        conn.execute("ALTER TABLE pnms ADD COLUMN tags TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pnm_id INTEGER,
            member_name TEXT,
            vote INTEGER,
            UNIQUE(pnm_id, member_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pnm_id INTEGER,
            member_name TEXT,
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Replaces the old pnms.tags comma-separated column — that had no way
    # to know who added a given tag. One row per (pnm, tag), so we can show
    # "added by X" on hover. The old tags column is left in place, unused.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pnm_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pnm_id INTEGER,
            tag TEXT,
            added_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(pnm_id, tag)
        )
    """)
    conn.commit()
    return conn

conn = get_conn()

# ---------- HELPERS ----------
def fetch_pnms(dirty=None):
    df = pd.read_sql_query("SELECT * FROM pnms ORDER BY checked_in_at DESC", conn)
    # pandas reads a SQL NULL in a text column back as NaN (a float), not
    # None — and NaN is still truthy in Python, so `row["tags"] or ""`
    # doesn't catch it and `.split(",")` crashes on a float. Fill these
    # columns with "" right here so every caller gets a real string.
    for col in ("hometown", "major", "phone", "tags", "assigned_to"):
        df[col] = df[col].fillna("")
    if dirty is not None:
        df = df[df["dirty"] == (1 if dirty else 0)]
    return df

def vote_counts(pnm_id):
    row = conn.execute(
        "SELECT "
        "COALESCE(SUM(CASE WHEN vote=1 THEN 1 ELSE 0 END),0), "
        "COALESCE(SUM(CASE WHEN vote=0 THEN 1 ELSE 0 END),0), "
        "COALESCE(SUM(CASE WHEN vote=-1 THEN 1 ELSE 0 END),0) "
        "FROM votes WHERE pnm_id=?",
        (pnm_id,)
    ).fetchone()
    return row  # (yes, maybe, no)

def my_vote(pnm_id, member_name):
    row = conn.execute(
        "SELECT vote FROM votes WHERE pnm_id=? AND member_name=?",
        (pnm_id, member_name)
    ).fetchone()
    return row[0] if row else None

def cast_vote(pnm_id, member_name, value):
    existing = my_vote(pnm_id, member_name)
    try:
        if existing == value:
            conn.execute("DELETE FROM votes WHERE pnm_id=? AND member_name=?", (pnm_id, member_name))
        else:
            conn.execute(
                "INSERT INTO votes (pnm_id, member_name, vote) VALUES (?,?,?) "
                "ON CONFLICT(pnm_id, member_name) DO UPDATE SET vote=excluded.vote",
                (pnm_id, member_name, value)
            )
        conn.commit()
        return True
    except sqlite3.OperationalError:
        st.error("That didn't save — the app's busy, try again in a second.")
        return False

def add_comment(pnm_id, member_name, text):
    try:
        conn.execute(
            "INSERT INTO comments (pnm_id, member_name, comment) VALUES (?,?,?)",
            (pnm_id, member_name, text)
        )
        conn.commit()
        return True
    except sqlite3.OperationalError:
        st.error("That didn't save — the app's busy, try again in a second.")
        return False

def get_comments(pnm_id):
    return conn.execute(
        "SELECT member_name, comment, created_at FROM comments WHERE pnm_id=? ORDER BY created_at DESC",
        (pnm_id,)
    ).fetchall()

def set_dirty(pnm_id, value):
    try:
        conn.execute("UPDATE pnms SET dirty=? WHERE id=?", (1 if value else 0, pnm_id))
        conn.commit()
        return True
    except sqlite3.OperationalError:
        st.error("That didn't save — the app's busy, try again in a second.")
        return False

def set_assigned(pnm_id, assigned_to):
    try:
        conn.execute("UPDATE pnms SET assigned_to=? WHERE id=?", (assigned_to, pnm_id))
        conn.commit()
        return True
    except sqlite3.OperationalError:
        st.error("That didn't save — the app's busy, try again in a second.")
        return False

def update_pnm_info(pnm_id, name, hometown, major, phone):
    try:
        conn.execute(
            "UPDATE pnms SET name=?, hometown=?, major=?, phone=? WHERE id=?",
            (name, hometown, major, phone, pnm_id)
        )
        conn.commit()
        return True
    except sqlite3.OperationalError:
        st.error("That didn't save — the app's busy, try again in a second.")
        return False

def get_pnm_tags(pnm_id):
    """{tag: added_by} for one PNM, in the order tags were added."""
    rows = conn.execute(
        "SELECT tag, added_by FROM pnm_tags WHERE pnm_id=? ORDER BY created_at", (pnm_id,)
    ).fetchall()
    return {tag: added_by for tag, added_by in rows}

def get_all_pnm_tags():
    """{pnm_id: {tag: added_by}} for every PNM — one query instead of N."""
    rows = conn.execute("SELECT pnm_id, tag, added_by FROM pnm_tags").fetchall()
    result = {}
    for pnm_id, tag, added_by in rows:
        result.setdefault(pnm_id, {})[tag] = added_by
    return result

def add_pnm_tag(pnm_id, tag, added_by):
    try:
        conn.execute(
            "INSERT INTO pnm_tags (pnm_id, tag, added_by) VALUES (?,?,?) "
            "ON CONFLICT(pnm_id, tag) DO UPDATE SET added_by=excluded.added_by",
            (pnm_id, tag, added_by)
        )
        conn.commit()
        return True
    except sqlite3.OperationalError:
        st.error("That didn't save — the app's busy, try again in a second.")
        return False

def remove_pnm_tag(pnm_id, tag):
    try:
        conn.execute("DELETE FROM pnm_tags WHERE pnm_id=? AND tag=?", (pnm_id, tag))
        conn.commit()
        return True
    except sqlite3.OperationalError:
        st.error("That didn't save — the app's busy, try again in a second.")
        return False

def set_texted(pnm_id, value):
    try:
        conn.execute("UPDATE pnms SET texted=? WHERE id=?", (1 if value else 0, pnm_id))
        conn.commit()
        return True
    except sqlite3.OperationalError:
        st.error("That didn't save — the app's busy, try again in a second.")
        return False

def delete_pnm(pnm_id):
    try:
        conn.execute("DELETE FROM pnms WHERE id=?", (pnm_id,))
        conn.execute("DELETE FROM votes WHERE pnm_id=?", (pnm_id,))
        conn.execute("DELETE FROM comments WHERE pnm_id=?", (pnm_id,))
        conn.commit()
        return True
    except sqlite3.OperationalError:
        st.error("That didn't save — the app's busy, try again in a second.")
        return False

def render_pnm_card(row, member_name, is_admin, show_assign=False):
    with st.container(border=True):
        c1, c2 = st.columns([1, 3])
        with c1:
            if row["photo"] is not None:
                st.image(BytesIO(row["photo"]), width=150)
            else:
                st.write("No photo")
        with c2:
            st.subheader(row["name"])
            st.write(f"{row['hometown']} · {row['major']}")
            if row["phone"]:
                st.caption(f"📱 {row['phone']}")

            pnm_tag_map = get_pnm_tags(row["id"])  # {tag: added_by}

            st.caption("Tags — click to add/remove, hover to see who added it")
            for i in range(0, len(SUGGESTED_TAGS), 4):
                chunk = SUGGESTED_TAGS[i:i + 4]
                tag_cols = st.columns(4)
                for j, tag in enumerate(chunk):
                    with tag_cols[j]:
                        added_by = pnm_tag_map.get(tag)
                        active = added_by is not None
                        label = ("✅ " if active else "") + tag
                        help_text = f"Added by {added_by}" if active else None
                        if st.button(label, key=f"tagbtn_{row['id']}_{tag}", help=help_text):
                            ok = remove_pnm_tag(row["id"], tag) if active else add_pnm_tag(row["id"], tag, member_name)
                            if ok:
                                st.rerun()

            custom_existing = {t: by for t, by in pnm_tag_map.items() if t not in SUGGESTED_TAGS}
            if custom_existing:
                st.write(" · ".join(f"`{t}` _(by {by})_" for t, by in custom_existing.items()))
            tgc1, tgc2 = st.columns([3, 1])
            with tgc1:
                custom_tags_val = st.text_input(
                    "Other tags (comma-separated)",
                    value=", ".join(custom_existing.keys()),
                    key=f"customtags_{row['id']}",
                )
            with tgc2:
                st.write("")
                st.write("")
                if st.button("Save", key=f"tags_save_{row['id']}"):
                    custom_parsed = {t.strip() for t in custom_tags_val.split(",") if t.strip()}
                    existing_set = set(custom_existing.keys())
                    ok = True
                    for t in custom_parsed - existing_set:
                        ok = add_pnm_tag(row["id"], t, member_name) and ok
                    for t in existing_set - custom_parsed:
                        ok = remove_pnm_tag(row["id"], t) and ok
                    if ok:
                        st.rerun()

            yes, maybe, no = vote_counts(row["id"])
            mine = my_vote(row["id"], member_name)

            vc1, vc2, vc3, vc4 = st.columns([1, 1, 1, 2])
            with vc1:
                label = f"✅ {yes}" + (" ✓" if mine == 1 else "")
                if st.button(label, key=f"yes_{row['id']}"):
                    if cast_vote(row["id"], member_name, 1):
                        st.rerun()
            with vc2:
                label = f"🤔 {maybe}" + (" ✓" if mine == 0 else "")
                if st.button(label, key=f"maybe_{row['id']}"):
                    if cast_vote(row["id"], member_name, 0):
                        st.rerun()
            with vc3:
                label = f"❌ {no}" + (" ✓" if mine == -1 else "")
                if st.button(label, key=f"no_{row['id']}"):
                    if cast_vote(row["id"], member_name, -1):
                        st.rerun()
            with vc4:
                if is_admin:
                    if row["dirty"]:
                        if st.button("↩️ Move back", key=f"undirty_{row['id']}"):
                            if set_dirty(row["id"], False):
                                st.rerun()
                    else:
                        if st.button("🚩 Dirty", key=f"dirty_{row['id']}"):
                            if set_dirty(row["id"], True):
                                st.rerun()

            if show_assign:
                ac1, ac2, ac3 = st.columns([3, 1, 1.3])
                with ac1:
                    assign_val = st.text_input(
                        "Assigned to (who's texting him)",
                        value=row["assigned_to"] or "",
                        key=f"assign_input_{row['id']}",
                    )
                with ac2:
                    st.write("")
                    st.write("")
                    if st.button("Save", key=f"assign_save_{row['id']}"):
                        if set_assigned(row["id"], assign_val.strip()):
                            st.rerun()
                with ac3:
                    st.write("")
                    texted_val = st.checkbox(
                        "✅ Texted",
                        value=bool(row["texted"]),
                        key=f"texted_{row['id']}",
                    )
                    if texted_val != bool(row["texted"]):
                        if set_texted(row["id"], texted_val):
                            st.rerun()

            with st.expander(f"Comments ({len(get_comments(row['id']))})"):
                new_comment = st.text_input("Add a comment", key=f"comment_input_{row['id']}")
                if st.button("Post", key=f"post_{row['id']}"):
                    if new_comment.strip():
                        if add_comment(row["id"], member_name, new_comment.strip()):
                            st.rerun()
                for cmt_member, comment, created_at in get_comments(row["id"]):
                    st.write(f"**{cmt_member}**: {comment}")

            if is_admin:
                with st.expander("✏️ Edit / delete (admin)"):
                    e1, e2, e3, e4 = st.columns(4)
                    with e1:
                        edit_name = st.text_input("Name", value=row["name"], key=f"edit_name_{row['id']}")
                    with e2:
                        edit_hometown = st.text_input("Hometown", value=row["hometown"] or "", key=f"edit_hometown_{row['id']}")
                    with e3:
                        edit_major = st.text_input("Major", value=row["major"] or "", key=f"edit_major_{row['id']}")
                    with e4:
                        edit_phone = st.text_input("Phone", value=row["phone"] or "", key=f"edit_phone_{row['id']}")
                    if st.button("Save changes", key=f"edit_save_{row['id']}"):
                        if update_pnm_info(row["id"], edit_name.strip(), edit_hometown.strip(), edit_major.strip(), edit_phone.strip()):
                            st.rerun()

                    confirm_key = f"confirm_delete_{row['id']}"
                    if st.session_state.get(confirm_key):
                        st.warning("Delete this PNM permanently — votes and comments go with it. Can't be undone.")
                        dc1, dc2 = st.columns(2)
                        with dc1:
                            if st.button("Yes, delete", key=f"confirm_yes_{row['id']}"):
                                if delete_pnm(row["id"]):
                                    st.session_state[confirm_key] = False
                                    st.rerun()
                        with dc2:
                            if st.button("Cancel", key=f"confirm_no_{row['id']}"):
                                st.session_state[confirm_key] = False
                                st.rerun()
                    else:
                        if st.button("🗑️ Delete this PNM", key=f"delete_{row['id']}"):
                            st.session_state[confirm_key] = True
                            st.rerun()

# ---------- TOP-LEVEL NAV ----------
# Defaults to Member Area, not Check-In — landing straight on your own
# camera preview every time you open the link was jarring for members just
# trying to vote. The door station just needs one click to Check-In and
# can stay parked there.
if "area" not in st.session_state:
    st.session_state.area = "member"

with st.sidebar:
    st.write("### Rush Tracker")
    if st.button("📸 PNM Check-In", use_container_width=True):
        st.session_state.area = "checkin"
        st.rerun()
    if st.button("🔑 Member Area", use_container_width=True):
        st.session_state.area = "member"
        st.rerun()

# ---------- PUBLIC: PNM CHECK-IN (no PIN) ----------
if st.session_state.area == "checkin":
    st.title("📸 Check In")
    st.caption("Take a photo and fill this out — takes 30 seconds.")

    with st.form("checkin_form", clear_on_submit=True):
        photo = st.camera_input("Take your photo")
        name = st.text_input("Name")
        hometown = st.text_input("Hometown")
        major = st.text_input("Major")
        phone = st.text_input("Phone number")

        submitted = st.form_submit_button("Check In", type="primary")
        if submitted:
            if not name.strip():
                st.error("Name is required.")
            else:
                photo_bytes = photo.getvalue() if photo is not None else None
                try:
                    conn.execute(
                        "INSERT INTO pnms (name, hometown, major, phone, photo, checked_in_at) VALUES (?,?,?,?,?,?)",
                        (name.strip(), hometown.strip(), major.strip(), phone.strip(), photo_bytes, datetime.now())
                    )
                    conn.commit()
                    st.success(f"Thanks {name}, you're checked in!")
                except sqlite3.OperationalError:
                    st.error("That didn't save — the app's busy, hit Check In again.")

# ---------- MEMBER AREA (PIN required) ----------
else:
    if "authed" not in st.session_state:
        st.session_state.authed = False
    if "member_name" not in st.session_state:
        st.session_state.member_name = ""
    if "is_admin" not in st.session_state:
        st.session_state.is_admin = False

    if not st.session_state.authed:
        st.title("🔑 Member Area")
        st.subheader("Enter the house code to continue")
        pin = st.text_input("PIN", type="password")
        name = st.text_input("Your first name (used to tag your votes/comments)")
        if st.button("Enter", type="primary"):
            # Normalized (trimmed + title-cased) so "colin", "Colin ", and
            # "COLIN" all resolve to the same voter identity — otherwise the
            # one-vote-per-name UNIQUE constraint is trivially bypassed by
            # typing your name slightly differently.
            normalized_name = " ".join(name.strip().split()).title()
            if not normalized_name:
                st.error("Enter your name so we can attribute your votes.")
            elif pin == ADMIN_PIN:
                st.session_state.authed = True
                st.session_state.member_name = normalized_name
                st.session_state.is_admin = True
                st.rerun()
            elif pin == HOUSE_PIN:
                st.session_state.authed = True
                st.session_state.member_name = normalized_name
                st.session_state.is_admin = False
                st.rerun()
            else:
                st.error("Wrong PIN.")
        st.stop()

    st.sidebar.write(f"Signed in as **{st.session_state.member_name}**" + (" (admin)" if st.session_state.is_admin else ""))
    if st.sidebar.button("Switch user"):
        st.session_state.authed = False
        st.session_state.member_name = ""
        st.session_state.is_admin = False
        st.rerun()

    page = st.sidebar.radio("Go to", ["Vote & Comment", "Dirty Tab", "Browse by Tag", "Leaderboard / Export"])
    member_name = st.session_state.member_name
    is_admin = st.session_state.is_admin

    if page == "Vote & Comment":
        st.title("👍 Vote & Comment")
        df = fetch_pnms(dirty=False)
        all_pnm_tags = get_all_pnm_tags()

        all_tags = sorted({tag for tags in all_pnm_tags.values() for tag in tags})
        selected_tags = st.multiselect(
            "Filter by tag — find who to go talk to",
            all_tags,
        )
        if selected_tags:
            df = df[df["id"].apply(lambda pid: bool(set(all_pnm_tags.get(pid, {})) & set(selected_tags)))]

        search = st.text_input("Search by name")
        if search:
            df = df[df["name"].str.contains(search, case=False, na=False)]

        if df.empty:
            st.info("No PNMs checked in yet.")
        for _, row in df.iterrows():
            render_pnm_card(row, member_name, is_admin)

    elif page == "Dirty Tab":
        st.title("🚩 Dirty Rush List")
        st.caption("Guys tagged for dirty rush.")
        df = fetch_pnms(dirty=True)

        if df.empty:
            st.info("Nobody's been tagged yet.")
        for _, row in df.iterrows():
            render_pnm_card(row, member_name, is_admin, show_assign=True)

    elif page == "Browse by Tag":
        st.title("🏷️ Browse by Tag")
        st.caption("Find who to go talk to — pick a tag, see everyone who has it.")
        df = fetch_pnms()
        all_pnm_tags = get_all_pnm_tags()

        browse_all_tags = sorted({tag for tags in all_pnm_tags.values() for tag in tags})
        browse_selected = st.multiselect("Tags", browse_all_tags)

        if browse_selected:
            df = df[df["id"].apply(lambda pid: bool(set(all_pnm_tags.get(pid, {})) & set(browse_selected)))]
        else:
            st.info("Pick one or more tags above to filter — showing everyone for now.")

        if df.empty:
            st.info("No one matches those tags.")
        for _, row in df.iterrows():
            with st.container(border=True):
                c1, c2 = st.columns([1, 4])
                with c1:
                    if row["photo"] is not None:
                        st.image(BytesIO(row["photo"]), width=100)
                    else:
                        st.write("No photo")
                with c2:
                    st.subheader(row["name"])
                    st.write(f"{row['hometown']} · {row['major']}")
                    if row["phone"]:
                        st.caption(f"📱 {row['phone']}")
                    if row["dirty"]:
                        st.caption("🚩 Dirty rush")
                    row_tags = all_pnm_tags.get(row["id"], {})
                    if row_tags:
                        st.write(" · ".join(f"`{t}` _(by {by})_" for t, by in row_tags.items()))

    elif page == "Leaderboard / Export":
        st.title("📊 Leaderboard")
        df = fetch_pnms()
        all_pnm_tags = get_all_pnm_tags()

        if df.empty:
            st.info("No PNMs checked in yet.")
        else:
            rows = []
            for _, row in df.iterrows():
                yes, maybe, no = vote_counts(row["id"])
                # Ranking weight: a Yes counts full, a Maybe counts half, a
                # No counts zero — per direct request, not a simple up/down
                # net score anymore.
                score = yes * 1 + maybe * 0.5 + no * 0
                # Streamlit Cloud's server clock is UTC — check-in
                # timestamps are stored that way, so convert to Pacific
                # (where the house actually is) for display.
                checked_in_pt = (
                    pd.to_datetime(row["checked_in_at"])
                    .tz_localize("UTC")
                    .tz_convert(ZoneInfo("America/Los_Angeles"))
                    .strftime("%Y-%m-%d %I:%M %p")
                )
                rows.append({
                    "Name": row["name"],
                    "Hometown": row["hometown"],
                    "Major": row["major"],
                    "Phone": row["phone"] or "",
                    "Tags": ", ".join(all_pnm_tags.get(row["id"], {}).keys()),
                    "Yes": yes,
                    "Maybe": maybe,
                    "No": no,
                    "Score": score,
                    "Dirty": "✓" if row["dirty"] else "",
                    "Assigned": row["assigned_to"] or "",
                    "Texted": "✓" if row["texted"] else "",
                    "Comments": len(get_comments(row["id"])),
                    "Checked in": checked_in_pt,
                })
            board = pd.DataFrame(rows).sort_values("Score", ascending=False)
            st.dataframe(board, use_container_width=True, hide_index=True)

            csv = board.to_csv(index=False).encode("utf-8")
            dl1, dl2 = st.columns(2)
            with dl1:
                st.download_button("Download CSV", csv, "rush_leaderboard.csv", "text/csv")
            with dl2:
                zip_buf = BytesIO()
                with zipfile.ZipFile(zip_buf, "w") as zf:
                    for _, row in df.iterrows():
                        if row["photo"] is not None:
                            safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", row["name"]) or "pnm"
                            zf.writestr(f"{safe_name}_{row['id']}.jpg", row["photo"])
                st.download_button(
                    "Download all photos (ZIP)",
                    zip_buf.getvalue(),
                    "rush_photos.zip",
                    "application/zip",
                )
                st.caption("Do this every night — SQLite storage on Streamlit Cloud isn't guaranteed to survive restarts.")
